"""Publishing, finding, verifying and applying updates. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_updater -v"""
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
from tests.fakes import LocalRepo

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
        self.repo = LocalRepo(self.root / "remote.git")

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
        updater.publish(package, manifest, self.private, self.repo.path)

    def check(self, build=BASE):
        return updater.find_updates(updater.BuildInfo("2.0", build, BASE), fetch=self.repo.fetch)

    def download(self, update):
        return updater.download(update, fetch=self.repo.fetch)


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


class ChannelTests(Case):
    """Updates travel through a branch of the GitHub project."""

    def test_round_trip(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "exe v2"}, notes="Fixed the thing.")
        self.publish(package, manifest)
        update = self.check().available
        self.assertEqual((update.version, update.build, update.base), ("2.0", BASE + 10, BASE))
        self.assertEqual(update.notes, "Fixed the thing.")
        self.assertEqual(self.download(update), package)

    def test_the_requests_go_to_the_right_place_and_skip_the_cache(self):
        self.check()
        url = self.repo.requests[0]
        self.assertTrue(url.startswith(f"https://raw.githubusercontent.com/{updater.DEFAULT_REPO}/updates/update.json?t="))

    def test_the_project_comes_from_the_build_info(self):
        info = updater.BuildInfo("2.0", BASE, BASE, "someone/else")
        updater.find_updates(info, fetch=self.repo.fetch)
        self.assertIn("raw.githubusercontent.com/someone/else/updates/update.json", self.repo.requests[0])

    def test_publishing_again_replaces_the_update_and_keeps_history_flat(self):
        for build, text in ((BASE + 5, "v5"), (BASE + 9, "v9")):
            package, manifest, _, _ = self.new_build(f"b{build}", build, {"ScheduleManager.exe": text})
            self.publish(package, manifest)
        self.assertEqual(self.check().available.build, BASE + 9)
        import subprocess
        count = subprocess.run(["git", "--git-dir", self.repo.path, "rev-list", "--count", "updates"], capture_output=True, text=True)
        self.assertEqual(count.stdout.strip(), "1")

    def test_nothing_published_yet_means_up_to_date(self):
        check = self.check()
        self.assertIsNone(check.available)
        self.assertIsNone(check.needs_new_installer)

    def test_same_or_older_build_is_not_offered(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 5, {"ScheduleManager.exe": "v5"})
        self.publish(package, manifest)
        self.assertIsNone(self.check(build=BASE + 5).available)
        self.assertIsNone(self.check(build=BASE + 6).available)

    def test_update_from_a_different_installer_asks_for_the_new_installer(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "v"})
        self.publish(package, dict(manifest, base=BASE + 999))
        check = self.check()
        self.assertIsNone(check.available)
        self.assertEqual(check.needs_new_installer.build, BASE + 10)

    def test_unreadable_update_information_is_reported(self):
        for bad in (b"not json", b"{}", b'{"build": "x"}', b"\xff\xfe"):
            with self.assertRaises(updater.UpdateError):
                updater.parse_meta(bad, updater.DEFAULT_REPO)

    def test_offline_raises_a_network_error(self):
        import dominos_schedule as core
        self.repo.offline = True
        with self.assertRaises(OSError) as ctx:
            self.check()
        self.assertTrue(core.is_network_error(ctx.exception))

    def test_an_incomplete_download_is_refused(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "v"})
        self.publish(package, manifest)
        update = self.check().available
        with self.assertRaises(updater.UpdateError):
            updater.download(update, fetch=lambda url, progress=None: self.repo.fetch(url)[:-5])

    def test_progress_is_reported(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"_internal/data.json": os.urandom(4000)})
        self.publish(package, manifest)
        seen = []
        updater.download(self.check().available, lambda done, total: seen.append((done, total)), fetch=self.repo.fetch)
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_publishing_to_a_missing_remote_reports_the_problem(self):
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "v"})
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.publish(package, manifest, self.private, str(self.root / "no-such-remote.git"))
        self.assertIn("git push failed", str(ctx.exception))

    def test_only_the_signed_package_is_trusted(self):
        """Anyone can read or even overwrite the branch; without the signing key an update still won't install."""
        updater.generate_keys(self.root / "evil")
        evil = updater.load_private_key(self.root / "evil" / "update_signing_key.pem")
        package, manifest, _, _ = self.new_build("b1", BASE + 10, {"ScheduleManager.exe": "malware"})
        updater.publish(package, manifest, evil, self.repo.path)
        update = self.check().available
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.stage(self.download(update), update, updater.BuildInfo("2.0", BASE, BASE), self.public,
                          self.device(), self.root / "stage")
        self.assertIn("not signed", str(ctx.exception))

    def test_the_real_http_download_code_reads_a_web_server(self):
        import http.server
        import threading
        import urllib.error
        payload = os.urandom(200_000)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/missing"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            seen = []
            self.assertEqual(updater.http_get(base + "/file", lambda d, t: seen.append((d, t))), payload)
            self.assertEqual(seen[-1], (len(payload), len(payload)))
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                updater.http_get(base + "/missing")
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


class StageTests(Case):
    def offered(self, changes, build=BASE + 10, notes="", removed=(), sign_with=None):
        package, manifest, ever, _ = self.new_build(f"b{build}", build, changes, notes=notes, removed=removed)
        updater.publish(package, manifest, sign_with or self.private, self.repo.path)
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
        updater.publish(package, manifest, self.private, self.repo.path)
        update = self.check(build=BASE + 1).available
        staged = updater.stage(package, update, updater.BuildInfo("2.0", BASE + 1, BASE), self.public, device, self.root / "stage2")
        self.assertEqual(sorted(staged.manifest["included"]), ["ScheduleManager.exe", "_internal/data.json"])


class ApplyTests(Case):
    def run_script(self, script, timeout=90):
        return subprocess.run([str(script)], capture_output=True, text=True, timeout=timeout, cwd=str(self.root))

    def staged_update(self, changes, removed=()):
        package, manifest, ever, _ = self.new_build("nb", BASE + 10, changes, removed=removed)
        updater.publish(package, manifest, self.private, self.repo.path)
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
