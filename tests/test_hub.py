"""The module registry. Run:  python -m unittest tests.test_hub -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
import hub


class HubTests(unittest.TestCase):
    def setUp(self):
        self.saved = list(hub._registry)
        hub._registry.clear()

    def tearDown(self):
        hub._registry[:] = self.saved

    def test_pages_and_sections_come_from_the_modules_in_order(self):
        hub.register(hub.Module("a", "A", pages=[hub.PageSpec("a1", "One", "x", "P"), hub.PageSpec("a2", "Two", "x", "P")]))
        hub.register(hub.Module("b", "B", section="TOOLS", pages=[hub.PageSpec("b1", "Three", "x", "P")]))
        self.assertEqual(hub.nav(), [("a1", "One"), ("a2", "Two"), ("b1", "Three")])
        self.assertEqual(hub.sections(), {"b1": "TOOLS"})

    def test_duplicates_are_refused(self):
        hub.register(hub.Module("a", "A", pages=[hub.PageSpec("p", "P", "x", "C")]))
        with self.assertRaises(ValueError):
            hub.register(hub.Module("a", "Again"))
        with self.assertRaises(ValueError):
            hub.register(hub.Module("c", "C", pages=[hub.PageSpec("p", "Clash", "x", "C")]))

    def test_module_folder_and_settings_are_separate_per_module(self):
        tmp = tempfile.TemporaryDirectory()
        saved, core.BASE_DIR = core.BASE_DIR, Path(tmp.name)
        try:
            folder = hub.module_dir("receipts")
            self.assertTrue(folder.is_dir())
            self.assertEqual(folder, Path(tmp.name) / "modules" / "receipts")
            cfg = {"job_wages": {}}
            hub.module_settings(cfg, "receipts")["folder"] = "D:/r"
            hub.module_settings(cfg, "storage")["host"] = "nas"
            self.assertEqual(cfg["modules"], {"receipts": {"folder": "D:/r"}, "storage": {"host": "nas"}})
            self.assertEqual(hub.module_settings(cfg, "receipts")["folder"], "D:/r")
        finally:
            core.BASE_DIR = saved
            tmp.cleanup()

    def test_the_real_app_registers_home_schedule_and_app(self):
        hub._registry[:] = self.saved
        import schedule_gui as g
        keys = [k for k, _t in g.App.NAV]
        self.assertEqual(keys[0], "home")
        self.assertEqual(keys[-2:], ["settings", "help"])
        self.assertEqual(set(g.App.PAGES), set(keys))
        self.assertTrue(all(k in g.NAV_ICONS for k in keys))


if __name__ == "__main__":
    unittest.main()
