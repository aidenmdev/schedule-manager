"""Publishing, finding, verifying and applying updates. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_updater -v"""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import updater
from tests.fakes import FakeMailbox

BASE = 20260101000000


def make_tree(root: Path, files: dict):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


INSTALLER = {"ScheduleManager.exe": "exe v1", "_internal/lib.dll": "dll", "_internal/data.json": "data",
             "_internal/build_info.json": "info1", "_internal/credentials.json": "SECRET-A"}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        updater.generate_keys(self.root / "keys")
        self.private = updater.load_private_key(self.root / "keys" / "update_signing_key.pem")
        self.public = updater.load_public_key(self.root / "keys" / updater.KEY_NAME)
        self.baseline = updater.tree_manifest(make_tree(self.root / "baseline", INSTALLER))
        self.mail = FakeMailbox()

    def tearDown(self):
        self.tmp.cleanup()

    def device(self, name="device", changes=None):
        files = dict(INSTALLER)
        files.update(changes or {})
        return make_tree(self.root / name, files)

    def new_build(self, name, build, changes, notes="", ever=None, removed=()):
        files = dict(INSTALLER)
        files.update(changes)
        for gone in removed:
            files.pop(gone)
        tree = make_tree(self.root / name, files)
        info = updater.BuildInfo("2.0", build, BASE)
        return updater.build_package(tree, self.baseline, info, notes, set(ever or ())) + (tree,)

    def publish(self, package, manifest):
        return updater.publish(self.mail, package, manifest, self.private)

    def check(self, build=BASE):
        return updater.find_updates(self.mail, updater.BuildInfo("2.0", build, BASE))


class PackageTests(Case):
    def test_manifest_skips_the_credentials_file(self):
        self.assertNotIn("_internal/credentials.json", self.baseline)
        self.assertIn("ScheduleManager.exe", self.baseline)

    def test_only_changed_files_are_shipped(self):
        package, manifest, ever, _ = self.new_build("b1", BASE + 1, {"ScheduleManager.exe": "exe v2", "_internal/build_info.json": "info2"})
        self.assertEqual(manifest["included"], ["ScheduleManager.exe", "_internal/build_info.json"])
        self.assertEqual(set(manifest["files"]), set(self.baseline))
        names = zipfile.ZipFile(io.BytesIO(package)).namelist()
        self.assertEqual(sorted(names), ["files/ScheduleManager.exe", "files/_internal/build_info.json", "manifest.json"])
        self.assertEqual(ever, {"ScheduleManager.exe", "_internal/build_info.json"})

    def test_a_file_that_changed_once_keeps_being_shipped_even_if_it_goes_back(self):
        _, _, ever, _ = self.new_build("b1", BASE + 1, {"_internal/data.json": "data v2"})
        _, manifest, _, _ = self.new_build("b2", BASE + 2, {"ScheduleManager.exe": "exe v3"}, ever=ever)
        self.assertIn("_internal/data.json", manifest["included"])   # so a computer on build 1 gets it reverted

    def test_credentials_are_never_packaged(self):
        _, manifest, _, _ = self.new_build("b1", BASE + 1, {"_internal/credentials.json": "SECRET-B"})
        self.assertNotIn("_internal/credentials.json", manifest["files"])
        self.assertNotIn("_internal/credentials.json", manifest["included"])

    def test_unchanged_build_ships_nothing_but_the_manifest(self):
        _, manifest, _, _ = self.new_build("b1", BASE + 1, {})
        self.assertEqual(manifest["included"], [])


