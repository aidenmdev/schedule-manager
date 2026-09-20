"""Installer logic and where the app keeps its files. Uses temp folders and a throwaway registry key.
Run:  .venv\\Scripts\\python.exe -m unittest tests.test_installer -v"""
import os
import sys
import tempfile
import unittest
import winreg
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
import install_lib as lib

TEST_KEY = r"Software\ScheduleManagerInstallerTest"


def make_payload(path: Path, extra=None, exe=True):
    with zipfile.ZipFile(path, "w") as z:
        if exe:
            z.writestr(lib.EXE_NAME, b"MZ fake program")
        z.writestr("_internal/library.dll", b"dll")
        z.writestr("_internal/data/config.example.json", "{}")
        for name, data in (extra or {}).items():
            z.writestr(name, data)


class PathTests(unittest.TestCase):
    def test_from_source_everything_stays_in_the_folder(self):
        data, res = core.resolve_dirs(False, env={}, source_dir=Path("C:/src/app"))
        self.assertEqual((data, res), (Path("C:/src/app"), Path("C:/src/app")))

    def test_installed_app_keeps_data_out_of_the_program_folder(self):
        data, res = core.resolve_dirs(True, r"C:\Apps\SM\ScheduleManager.exe", r"C:\Apps\SM\_internal",
                                      env={"LOCALAPPDATA": r"C:\Users\Me\AppData\Local"})
        self.assertEqual(data, Path(r"C:\Users\Me\AppData\Local") / "Schedule Manager")
        self.assertEqual(res, Path(r"C:\Apps\SM\_internal"))

    def test_data_folder_can_be_overridden(self):
        env = {"SCHEDULE_MANAGER_DATA": r"D:\sm-data", "LOCALAPPDATA": r"C:\x"}
        self.assertEqual(core.resolve_dirs(True, r"C:\a\b.exe", r"C:\a\_i", env=env)[0], Path(r"D:\sm-data"))
        self.assertEqual(core.resolve_dirs(False, env=env, source_dir=Path("C:/src"))[0], Path(r"D:\sm-data"))

    def test_frozen_without_meipass_uses_the_exe_folder(self):
        self.assertEqual(core.resolve_dirs(True, r"C:\a\b.exe", "", env={"LOCALAPPDATA": "C:/l"})[1], Path(r"C:\a"))

    def test_install_helpers_agree_with_the_app_about_the_data_folder(self):
        env = {"LOCALAPPDATA": r"C:\Users\Me\AppData\Local"}
        self.assertEqual(lib.data_dir(env), core.resolve_dirs(True, "C:/a/b.exe", "", env=env)[0])
        self.assertEqual(lib.default_install_dir(env), Path(r"C:\Users\Me\AppData\Local\Programs\Schedule Manager"))

    def test_bundled_credentials_are_copied_on_first_use(self):
        tmp = tempfile.TemporaryDirectory()
        saved = core.RESOURCE_DIR, core.CREDENTIALS_PATH, core.TOKEN_PATH
        d = Path(tmp.name)
        (d / "res").mkdir()
        (d / "res" / "credentials.json").write_text("{}", encoding="utf-8")
        core.RESOURCE_DIR, core.CREDENTIALS_PATH, core.TOKEN_PATH = d / "res", d / "credentials.json", d / "token.json"
        try:
            with self.assertRaises(Exception):
                core.get_credentials()   # not valid client secrets, but the copy happens first
            self.assertTrue((d / "credentials.json").exists())
        finally:
            core.RESOURCE_DIR, core.CREDENTIALS_PATH, core.TOKEN_PATH = saved
            tmp.cleanup()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folders = lib.Folders(self.root / "Desktop", self.root / "Programs", self.root / "Startup")
        self.payload = self.root / "payload.zip"
        make_payload(self.payload)
        self.dest = self.root / "Apps" / "Schedule Manager"

    def tearDown(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
        except OSError:
            pass
        self.tmp.cleanup()

    def install(self, **kw):
        return lib.install(self.payload, self.dest, version="9.9", folders=self.folders, reg_key=TEST_KEY, **kw)

    def test_installs_files_registers_and_makes_shortcuts(self):
        exe = self.install()
        self.assertEqual(exe, self.dest / lib.EXE_NAME)
        self.assertTrue((self.dest / "_internal" / "library.dll").exists())
        self.assertTrue((self.folders.desktop / lib.DESKTOP_LINK).exists())
        for name in lib.SHORTCUTS:
            self.assertTrue((self.folders.programs / lib.START_MENU_FOLDER / name).exists(), name)
        self.assertFalse((self.folders.startup / lib.STARTUP_LINK).exists())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            self.assertEqual(winreg.QueryValueEx(key, "DisplayVersion")[0], "9.9")
            self.assertEqual(winreg.QueryValueEx(key, "InstallLocation")[0], str(self.dest))
            self.assertEqual(winreg.QueryValueEx(key, "UninstallString")[0], f'"{exe}" --uninstall')
        self.assertEqual(lib.installed_location(TEST_KEY), self.dest)

    def test_shortcuts_point_at_the_exe_with_the_right_flags(self):
        self.install(tablet_autostart=True)
        import subprocess
        script = ("$w = New-Object -ComObject WScript.Shell; foreach ($p in @(" +
                  ",".join(lib._ps_quote(p) for p in (
                      self.folders.programs / lib.START_MENU_FOLDER / "Tablet Display.lnk",
                      self.folders.programs / lib.START_MENU_FOLDER / "Uninstall Schedule Manager.lnk",
                      self.folders.startup / lib.STARTUP_LINK)) +
                  ")) { $s = $w.CreateShortcut($p); \"$($s.TargetPath)|$($s.Arguments)\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.split("\n")
        exe = str(self.dest / lib.EXE_NAME)
        self.assertEqual([o.strip() for o in out[:3]], [f"{exe}|--tablet-launch", f"{exe}|--uninstall",
                                                         f"{exe}|--tablet-launch --quiet"])

    def test_shortcut_options_can_be_switched_off(self):
        self.install(desktop=False, start_menu=False)
        self.assertFalse((self.folders.desktop / lib.DESKTOP_LINK).exists())
        self.assertFalse((self.folders.programs / lib.START_MENU_FOLDER).exists())

    def test_update_replaces_program_files_and_drops_switched_off_shortcuts(self):
        self.install(tablet_autostart=True)
        (self.dest / "stale.txt").write_text("old", encoding="utf-8")
        (self.dest / "_internal" / "old.dll").write_bytes(b"x")
        make_payload(self.payload, {"_internal/new.dll": b"new"})
        self.install(tablet_autostart=False)
        self.assertFalse((self.dest / "stale.txt").exists())
        self.assertFalse((self.dest / "_internal" / "old.dll").exists())
        self.assertTrue((self.dest / "_internal" / "new.dll").exists())
        self.assertFalse((self.folders.startup / lib.STARTUP_LINK).exists())

    def test_refuses_a_folder_that_has_other_files(self):
        self.dest.mkdir(parents=True)
        (self.dest / "important.docx").write_text("mine", encoding="utf-8")
        with self.assertRaises(lib.InstallError) as ctx:
            self.install()
        self.assertIn("other files", str(ctx.exception))
        self.assertTrue((self.dest / "important.docx").exists())

    def test_empty_existing_folder_is_fine(self):
        self.dest.mkdir(parents=True)
        self.install()
        self.assertTrue((self.dest / lib.EXE_NAME).exists())

    def test_rejects_a_package_that_writes_outside_the_folder(self):
        make_payload(self.payload, {"../escaped.txt": b"bad"})
        with self.assertRaises(lib.InstallError):
            self.install()
        self.assertFalse((self.root / "Apps" / "escaped.txt").exists())

    def test_rejects_a_damaged_or_incomplete_package(self):
        self.payload.write_bytes(b"not a zip")
        with self.assertRaises(lib.InstallError):
            self.install()
        make_payload(self.payload, exe=False)
        with self.assertRaises(lib.InstallError) as ctx:
            self.install()
        self.assertIn(lib.EXE_NAME, str(ctx.exception))

    def test_progress_is_reported(self):
        seen = []
        self.install(progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertGreaterEqual(len(seen), 3)

    def test_uninstall_removes_program_shortcuts_and_registry_but_keeps_data(self):
        data = self.root / "data"
        data.mkdir()
        (data / "config.json").write_text("{}", encoding="utf-8")
        self.install(tablet_autostart=True)
        lib.uninstall(self.dest, folders=self.folders, reg_key=TEST_KEY, data=data)
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.folders.desktop / lib.DESKTOP_LINK).exists())
        self.assertFalse((self.folders.programs / lib.START_MENU_FOLDER).exists())
        self.assertFalse((self.folders.startup / lib.STARTUP_LINK).exists())
        self.assertIsNone(lib.installed_location(TEST_KEY))
        self.assertTrue((data / "config.json").exists())

    def test_uninstall_can_also_delete_data(self):
        data = self.root / "data"
        data.mkdir()
        (data / "config.json").write_text("{}", encoding="utf-8")
        self.install()
        lib.uninstall(self.dest, delete_data=True, folders=self.folders, reg_key=TEST_KEY, data=data)
        self.assertFalse(data.exists())

    def test_deferred_uninstall_removes_the_folder_after_a_moment(self):
        import time
        self.install()
        old_cwd = os.getcwd()
        os.chdir(self.dest)      # like a shortcut that starts the program in its own folder
        try:
            lib.uninstall(self.dest, folders=self.folders, reg_key=TEST_KEY, data=self.root / "d", defer_delete=True)
        finally:
            os.chdir(old_cwd)
        self.assertTrue(self.dest.exists())     # not yet: the helper waits for this program to exit
        end = time.time() + 20
        while self.dest.exists() and time.time() < end:
            time.sleep(0.5)
        self.assertFalse(self.dest.exists())

    def test_uninstall_when_already_partly_gone(self):
        lib.uninstall(self.root / "nothing-here", folders=self.folders, reg_key=TEST_KEY, data=self.root / "nodata")

    def test_reinstall_after_uninstall(self):
        self.install()
        lib.uninstall(self.dest, folders=self.folders, reg_key=TEST_KEY, data=self.root / "d")
        self.install()
        self.assertTrue((self.dest / lib.EXE_NAME).exists())

    def test_no_processes_found_for_an_unused_folder(self):
        self.assertEqual(lib.running_processes(self.root / "never-run"), [])

    def test_apostrophes_in_paths_survive_shortcut_creation(self):
        weird = lib.Folders(self.root / "Bob's Desktop", self.root / "Bob's Programs", self.root / "Startup")
        lib.install(self.payload, self.root / "Bob's Apps" / "SM", version="1", folders=weird, reg_key=TEST_KEY)
        self.assertTrue((weird.desktop / lib.DESKTOP_LINK).exists())

    def test_real_windows_folders_are_found(self):
        f = lib.system_folders()
        for p in (f.desktop, f.programs, f.startup):
            self.assertTrue(p.exists(), p)
        self.assertTrue(str(f.programs).lower().endswith("programs"))


class SetupArgsTests(unittest.TestCase):
    def test_silent_flags(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
        import setup_app
        a = setup_app.parse(["/S", "--dir", r"C:\x", "--no-desktop", "--tablet-autostart", "--no-launch"])
        self.assertTrue(a.silent and a.no_desktop and a.tablet_autostart and a.no_launch)
        self.assertEqual(a.dir, r"C:\x")
        self.assertEqual(setup_app.make_options(a), {"desktop": False, "start_menu": True, "tablet_autostart": True})
        self.assertFalse(setup_app.parse([]).silent)

    def test_silent_install_end_to_end(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
        import setup_app
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        payload = root / "p.zip"
        make_payload(payload)
        real_key, real_folders = lib.REG_KEY, lib.system_folders
        lib.REG_KEY = TEST_KEY
        lib.system_folders = lambda: lib.Folders(root / "D", root / "P", root / "S")
        try:
            args = setup_app.parse(["--silent", "--dir", str(root / "app"), "--payload", str(payload), "--no-launch"])
            self.assertEqual(setup_app.silent_install(args), 0)
            self.assertTrue((root / "app" / lib.EXE_NAME).exists())
            self.assertTrue((root / "D" / lib.DESKTOP_LINK).exists())
            bad = setup_app.parse(["--silent", "--dir", str(root / "app"), "--payload", str(root / "missing.zip"), "--no-launch"])
            self.assertEqual(setup_app.silent_install(bad), 1)
        finally:
            lib.REG_KEY, lib.system_folders = real_key, real_folders
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
            except OSError:
                pass
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
