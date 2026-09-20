"""The app's sync behavior with a fake Google account. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_gui_sync -v"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

import dominos_schedule as core
import sync
from tests.fakes import FakeGoogleAccount
from tests.gui_env import Env, find_widgets, pump


class GuiSyncTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().install()
        self.acct = FakeGoogleAccount()
        self.env.cfg["sync_enabled"] = True
        self.env.cfg_path.write_text(json.dumps(self.env.cfg, indent=2), encoding="utf-8")
        g = self.env.g
        self._orig = g.App._sync_service
        g.App._sync_service = lambda app: self.acct
        self.apps = []
        self.other = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.env.g.App._sync_service = self._orig
        for app in self.apps:
            try:
                app.destroy()
            except Exception:
                pass
        self.env.uninstall()
        self.other.cleanup()

    def start(self, seconds=3.0):
        app = self.env.g.App()
        self.apps.append(app)
        pump(app, seconds)
        return app

    def other_computer(self):
        d = Path(self.other.name)
        return sync.LocalFiles(config=d / "config.json", state=d / "state.json", prefs=d / "gui_prefs.json",
                               meta=d / "sync_state.json", backups=d / "backups"), d

    def shared_doc(self):
        cal = app_cal(self.acct)
        tag = sync.parse_head(self.acct.events_in(cal, "head")[0]["description"])["tag"]
        chunks = [c for c in self.acct.events_in(cal, "chunk") if c["extendedProperties"]["private"]["sm_tag"] == tag]
        chunks.sort(key=lambda e: int(e["extendedProperties"]["private"]["sm_idx"]))
        return sync.decode_doc("".join(c["description"] for c in chunks))

    def test_opening_the_app_uploads_and_says_so(self):
        app = self.start()
        self.assertEqual(self.env.errors, "")
        self.assertIn("Last synced", app.sync_text)
        self.assertEqual(self.acct.calls.count("calendars.insert"), 1)
        self.assertEqual(self.shared_doc()["config"]["job_wages"], self.env.cfg["job_wages"])

    def test_saving_state_syncs_by_itself(self):
        app = self.start()
        app.store.log("quick_add", title="Study", event_ids=["z"])
        app.store.save()
        pump(app, 4.5)
        self.assertEqual(self.env.errors, "")
        actions = [e["action"] for e in self.shared_doc()["state"]["history"]]
        self.assertIn("quick_add", actions)

    def test_several_quick_saves_become_one_sync(self):
        app = self.start()
        before = len(self.acct.events_in(app_cal(self.acct), "head"))
        heads_etag = self.acct.events_in(app_cal(self.acct), "head")[0]["etag"]
        for i in range(5):
            app.store.log("quick_add", title=f"t{i}", event_ids=[str(i)])
            app.store.save()
            pump(app, 0.2)
        pump(app, 4.5)
        self.assertEqual(before, 1)
        rev = sync.parse_head(self.acct.events_in(app_cal(self.acct), "head")[0]["description"])["rev"]
        self.assertEqual(rev, 2)   # the first upload plus one combined update
        self.assertNotEqual(self.acct.events_in(app_cal(self.acct), "head")[0]["etag"], heads_etag)

    def test_changes_from_another_computer_appear_without_restarting(self):
        app = self.start()
        files, d = self.other_computer()
        (d / "config.json").write_text(json.dumps(self.env.cfg), encoding="utf-8")
        sync.sync_once(self.acct, files, now=datetime.now(timezone.utc))   # the other computer joins first
        other_cfg = dict(self.env.cfg, job_wages={"Dominos": 21.5, "Staples": 17.2}, weekly_hours_goal=30)
        (d / "config.json").write_text(json.dumps(other_cfg), encoding="utf-8")
        week = {"email_id": "m", "week_start": "2026-10-05", "week_end": "2026-10-11", "imported_at": "2026-09-19T10:00:00",
                "calendar_id": "primary", "event_ids": ["x1"], "shift_keys": ["k1"]}
        (d / "state.json").write_text(json.dumps({"weeks": {"2026-10-05": week}, "imported_shift_keys": ["k1"], "history": [
            {"action": "import", "timestamp": "2026-09-19T20:00:00", "week": "2026-10-05", "event_ids": ["x1"]}]}), encoding="utf-8")
        (d / "gui_prefs.json").write_text(json.dumps({"plan_title": "Read"}), encoding="utf-8")
        sync.sync_once(self.acct, files, now=datetime.now(timezone.utc))
        app.sync_now(manual=True)
        pump(app, 3.0)
        self.assertEqual(self.env.errors, "")
        self.assertIn("2026-10-05", app.store.data["weeks"])
        self.assertEqual(app.config_data["weekly_hours_goal"], 30)
        self.assertEqual(app.config_data["job_wages"]["Dominos"], 21.5)
        self.assertEqual(app.prefs["plan_title"], "Read")
        self.assertEqual(json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))["sync_enabled"], True)
        for key, _title in self.env.g.App.NAV:
            app.show_page(key)
            pump(app, 0.3)
        self.assertEqual(self.env.errors, "")

    def test_offline_is_quiet(self):
        app = self.start(2.0)
        self.acct.offline = True
        app.sync_now()
        pump(app, 2.0)
        self.assertIn("Offline", app.sync_text)
        self.assertEqual(self.env.errors, "")
        self.acct.offline = False
        app.sync_now(manual=True)
        pump(app, 2.0)
        self.assertIn("Last synced", app.sync_text)

    def test_can_be_turned_off_in_settings(self):
        app = self.start()
        app.show_page("settings")
        pump(app, 0.5)
        page = app.pages["settings"]
        page.sync_switch.deselect()
        page._toggle_sync()
        self.assertFalse(json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))["sync_enabled"])
        self.assertIn("off", page.sync_lbl.cget("text"))
        calls = len(self.acct.calls)
        rev_before = sync.parse_head(self.acct.events_in(app_cal(self.acct), "head")[0]["description"])["rev"]
        app.store.log("quick_add", title="x", event_ids=["q"])
        app.store.save()
        app.sync_now()
        pump(app, 4.0)
        rev_after = sync.parse_head(self.acct.events_in(app_cal(self.acct), "head")[0]["description"])["rev"]
        self.assertEqual(rev_before, rev_after)
        self.assertEqual(calls, len(self.acct.calls))

    def test_settings_page_has_the_sync_section(self):
        app = self.start(2.0)
        app.show_page("settings")
        pump(app, 0.8)
        texts = [getattr(w, "_text", "") for w in find_widgets(app.pages["settings"], ctk.CTkButton)]
        self.assertIn("Sync now", texts)
        self.assertEqual(self.env.errors, "")

    def test_saving_settings_syncs_them(self):
        app = self.start()
        app.show_page("settings")
        pump(app, 0.5)
        page = app.pages["settings"]
        page.vars["display_name"].delete(0, "end")
        page.vars["display_name"].insert(0, "Changed Name")
        page.save()
        pump(app, 4.5)
        self.assertEqual(self.shared_doc()["config"]["display_name"], "Changed Name")

    def test_a_sync_problem_does_not_break_the_app(self):
        self.acct.calendars_by_id["cal1"] = {"id": "cal1", "summary": sync.CALENDAR_NAME}
        self.acct.events_by_cal["cal1"] = {}
        self.acct.events_by_cal["cal1"]["h"] = {"id": "h", "etag": '"1"', "created": "2026-01-01T00:00:00Z", "description": "garbage",
                                                "extendedProperties": {"private": {"sm_kind": "head"}}}
        app = self.start(3.0)
        self.assertIn("Sync problem", app.sync_text)
        for key, _title in self.env.g.App.NAV:
            app.show_page(key)
            pump(app, 0.2)
        self.assertEqual(self.env.errors, "")


def app_cal(acct):
    return next(c["id"] for c in acct.calendars_by_id.values() if c["summary"] == sync.CALENDAR_NAME)


if __name__ == "__main__":
    unittest.main()