class SigningTests(Case):
    def test_valid_and_tampered(self):
        sig = updater.sign(self.private, 5, BASE, "abc")
        self.assertTrue(updater.signature_ok(self.public, 5, BASE, "abc", sig))
        self.assertFalse(updater.signature_ok(self.public, 5, BASE, "abd", sig))
        self.assertFalse(updater.signature_ok(self.public, 6, BASE, "abc", sig))
        self.assertFalse(updater.signature_ok(self.public, 5, BASE + 1, "abc", sig))
        self.assertFalse(updater.signature_ok(self.public, 5, BASE, "abc", "not base64 !!"))

    def test_someone_elses_key_is_rejected(self):
        updater.generate_keys(self.root / "other")
        other = updater.load_private_key(self.root / "other" / "update_signing_key.pem")
        sig = updater.sign(other, 5, BASE, "abc")
        self.assertFalse(updater.signature_ok(self.public, 5, BASE, "abc", sig))


class MailTests(Case):
    def test_round_trip_through_a_mailbox(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "exe v2"}, notes="Fixed the thing.")
        self.assertEqual(self.publish(package, manifest), 1)
        check = self.check()
        self.assertEqual(check.available.build, BASE + 10)
        self.assertEqual(check.available.notes, "Fixed the thing.")
        self.assertEqual(check.available.version, "2.0")
        self.assertEqual(updater.download(self.mail, check.available), package)

    def test_large_updates_are_split_across_messages_and_reassembled(self):
        saved = updater.PART_BYTES
        updater.PART_BYTES = 700
        try:
            package, manifest, _, _ = self.new_build("b1", BASE + 10, {"_internal/data.json": os.urandom(5000)})
            sent = self.publish(package, manifest)
        finally:
            updater.PART_BYTES = saved
        self.assertGreater(sent, 3)
        subjects = [m["subject"] for m in self.mail.stored.values()]
        self.assertTrue(all(s.startswith(updater.SUBJECT_TAG) for s in subjects))
        self.assertIn(f"(part 1 of {sent})", subjects[0])
        update = self.check().available
        self.assertEqual(len(update.parts), sent)
        seen = []
        self.assertEqual(updater.download(self.mail, update, lambda i, n: seen.append((i, n))), package)
        self.assertEqual(seen[-1], (sent, sent))

    def test_the_package_travels_as_text_not_as_a_binary_attachment(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "exe v2"})
        self.publish(package, manifest)
        message = next(iter(self.mail.stored.values()))
        attachments = [p for p in message["payload"]["parts"] if p["filename"]]
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]["filename"].endswith(".txt"))
        self.assertEqual(attachments[0]["mimeType"], "text/plain")

    def test_wire_size_stays_well_under_gmails_limit(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"_internal/data.json": os.urandom(400_000)})
        self.publish(package, manifest)
        self.assertLess(max(self.mail.sent_sizes), 1.5 * len(package) + 20_000)
        self.assertLess(updater.PART_BYTES * 1.4 * 1.4, 25 * 1024 * 1024)

    def test_big_messages_use_the_upload_endpoint_and_small_ones_do_not(self):
        small, manifest, _, _ = self.new_build("s", BASE + 1, {"ScheduleManager.exe": "tiny"})
        self.publish(small, manifest)
        self.assertEqual(self.mail.media_sends, 0)
        big, manifest, _, _ = self.new_build("g", BASE + 2, {"_internal/data.json": os.urandom(3_000_000)})
        self.publish(big, manifest)
        self.assertEqual(self.mail.media_sends, 1)
        self.assertEqual(updater.download(self.mail, self.check().available), big)

    def test_newest_complete_update_wins(self):
        for build, text in ((BASE + 5, "v5"), (BASE + 9, "v9")):
            package, manifest, _, _ = self.new_build(f"b{build}", build, {"ScheduleManager.exe": text})
            self.publish(package, manifest)
        self.assertEqual(self.check().available.build, BASE + 9)

    def test_up_to_date_and_older_updates_are_ignored(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 5, {"ScheduleManager.exe": "v5"})
        self.publish(package, manifest)
        self.assertIsNone(self.check(build=BASE + 5).available)
        self.assertIsNone(self.check(build=BASE + 6).available)

    def test_incomplete_update_is_not_offered(self):
        saved = updater.PART_BYTES
        updater.PART_BYTES = 700
        try:
            package, manifest, _, _ = self.new_build("b1", BASE + 10, {"_internal/data.json": os.urandom(3000)})
            self.publish(package, manifest)
        finally:
            updater.PART_BYTES = saved
        del self.mail.stored[next(iter(self.mail.stored))]   # one part was deleted
        self.assertIsNone(self.check().available)

    def test_update_from_a_different_installer_asks_for_the_new_installer(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "v"})
        manifest = dict(manifest, base=BASE + 999)
        self.publish(package, manifest)
        check = self.check()
        self.assertIsNone(check.available)
        self.assertEqual(check.needs_new_installer.build, BASE + 10)

    def test_ordinary_mail_and_other_senders_are_ignored(self):
        self.mail.add_plain("Weekly Schedule 9/21", "hello")
        self.mail.add_plain(f"{updater.SUBJECT_TAG} 2.0 build 99999999999999 (part 1 of 1)", "no machine line")
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "v"})
        self.publish(package, manifest)
        self.mail.stored[list(self.mail.stored)[-1]]["from"] = "stranger@example.net"
        self.assertIsNone(self.check().available)

    def test_offline_raises_a_network_error(self):
        self.mail.offline = True
        import dominos_schedule as core
        with self.assertRaises(OSError) as ctx:
            self.check()
        self.assertTrue(core.is_network_error(ctx.exception))

    def test_a_damaged_attachment_is_reported(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "v"})
        self.publish(package, manifest)
        key = next(iter(self.mail.attachment_data))
        self.mail.attachment_data[key] = b"@@@ not base64 @@@"
        with self.assertRaises(updater.UpdateError):
            updater.download(self.mail, self.check().available)


