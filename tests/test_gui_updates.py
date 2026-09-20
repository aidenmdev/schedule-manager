"""The app's update behavior against a fake mailbox. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_gui_updates -v"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

import dominos_schedule as core
import updater
from tests.fakes import LocalRepo
from tests.gui_env import Env, find_widgets, pump
from tests.test_updater import BASE, INSTALLER, make_tree


class GuiUpdateTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().install()
        g = self.env.g
        self.root = self.env.tmp
        self.repo = LocalRepo(self.root / "remote.git")
        updater.generate_keys(self.root / "keys")
        self.private = updater.load_private_key(self.root / "keys" / "update_signing_key.pem")
        self.device = make_tree(self.root / "installed", INSTALLER)
        self.baseline = updater.tree_manifest(self.device)
        self.info = updater.BuildInfo("2.0", BASE, BASE)
        self.relaunched = []

        self.saved = {"is_installed": updater.is_installed, "read_build_info": updater.read_build_info,
                      "resource": core.RESOURCE_DIR, "get": updater.http_get, "dir": g.App._install_dir,
                      "relaunch": g.App._relaunch, "tablet": g.App._tablet_running}
        updater.is_installed = lambda: True
        updater.read_build_info = lambda *a: self.info
        core.RESOURCE_DIR = self.root / "keys"
        updater.http_get = self.repo.fetch      # the app reads GitHub through this
        g.App._install_dir = lambda app: self.device
        g.App._relaunch = lambda app, script: self.relaunched.append(script)
        g.App._tablet_running = lambda app: False
        self.apps = []

    def tearDown(self):
        g = self.env.g
        updater.is_installed, updater.read_build_info = self.saved["is_installed"], self.saved["read_build_info"]
        core.RESOURCE_DIR = self.saved["resource"]
        updater.http_get, g.App._install_dir = self.saved["get"], self.saved["dir"]
        g.App._relaunch, g.App._tablet_running = self.saved["relaunch"], self.saved["tablet"]
        for app in self.apps:
            try:
                app.destroy()
            except Exception:
                pass
        self.env.uninstall()

    def start(self, seconds=2.0, prefs=None):
        if prefs is not None:
            (self.root / "gui_prefs.json").write_text(json.dumps(prefs), encoding="utf-8")
        app = self.env.g.App()
        self.apps.append(app)
        pump(app, seconds)
        return app

    def publish(self, build, changes, notes="Faster week view."):
        files = dict(INSTALLER)
        files.update(changes)
        tree = make_tree(self.root / f"build{build}", files)
        package, manifest, _ = updater.build_package(tree, self.baseline, updater.BuildInfo("2.0", build, BASE), notes, set())
        updater.publish(package, manifest, self.private, self.repo.path)

    def wait_for(self, app, predicate, seconds=6):
        end = time.time() + seconds
        while time.time() < end:
            pump(app, 0.2)
            if predicate():
                return True
        return False

    def test_help_page_shows_the_version_and_update_controls(self):
        app = self.start(prefs={"seen_build": BASE})
        app.show_page("help")
        pump(app, 0.8)
        page = app.pages["help"]
        self.assertIn("Version 2.0", page.version_lbl.cget("text"))
        texts = [getattr(w, "_text", "") for w in find_widgets(page, ctk.CTkButton)]
        self.assertIn("Check for updates", texts)
        self.assertEqual(self.env.errors, "")

    def test_updates_are_no_longer_on_the_settings_page(self):
        app = self.start(prefs={"seen_build": BASE})
        app.show_page("settings")
        pump(app, 0.8)
        texts = [getattr(w, "_text", "") for w in find_widgets(app.pages["settings"], ctk.CTkButton)]
        self.assertNotIn("Check for updates", texts)
        self.assertFalse(hasattr(app.pages["settings"], "update_lbl"))
        self.assertEqual(self.env.errors, "")

    def test_status_text_reaches_the_help_page_while_it_is_open(self):
        app = self.start(prefs={"seen_build": BASE})
        app.show_page("help")
        pump(app, 0.6)
        app.check_updates(manual=True)
        self.assertTrue(self.wait_for(app, lambda: "up to date" in app.pages["help"].update_lbl.cget("text")))

    def test_finds_an_update_and_offers_it(self):
        app = self.start(prefs={"seen_build": BASE})
        self.publish(BASE + 100, {"ScheduleManager.exe": "exe v2"}, notes="Faster week view.")
        app.check_updates(manual=True)
        self.assertTrue(self.wait_for(app, lambda: app.update_check is not None))
        self.assertEqual(app.update_check.available.build, BASE + 100)
        self.assertIn("Update available", app.update_text)
        self.assertEqual(self.env.errors, "")

    def test_says_when_you_are_up_to_date(self):
        app = self.start(prefs={"seen_build": BASE})
        app.check_updates(manual=True)
        self.assertTrue(self.wait_for(app, lambda: "up to date" in app.update_text))
        self.assertIsNone(app.update_check.available)

    def test_offline_is_quiet_and_recovers(self):
        app = self.start(prefs={"seen_build": BASE})
        self.repo.offline = True
        app.check_updates()
        self.assertTrue(self.wait_for(app, lambda: "Offline" in app.update_text))
        self.assertEqual(self.env.errors, "")
        self.repo.offline = False
        app.check_updates(manual=True)
        self.assertTrue(self.wait_for(app, lambda: "up to date" in app.update_text))

    def test_installing_downloads_verifies_and_hands_over_to_the_helper(self):
        app = self.start(prefs={"seen_build": BASE})
        self.publish(BASE + 100, {"ScheduleManager.exe": "exe v2", "_internal/new.dll": "new"})
        app.check_updates(manual=True)
        self.assertTrue(self.wait_for(app, lambda: app.update_check is not None))
        app.install_update(app.update_check.available)
        self.assertTrue(self.wait_for(app, lambda: bool(self.relaunched), seconds=10))
        script = self.relaunched[0]
        self.assertTrue(script.exists())
        text = script.read_bytes().decode("mbcs")
        self.assertIn(str(self.device), text)
        self.assertIn("ScheduleManager.exe", text)
        self.assertEqual((self.device / "ScheduleManager.exe").read_text(), "exe v1")   # the helper does the swap after exit
        self.assertEqual((core.BASE_DIR / "update" / "stage" / "files" / "_internal" / "new.dll").read_text(), "new")
        self.assertEqual(self.env.errors, "")

    def test_a_forged_update_is_refused_and_nothing_is_handed_over(self):
        app = self.start(prefs={"seen_build": BASE})
        updater.generate_keys(self.root / "evil")
        evil = updater.load_private_key(self.root / "evil" / "update_signing_key.pem")
        tree = make_tree(self.root / "evilbuild", {**INSTALLER, "ScheduleManager.exe": "malware"})
        package, manifest, _ = updater.build_package(tree, self.baseline, updater.BuildInfo("2.0", BASE + 100, BASE), "", set())
        updater.publish(package, manifest, evil, self.repo.path)
        app.check_updates(manual=True)
        self.assertTrue(self.wait_for(app, lambda: app.update_check is not None))
        toasts = []
        original = app.toast
        app.toast = lambda text, *a, **k: (toasts.append(text), original(text, *a, **k))[1]
        app.install_update(app.update_check.available)
        self.assertTrue(self.wait_for(app, lambda: any("not signed" in t for t in toasts)))
        self.assertEqual(self.relaunched, [])

    def test_a_copy_running_from_source_does_not_update(self):
        updater.is_installed = lambda: False
        app = self.start(prefs={"seen_build": BASE})
        app.check_updates(manual=True)
        pump(app, 0.5)
        self.assertIsNone(app.update_check)
        app.show_page("help")
        pump(app, 0.5)
        self.assertEqual(str(app.pages["help"].update_check_btn.cget("state")), "disabled")

    def test_automatic_checks_respect_the_switch_and_the_twelve_hour_gap(self):
        app = self.start(prefs={"seen_build": BASE, "auto_check_updates": False})
        calls = []
        app.check_updates = lambda manual=False: calls.append(manual)
        app.maybe_check_updates()
        self.assertEqual(calls, [])
        app.prefs["auto_check_updates"] = True
        app.prefs["last_update_check"] = time.time() - 3600
        app.maybe_check_updates()
        self.assertEqual(calls, [])
        app.prefs["last_update_check"] = time.time() - 13 * 3600
        app.maybe_check_updates()
        self.assertEqual(calls, [False])

    def test_opening_after_an_update_says_so_once(self):
        toasts = []
        original = self.env.g.App.toast
        self.env.g.App.toast = lambda app, text, *a, **k: (toasts.append(text), original(app, text, *a, **k))[1]
        try:
            app = self.start(prefs={"seen_build": BASE - 5})
            self.assertTrue(any("was updated to version 2.0" in t for t in toasts))
            self.assertEqual(app.prefs["seen_build"], BASE)
            toasts.clear()
            app.destroy()
            self.apps.remove(app)
            self.start()
            self.assertFalse(any("was updated" in t for t in toasts))
        finally:
            self.env.g.App.toast = original

    def test_a_failed_update_is_reported_on_the_next_start(self):
        (core.BASE_DIR / "update_failed.txt").write_text("nope", encoding="utf-8")
        toasts = []
        original = self.env.g.App.toast
        self.env.g.App.toast = lambda app, text, *a, **k: (toasts.append(text), original(app, text, *a, **k))[1]
        try:
            self.start(prefs={"seen_build": BASE})
        finally:
            self.env.g.App.toast = original
        self.assertTrue(any("previous version was restored" in t for t in toasts))
        self.assertFalse((core.BASE_DIR / "update_failed.txt").exists())


if __name__ == "__main__":
    unittest.main()
