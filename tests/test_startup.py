"""Startup edge cases: damaged/missing files must produce a clear message, not a crash.
Run:  .venv\\Scripts\\python.exe -m unittest tests.test_startup -v"""
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
from tests.gui_env import Env, pump


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().install()
        self.msgs = []
        g = self.env.g
        self.orig_toast = g.App.toast

        def spy(app, text, *a, **k):
            self.msgs.append(text)
            return self.orig_toast(app, text, *a, **k)
        g.App.toast = spy

    def tearDown(self):
        self.env.g.App.toast = self.orig_toast
        self.env.uninstall()

    def test_damaged_config_and_state_are_reported_not_crashed(self):
        core.CONFIG_PATH.write_text("{ broken", encoding="utf-8")
        (self.env.tmp / "state.json").write_text("{ broken too", encoding="utf-8")
        app = self.env.g.App()
        try:
            pump(app, 2.5)
            self.assertIsNone(app.config_data)
            joined = " | ".join(self.msgs)
            self.assertIn("config.json is damaged", joined)
            self.assertIn("state.json is damaged", joined)
            for key, _title in self.env.g.App.NAV:  # every page still opens
                app.show_page(key)
                pump(app, 0.4)
            self.assertEqual(self.env.errors, "")
            # nothing was overwritten
            self.assertEqual(core.CONFIG_PATH.read_text(encoding="utf-8"), "{ broken")
            self.assertEqual((self.env.tmp / "state.json").read_text(encoding="utf-8"), "{ broken too")
        finally:
            app.destroy()

    def test_damaged_config_repaired_from_backup(self):
        core.write_json_atomic(core.CONFIG_PATH, self.env.cfg)      # creates a valid .bak on the next write
        core.write_json_atomic(core.CONFIG_PATH, self.env.cfg)
        core.CONFIG_PATH.write_text("{ broken", encoding="utf-8")
        app = self.env.g.App()
        try:
            pump(app, 2.0)
            self.assertIsNotNone(app.config_data)  # restored automatically from config.json.bak
            self.assertEqual(self.env.errors, "")
        finally:
            app.destroy()

    def test_missing_config_created_from_template(self):
        example = self.env.tmp / "config.example.json"
        shutil.copy(core.CONFIG_PATH, example)
        core.CONFIG_PATH.unlink()
        saved = core.CONFIG_EXAMPLE_PATH
        core.CONFIG_EXAMPLE_PATH = example
        try:
            app = self.env.g.App()
            try:
                pump(app, 2.0)
                self.assertTrue(core.CONFIG_PATH.exists())
                self.assertIsNotNone(app.config_data)
                self.assertTrue(any("Created config.json" in m for m in self.msgs))
            finally:
                app.destroy()
        finally:
            core.CONFIG_EXAMPLE_PATH = saved

    def test_missing_config_and_no_template(self):
        core.CONFIG_PATH.unlink()
        saved = core.CONFIG_EXAMPLE_PATH
        core.CONFIG_EXAMPLE_PATH = self.env.tmp / "nope.json"
        try:
            app = self.env.g.App()
            try:
                pump(app, 2.0)
                self.assertIsNone(app.config_data)
                self.assertTrue(any("Missing config.json" in m for m in self.msgs))
                app.show_page("settings")
                pump(app, 0.5)
                self.assertEqual(self.env.errors, "")
            finally:
                app.destroy()
        finally:
            core.CONFIG_EXAMPLE_PATH = saved


if __name__ == "__main__":
    unittest.main()