class StageTests(Case):
    def offered(self, changes, build=BASE + 10, notes="", removed=(), sign_with=None):
        package, manifest, ever, _ = self.new_build(f"b{build}", build, changes, notes=notes, removed=removed)
        updater.publish(self.mail, package, manifest, sign_with or self.private)
        update = self.check().available
        return package, update

    def stage(self, package, update, device, current=None):
        return updater.stage(package, update, current or updater.BuildInfo("2.0", BASE, BASE), self.public, device,
                             self.root / "stage")

    def test_a_good_update_stages_and_lists_what_it_removes(self):
        package, update = self.offered({"ScheduleManager.exe": "exe v2"}, removed=("_internal/data.json",))
        device = self.device()
        staged = self.stage(package, update, device)
        self.assertEqual((staged.folder / "files" / "ScheduleManager.exe").read_text(), "exe v2")
        self.assertEqual(staged.removed, ["_internal/data.json"])
        self.assertEqual((device / "ScheduleManager.exe").read_text(), "exe v1")   # nothing installed yet

    def test_credentials_on_the_device_are_never_marked_for_removal(self):
        package, update = self.offered({"ScheduleManager.exe": "exe v2"})
        staged = self.stage(package, update, self.device())
        self.assertNotIn("_internal/credentials.json", staged.removed)

    def test_an_update_signed_by_someone_else_is_refused(self):
        updater.generate_keys(self.root / "evil")
        evil = updater.load_private_key(self.root / "evil" / "update_signing_key.pem")
        package, update = self.offered({"ScheduleManager.exe": "malware"}, sign_with=evil)
        with self.assertRaises(updater.UpdateError) as ctx:
            self.stage(package, update, self.device())
        self.assertIn("not signed", str(ctx.exception))
        self.assertFalse((self.root / "stage" / "files").exists())

    def test_a_tampered_package_is_refused(self):
        package, update = self.offered({"ScheduleManager.exe": "exe v2"})
        with self.assertRaises(updater.UpdateError) as ctx:
            self.stage(package + b"x", update, self.device())
        self.assertIn("damaged", str(ctx.exception))

    def test_older_or_same_build_is_refused(self):
        package, update = self.offered({"ScheduleManager.exe": "exe v2"})
        with self.assertRaises(updater.UpdateError):
            self.stage(package, update, self.device(), current=updater.BuildInfo("2.0", BASE + 10, BASE))

    def test_different_installer_is_refused(self):
        package, update = self.offered({"ScheduleManager.exe": "exe v2"})
        with self.assertRaises(updater.UpdateError) as ctx:
            self.stage(package, update, self.device(), current=updater.BuildInfo("2.0", BASE, BASE - 1))
        self.assertIn("different installer", str(ctx.exception))

    def test_a_computer_whose_files_were_changed_is_told_to_reinstall(self):
        package, update = self.offered({"ScheduleManager.exe": "exe v2"})
        device = self.device(changes={"_internal/lib.dll": "someone edited me"})
        with self.assertRaises(updater.UpdateError) as ctx:
            self.stage(package, update, device)
        self.assertIn("latest Schedule Manager Setup", str(ctx.exception))

    def test_a_path_that_escapes_the_folder_is_refused(self):
        buf = io.BytesIO()
        manifest = {"version": "2.0", "build": BASE + 10, "base": BASE, "notes": "",
                    "files": {"../escaped.txt": hashlib.sha256(b"x").hexdigest()}, "included": ["../escaped.txt"]}
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest))
            z.writestr("files/../escaped.txt", b"x")
        package = buf.getvalue()
        sha = hashlib.sha256(package).hexdigest()
        update = updater.Update("2.0", BASE + 10, BASE, "", sha, updater.sign(self.private, BASE + 10, BASE, sha), len(package), 1)
        with self.assertRaises(updater.UpdateError):
            self.stage(package, update, self.device())
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_a_file_that_does_not_match_its_hash_is_refused(self):
        buf = io.BytesIO()
        manifest = {"version": "2.0", "build": BASE + 10, "base": BASE, "notes": "",
                    "files": dict(self.baseline, **{"ScheduleManager.exe": hashlib.sha256(b"honest").hexdigest()}),
                    "included": ["ScheduleManager.exe"]}
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest))
            z.writestr("files/ScheduleManager.exe", b"different")
        package = buf.getvalue()
        sha = hashlib.sha256(package).hexdigest()
        update = updater.Update("2.0", BASE + 10, BASE, "", sha, updater.sign(self.private, BASE + 10, BASE, sha), len(package), 1)
        with self.assertRaises(updater.UpdateError):
            self.stage(package, update, self.device())

    def test_a_computer_one_update_behind_ends_up_identical(self):
        _, _, ever, tree1 = self.new_build("b1", BASE + 1, {"_internal/data.json": "data v2", "ScheduleManager.exe": "exe v2"})
        device = make_tree(self.root / "dev", {**INSTALLER, "_internal/data.json": "data v2", "ScheduleManager.exe": "exe v2"})
        package, manifest, _, tree2 = self.new_build("b2", BASE + 2, {"ScheduleManager.exe": "exe v3"}, ever=ever)
        updater.publish(self.mail, package, manifest, self.private)
        update = self.check(build=BASE + 1).available
        staged = updater.stage(package, update, updater.BuildInfo("2.0", BASE + 1, BASE), self.public, device, self.root / "stage2")
        self.assertEqual(sorted(staged.manifest["included"]), ["ScheduleManager.exe", "_internal/data.json"])


class ApplyTests(Case):
    def run_script(self, script, timeout=90):
        return subprocess.run([str(script)], capture_output=True, text=True, timeout=timeout, cwd=str(self.root))

    def staged_update(self, changes, removed=()):
        package, manifest, ever, _ = self.new_build("nb", BASE + 10, changes, removed=removed)
        updater.publish(self.mail, package, manifest, self.private)
        update = self.check().available
        device = self.device()
        staged = updater.stage(package, update, updater.BuildInfo("2.0", BASE, BASE), self.public, device, self.root / "work" / "stage")
        return device, staged

    def script(self, staged, device, **kw):
        return updater.apply_script(staged, device, self.root / "work", wait_for_pid=999_999, exe_name="NotRunning.exe",
                                    restart=False, failure_note=self.root / "failed.txt", **kw)

    def test_files_are_swapped_removed_and_cleaned_up(self):
        device, staged = self.staged_update({"ScheduleManager.exe": "exe v2", "_internal/new.dll": "new"},
                                            removed=("_internal/data.json",))
        result = self.run_script(self.script(staged, device))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((device / "ScheduleManager.exe").read_text(), "exe v2")
        self.assertEqual((device / "_internal" / "new.dll").read_text(), "new")
        self.assertFalse((device / "_internal" / "data.json").exists())
        self.assertEqual((device / "_internal" / "lib.dll").read_text(), "dll")
        self.assertEqual((device / "_internal" / "credentials.json").read_text(), "SECRET-A")
        self.assertFalse((self.root / "failed.txt").exists())
        self.assertFalse((self.root / "work" / "backup").exists())
        self.assertFalse((self.root / "work" / "stage").exists())
        self.assertFalse((self.root / "work" / "apply-update.bat").exists())

    def test_a_failed_copy_puts_everything_back(self):
        device, staged = self.staged_update({"ScheduleManager.exe": "exe v2", "_internal/lib.dll": "dll v2"})
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.CreateFileW.restype = ctypes.c_void_p
        handle = kernel.CreateFileW(str(device / "_internal" / "lib.dll"), 0x80000000, 1, None, 3, 0, None)  # readable, not writable
        try:
            self.run_script(self.script(staged, device), timeout=120)
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
        self.assertEqual((device / "ScheduleManager.exe").read_text(), "exe v1")
        self.assertEqual((device / "_internal" / "lib.dll").read_text(), "dll")
        self.assertTrue((self.root / "failed.txt").exists())
        self.assertFalse((self.root / "work" / "backup").exists())

    def test_the_script_waits_for_the_app_to_close(self):
        device, staged = self.staged_update({"ScheduleManager.exe": "exe v2"})
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6)"])
        script = updater.apply_script(staged, device, self.root / "work", wait_for_pid=sleeper.pid, exe_name="NotRunning.exe",
                                      restart=False)
        proc = subprocess.Popen([str(script)], cwd=str(self.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time
        time.sleep(2.5)
        self.assertEqual((device / "ScheduleManager.exe").read_text(), "exe v1")   # still waiting
        proc.wait(timeout=60)
        sleeper.wait(timeout=10)
        self.assertEqual((device / "ScheduleManager.exe").read_text(), "exe v2")

    def test_restart_lines_are_only_written_when_asked(self):
        device, staged = self.staged_update({"ScheduleManager.exe": "exe v2"})
        text = updater.apply_script(staged, device, self.root / "work", 1, restart=True, restart_tablet=True).read_bytes().decode("mbcs")
        self.assertIn('start "" "%DEST%\\ScheduleManager.exe"', text)
        self.assertIn("--tablet-launch --quiet", text)
        text = updater.apply_script(staged, device, self.root / "work", 1, restart=False).read_bytes().decode("mbcs")
        self.assertNotIn("start ", text)


class BuildInfoTests(unittest.TestCase):
    def test_reading_and_describing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(updater.read_build_info(Path(d)))
            (Path(d) / updater.BUILD_INFO_NAME).write_text(json.dumps({"version": "2.0", "build": 20260919173045, "base": 20260919170000}))
            info = updater.read_build_info(Path(d))
            self.assertEqual(info, updater.BuildInfo("2.0", 20260919173045, 20260919170000))
            self.assertEqual(updater.describe_build(info), "Version 2.0, built 2026-09-19 17:30")
            (Path(d) / updater.BUILD_INFO_NAME).write_text("{ broken")
            self.assertIsNone(updater.read_build_info(Path(d)))

    def test_running_from_source_is_not_an_installed_app(self):
        self.assertFalse(updater.is_installed())


if __name__ == "__main__":
    unittest.main()
