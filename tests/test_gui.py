"""GUI tests: the real app against fake Google services. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_gui -v"""
import contextlib
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk

import dominos_schedule as core
from tests.gui_env import Env, find_widgets, pump


@contextlib.contextmanager
def auto_modal(g, button_text, before=None):
    """Make every dialog click `button_text` immediately instead of waiting for a person."""
    orig = g.Modal.show

    def show(self):
        if before:
            before(self)
        matches = [b for b in find_widgets(self.body, ctk.CTkButton) if b._text == button_text]
        matches[0].invoke()
    g.Modal.show = show
    try:
        yield
    finally:
        g.Modal.show = orig


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env().install()
        e = cls.env
        # "old-week" is already imported, "new-week" is not
        results = core.find_schedule_emails(e.gmail, e.cfg, 10)
        state = core.StateStore(core.STATE_PATH)
        old = next(p for p in results if p.email_id == "old-week")
        core.perform_import(e.gmail, e.cal, e.cfg, state, old, send_report=False, log=lambda *_: None)
        e.gmail.sent.clear()
        cls.g = e.g
        cls.app = e.g.App()
        pump(cls.app, 2.5)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        finally:
            cls.env.uninstall()

    def pump(self, s=1.0):
        pump(self.app, s)

    def no_errors(self):
        self.assertEqual(self.env.errors, "", "exceptions were logged:\n" + self.env.errors)

    def test_01_every_page_opens_cleanly(self):
        for key, _title in self.g.App.NAV:
            self.app.show_page(key)
            self.pump(1.0)
            self.assertTrue(self.app.pages[key].winfo_ismapped(), key)
        self.no_errors()

    def test_02_dashboard(self):
        d = self.app.pages["dashboard"]
        self.app.show_page("dashboard")
        d.refresh(check_email=True)
        self.pump(2.5)
        pay = d.stat["pay"].cget("text")
        self.assertRegex(pay, r"^\$\d[\d,]*\.\d\d$")
        self.assertTrue(d.stat["hours"].cget("text").endswith("h"))
        self.assertEqual(self.app.account_lbl.cget("text"), "test@example.com")
        banner = find_widgets(d.banner_slot, ctk.CTkButton, "Review & import")
        self.assertEqual(len(banner), 1)  # the not-yet-imported week
        d.seg.set("Next week")
        d._set_range("Next week")
        self.assertNotEqual(d.stat["pay"].cget("text"), pay)
        d.seg.set("This week")
        d._set_range("This week")
        self.assertEqual(d.stat["pay"].cget("text"), pay)
        self.no_errors()

    def test_03_week_select_and_delete_event(self):
        wp = self.app.pages["week"]
        self.app.show_page("week")
        self.pump(1.5)
        target = next(e for e in wp.report["events"] if e["summary"] == "Dentist")
        wp.on_select(target)
        self.assertIn(target["id"], self.env.cal.events_by_id)
        with auto_modal(self.g, "Delete"):
            wp.delete_selected()
            self.pump(1.5)
        self.assertNotIn(target["id"], self.env.cal.events_by_id)
        # the toast offers Undo, which puts the event back
        undo = find_widgets(self.app._toast, ctk.CTkButton, "Undo")
        self.assertEqual(len(undo), 1)
        undo[0].invoke()
        self.pump(2.0)
        restored = [e for e in self.env.cal.events_by_id.values() if e["summary"] == "Dentist"]
        self.assertEqual(len(restored), 1)
        self.no_errors()

    def test_04_quick_add(self):
        before = len(self.env.cal.events_by_id)

        def fill(m):
            entries = find_widgets(m.body, ctk.CTkEntry)
            entries[0].insert(0, "Study")
            for e, v in zip(entries[1:], ["today", "6pm", "7:30 PM"]):
                e.delete(0, "end")
                e.insert(0, v)
        with auto_modal(self.g, "Add event", before=fill):
            self.app.quick_add()
            self.pump(1.5)
        self.assertEqual(len(self.env.cal.events_by_id), before + 1)
        new = [e for e in self.env.cal.events_by_id.values() if e["summary"] == "Study"][0]
        self.assertEqual([o["minutes"] for o in new["reminders"]["overrides"]], [60, 30])
        self.assertIn("T18:00:00", new["start"]["dateTime"])
        self.no_errors()

    def test_05_quick_add_rejects_bad_input(self):
        before = len(self.env.cal.events_by_id)

        def fill(m):
            entries = find_widgets(m.body, ctk.CTkEntry)
            entries[0].insert(0, "Oops")
            entries[2].delete(0, "end")
            entries[2].insert(0, "banana")
            m.close = lambda result=None: setattr(m, "result", result)  # keep it open to inspect
        with auto_modal(self.g, "Add event", before=fill):
            self.app.quick_add()
            self.pump(0.5)
        self.assertEqual(len(self.env.cal.events_by_id), before)

    def test_06_import_flow(self):
        ip = self.app.pages["import"]
        ip.results = []
        self.app.show_page("import")
        self.pump(3.0)
        self.assertEqual([p.email_id for p in ip.results], ["new-week", "old-week"])  # noise ignored
        statuses = [ip.tree.set(k, "status") for k in ip.tree.get_children()]
        self.assertEqual(statuses, ["New", "Imported"])
        self.assertEqual(ip.selected().email_id, "new-week")  # first actionable one is preselected
        preview = ip.preview.get("1.0", "end")
        self.assertIn("What will happen", preview)
        self.assertIn("+ 4 new shift(s)", preview)
        self.assertRegex(preview, r"OVERLAP")  # the seeded Staples event collides with Monday's shift
        tags = set(ip.preview._textbox.tag_names())
        self.assertTrue({"c_" + self.g.C["success"].lstrip("#"), "c_" + self.g.C["danger"].lstrip("#")} <= tags)
        n_before = len(self.env.cal.events_by_id)
        with auto_modal(self.g, "Import"):
            ip.do_import()
            self.pump(3.0)
        self.assertEqual(len(self.env.cal.events_by_id), n_before + 4)
        self.assertEqual(len(self.env.gmail.sent), 1)
        self.assertIn("Weekly Schedule", self.env.gmail.sent[0]["subject"])
        state = core.StateStore(core.STATE_PATH)
        self.assertEqual(len(state.week_entry((self.env.mon + timedelta(days=14)).isoformat())["event_ids"]), 4)
        self.assertEqual([ip.tree.set(k, "status") for k in ip.tree.get_children()], ["Imported", "Imported"])
        # importing again changes nothing
        with auto_modal(self.g, "Import"):
            ip.tree.selection_set("0")
            self.pump(1.5)
            ip.do_import()
            self.pump(2.5)
        self.assertEqual(len(self.env.cal.events_by_id), n_before + 4)
        self.no_errors()

    def test_07_manage_delete_week(self):
        mp = self.app.pages["manage"]
        self.app.show_page("manage")
        self.pump(0.8)
        keys = list(mp.tree.get_children())
        self.assertGreaterEqual(len(keys), 1)
        target = (self.env.mon + timedelta(days=7)).isoformat()
        self.assertIn(target, keys)
        ids = list(self.app.store.week_entry(target)["event_ids"])
        mp.tree.selection_set(target)
        with auto_modal(self.g, "Delete"):
            mp.delete()
            self.pump(2.0)
        self.assertTrue(all(i not in self.env.cal.events_by_id for i in ids))
        self.assertNotIn(target, list(mp.tree.get_children()))
        self.no_errors()

    def test_08_jobs_dialog_saves_config(self):
        def edit(m):
            entries = find_widgets(m.body, ctk.CTkEntry)
            entries[4].delete(0, "end")
            entries[4].insert(0, "19.25")
        with auto_modal(self.g, "Save", before=edit):
            self.app.edit_jobs()
            self.pump(2.0)
        saved = json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["job_wages"]["Staples"], 19.25)
        self.assertEqual(saved["job_wages"]["Dominos"], 17.58)
        self.assertEqual(saved["display_name"], "Tester")  # untouched keys survive
        self.assertEqual(self.app.config_data["job_wages"]["Staples"], 19.25)
        self.no_errors()

    def test_09_settings_roundtrip_and_validation(self):
        sp = self.app.pages["settings"]
        self.app.show_page("settings")
        self.pump(1.0)
        before = json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))
        sp.save()
        after = json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(before, after)
        sp.vars["close"].delete(0, "end")
        sp.vars["close"].insert(0, "abc")
        sp.save()
        self.assertEqual(json.loads(core.CONFIG_PATH.read_text(encoding="utf-8")), before)
        self.no_errors()

    def test_10_damaged_config_is_not_overwritten(self):
        good = core.CONFIG_PATH.read_text(encoding="utf-8")
        bak = core.CONFIG_PATH.with_suffix(".json.bak")
        bak.unlink(missing_ok=True)
        core.CONFIG_PATH.write_text("{ not json", encoding="utf-8")
        try:
            sp = self.app.pages["settings"]
            self.assertIsNone(self.app.load_raw_config())
            sp.save()  # must refuse rather than wipe the file
            self.assertEqual(core.CONFIG_PATH.read_text(encoding="utf-8"), "{ not json")
        finally:
            core.CONFIG_PATH.write_text(good, encoding="utf-8")

    def test_11_trackpad_scroll(self):
        sp = self.app.pages["settings"]
        self.app.show_page("settings")
        self.pump(1.0)
        canvas = sp.scroll._parent_canvas
        child = sp.vars["gmail_query"]
        canvas.yview_moveto(0)
        for _ in range(4):
            child.event_generate("<TouchpadScroll>", x=10, y=10, delta=((-30) & 0xFFFF))
            self.app.update()
        self.pump(0.6)
        self.assertGreater(canvas.yview()[0], 0.05)
        self.assertIsNone(self.g.App.touch_target(self.app.pages["manage"].tree))

    def test_11b_offline_mode(self):
        d = self.app.pages["dashboard"]
        self.app.show_page("dashboard")
        d.refresh()
        self.pump(2.0)  # online: fills the saved-data cache
        self.assertFalse(self.app.offline_lbl.winfo_ismapped())
        live_pay = d.stat["pay"].cget("text")
        self.env.cal.offline = self.env.gmail.offline = True
        try:
            d.refresh(check_email=True)
            self.pump(2.5)
            self.assertTrue(self.app.offline_lbl.winfo_ismapped())
            self.assertIn("Offline", self.app.offline_lbl.cget("text"))
            self.assertEqual(d.stat["pay"].cget("text"), live_pay)  # same numbers from the saved copy
            # a stale report must not be emailed
            rp = self.app.pages["report"]
            self.app.show_page("report")
            rp.preview()
            self.pump(2.0)
            sent = len(self.env.gmail.sent)
            with auto_modal(self.g, "Send"):
                rp.send()
                self.pump(1.0)
            self.assertEqual(len(self.env.gmail.sent), sent)
            # the import page still lists the saved emails
            ip = self.app.pages["import"]
            ip.results = []
            ip.find()
            self.pump(2.5)
            self.assertEqual(len(ip.results), 2)
            self.assertTrue(all(p.from_cache for p in ip.results))
            ip.analysis.clear()
            ip.on_select(None)
            self.pump(2.0)
            self.assertIn("saved calendar data", ip.preview.get("1.0", "end"))
        finally:
            self.env.cal.offline = self.env.gmail.offline = False
        d.refresh()
        self.app.show_page("dashboard")
        self.pump(2.5)
        self.assertFalse(self.app.offline_lbl.winfo_ismapped())
        self.no_errors()

    def test_11c_month_view(self):
        mp = self.app.pages["month"]
        self.app.show_page("month")
        self.pump(2.5)
        gv = mp.grid_view
        self.assertGreater(len(gv.paid_hours), 3)
        self.assertRegex(mp.sub_lbl.cget("text"), r"h worked")
        self.assertIn(date.today().strftime("%B %Y"), mp.title_lbl.cget("text"))
        # clicking the middle of a cell resolves to that date and jumps to its week
        start, weeks, head, cw, ch = gv._layout()
        target = date.today()
        idx = (target - start).days
        x, y = (idx % 7) * cw + cw / 2, head + (idx // 7) * ch + ch / 2
        self.assertEqual(gv.date_at(x, y), target)
        self.assertIsNone(gv.date_at(x, head - 5))
        self.assertIsNone(gv.date_at(-5, y))
        gv.on_pick(gv.date_at(x, y))
        self.pump(1.5)
        self.assertEqual(self.app.current, "week")
        self.assertEqual(self.app.pages["week"].week_start, target - timedelta(days=target.weekday()))
        # navigation
        self.app.show_page("month")
        self.pump(1.5)
        before = mp.month
        mp.shift(1)
        self.pump(1.5)
        self.assertEqual((mp.month.year * 12 + mp.month.month), before.year * 12 + before.month + 1)
        mp.shift(-1)
        mp.go_today()
        self.pump(1.0)
        self.assertEqual(mp.month, date.today().replace(day=1))
        self.no_errors()

    def test_11d_search_dialog(self):
        picked = {}

        def drive(m):
            dlg = m.dialog
            dlg.entry.insert(0, "dentist")
            dlg.search_now()
            pump(self.app, 1.5)
            picked["rows"] = len(dlg.tree.get_children())
            picked["first"] = dlg.tree.set(dlg.tree.get_children()[0], "title") if dlg.tree.get_children() else None
            dlg.open_selected()
        orig = self.g.Modal.show
        self.g.Modal.show = drive
        try:
            self.app.show_page("dashboard")
            self.app.search()
            self.pump(1.5)
        finally:
            self.g.Modal.show = orig
        self.assertGreaterEqual(picked["rows"], 1)
        self.assertEqual(picked["first"].lower(), "dentist")
        self.assertEqual(self.app.current, "week")

        def none(m):
            dlg = m.dialog
            dlg.entry.insert(0, "zzzz-not-a-thing")
            dlg.search_now()
            pump(self.app, 1.5)
            picked["status"] = dlg.status.cget("text")
            m.close(None)
        self.g.Modal.show = none
        try:
            self.app.search()
        finally:
            self.g.Modal.show = orig
        self.assertEqual(picked["status"], "No matches.")
        self.no_errors()

    def test_11e_command_palette(self):
        seen = {}

        def drive(m):
            dlg = m.dialog
            seen["all"] = len(dlg.tree.get_children())
            dlg.entry.insert(0, "go rep")
            dlg._fill(dlg.entry.get())
            seen["filtered"] = [c[0] for c in dlg.shown]
            dlg.run_selected()
        orig = self.g.Modal.show
        self.g.Modal.show = drive
        try:
            self.app.show_page("dashboard")
            self.app.palette()
            self.pump(1.0)
        finally:
            self.g.Modal.show = orig
        self.assertGreaterEqual(seen["all"], 10)
        self.assertEqual(seen["filtered"], ["Go to Report"])
        self.assertEqual(self.app.current, "report")
        self.no_errors()

    def test_11f_earnings_modes_and_tax(self):
        ep = self.app.pages["earnings"]
        self.app.show_page("earnings")
        self.pump(2.5)
        self.assertEqual(len(ep.rows), 8)
        self.assertEqual(ep.vals["net"].cget("text"), "Not set")
        self.assertIn("WEEK", ep.cards["avg"].title_lbl.cget("text"))
        ep.seg.set("6 months")
        ep._pick("6 months")
        self.pump(2.5)
        self.assertEqual(len(ep.rows), 6)
        self.assertTrue(ep.rows[-1]["current"])
        self.assertIn("MONTH", ep.cards["avg"].title_lbl.cget("text"))
        self.assertGreater(len(ep.chart.find_all()), 20)
        self.app.config_data["tax_rate_percent"] = 20
        ep._summary()
        self.assertTrue(ep.vals["net"].cget("text").startswith("$"))
        self.assertIn("20%", ep.cards["net"].title_lbl.cget("text"))
        self.app.config_data["tax_rate_percent"] = 0
        ep.seg.set("8 weeks")
        ep._pick("8 weeks")
        self.pump(2.0)
        self.assertEqual(len(ep.rows), 8)
        self.no_errors()

    def test_11g_export_ics_and_csv(self):
        from unittest import mock
        import tempfile
        wp = self.app.pages["week"]
        self.app.show_page("week")
        self.pump(2.0)
        out = Path(tempfile.mkdtemp())
        with mock.patch.object(self.g.filedialog, "asksaveasfilename", return_value=str(out / "w.ics")):
            wp.export_ics()
        with mock.patch.object(self.g.filedialog, "asksaveasfilename", return_value=str(out / "w.csv")):
            wp.export()
        ics = (out / "w.ics").read_text(encoding="utf-8")
        self.assertGreaterEqual(ics.count("BEGIN:VEVENT"), 5)
        self.assertIn("SUMMARY:Dominos", ics)
        self.assertGreaterEqual(len((out / "w.csv").read_text(encoding="utf-8").splitlines()), 6)
        # cancelling the file dialog does nothing
        with mock.patch.object(self.g.filedialog, "asksaveasfilename", return_value=""):
            wp.export_ics()
        self.no_errors()

    def test_13_manage_undo_and_reset(self):
        mp = self.app.pages["manage"]
        e = self.env
        # make sure exactly one week is tracked, then undo it
        state = core.StateStore(core.STATE_PATH)
        core.perform_reset(e.cal, state, log=lambda *_: None)
        results = core.find_schedule_emails(e.gmail, e.cfg, 10)
        new = next(p for p in results if p.email_id == "new-week")
        core.perform_import(e.gmail, e.cal, e.cfg, state, new, send_report=False, log=lambda *_: None)
        self.app.show_page("manage")
        self.pump(1.0)
        self.assertEqual(len(mp.tree.get_children()), 1)
        n = len(e.cal.events_by_id)
        with auto_modal(self.g, "Undo"):
            mp.undo()
            self.pump(2.0)
        self.assertEqual(len(e.cal.events_by_id), n - 4)
        self.assertEqual(len(mp.tree.get_children()), 0)
        self.assertTrue(mp.tree_frame.empty.winfo_ismapped())  # friendly empty state
        with auto_modal(self.g, "Undo"):
            mp.undo()  # nothing left: must not crash
            self.pump(0.5)
        # reset requires typing RESET
        state = core.StateStore(core.STATE_PATH)
        core.perform_import(e.gmail, e.cal, e.cfg, state, new, send_report=False, log=lambda *_: None)
        self.app.show_page("manage")
        self.pump(1.0)
        n = len(e.cal.events_by_id)

        def wrong(m):
            find_widgets(m.body, ctk.CTkEntry)[0].insert(0, "nope")
            m.close = lambda result=None: setattr(m, "result", result)
        with auto_modal(self.g, "Reset", before=wrong):
            mp.reset()
            self.pump(1.0)
        self.assertEqual(len(e.cal.events_by_id), n)  # wrong confirmation text: nothing happened

        def right(m):
            find_widgets(m.body, ctk.CTkEntry)[0].insert(0, "RESET")
        with auto_modal(self.g, "Reset", before=right):
            mp.reset()
            self.pump(2.0)
        self.assertEqual(len(e.cal.events_by_id), n - 4)
        self.no_errors()

    def test_14_settings_actions(self):
        sp = self.app.pages["settings"]
        self.app.show_page("settings")
        self.pump(1.0)
        sp.check()
        self.pump(2.0)
        self.assertIn("test@example.com", sp.conn_lbl.cget("text"))
        self.assertIn("Test Calendar", sp.conn_lbl.cget("text"))
        sp.backup()
        self.assertTrue(list(core.BACKUP_DIR.glob("manual-*.zip")))
        # reminders: patches upcoming imported shifts
        state = core.StateStore(core.STATE_PATH)
        core.perform_reset(self.env.cal, state, log=lambda *_: None)
        results = core.find_schedule_emails(self.env.gmail, self.env.cfg, 10)
        core.perform_import(self.env.gmail, self.env.cal, self.env.cfg, state,
                            next(p for p in results if p.email_id == "new-week"), send_report=False, log=lambda *_: None)
        self.app.reload_store()
        self.app.config_data["reminder_minutes_before"] = [90, 15]
        with auto_modal(self.g, "Update"):
            sp.apply_reminders()
            self.pump(2.0)
        tracked = self.app.store.week_entry((self.env.mon + timedelta(days=14)).isoformat())["event_ids"]
        self.assertEqual([o["minutes"] for o in self.env.cal.events_by_id[tracked[0]]["reminders"]["overrides"]], [90, 15])
        self.app.config_data["reminder_minutes_before"] = [60, 30]
        # signing out removes the saved login
        core.TOKEN_PATH.write_text("{}", encoding="utf-8")
        with auto_modal(self.g, "Reset"):
            sp.reset_login()
        self.assertFalse(core.TOKEN_PATH.exists())
        self.no_errors()

    def test_15_dashboard_banner_and_fix_button(self):
        d = self.app.pages["dashboard"]
        e = self.env
        # an unclassified long event makes the dashboard offer to fix pay
        e.cal.add_raw("Big Shift", datetime.combine(date.today(), datetime.min.time()).replace(hour=8),
                      datetime.combine(date.today(), datetime.min.time()).replace(hour=14))
        self.app.show_page("dashboard")
        d.refresh(check_email=True)
        self.pump(2.5)
        self.assertTrue(d.pay_fix.winfo_ismapped())
        self.assertIn("Big Shift", d.pay_fix.cget("text"))
        opened = []
        orig = self.g.JobsDialog.run
        self.g.JobsDialog.run = lambda self_: opened.append(True)
        try:
            d.pay_fix.invoke()
        finally:
            self.g.JobsDialog.run = orig
        self.assertEqual(opened, [True])
        # the banner jumps to the import page with the new week selected
        banner = find_widgets(d.banner_slot, ctk.CTkButton, "Review & import")
        if banner:
            banner[0].invoke()
            self.pump(1.5)
            self.assertEqual(self.app.current, "import")
            emails = core.find_schedule_emails(self.env.gmail, self.env.cfg, 10)
            expected = next(p.email_id for p in emails if core.schedule_status(self.app.store, p) in ("new", "changed"))
            self.assertEqual(self.app.pages["import"].selected().email_id, expected)
        self.no_errors()

    def test_16_help_page(self):
        hp = self.app.pages["help"]
        self.app.show_page("help")
        self.pump(1.0)
        text = hp.diagnostics()
        self.assertIn(f"Schedule Manager v{core.APP_VERSION}", text)
        self.assertIn("Tracked weeks:", text)
        hp.copy_diagnostics()
        self.assertIn("Python", self.app.clipboard_get())
        self.no_errors()

    def test_17_hours_goal_and_rest_settings(self):
        d = self.app.pages["dashboard"]
        self.app.config_data["weekly_hours_goal"] = 5
        self.app.show_page("dashboard")
        d.refresh()
        self.pump(2.5)
        self.assertIn("over your 5h goal", d.hours_note.cget("text"))
        self.app.config_data["weekly_hours_goal"] = 500
        d._render_stats()
        self.assertEqual(d.hours_note.cget("text"), "Goal 500h")
        self.app.config_data["weekly_hours_goal"] = 0
        d._render_stats()
        self.assertEqual(d.hours_note.cget("text"), "")
        sp = self.app.pages["settings"]
        self.app.show_page("settings")
        self.pump(1.0)
        sp.vars["min_rest_hours"].delete(0, "end")
        sp.vars["min_rest_hours"].insert(0, "9.5")
        sp.vars["weekly_hours_goal"].delete(0, "end")
        sp.vars["weekly_hours_goal"].insert(0, "28")
        sp.save()
        saved = json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual((saved["min_rest_hours"], saved["weekly_hours_goal"]), (9.5, 28))
        sp.vars["min_rest_hours"].delete(0, "end")
        sp.vars["min_rest_hours"].insert(0, "-3")
        sp.save()
        self.assertEqual(json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))["min_rest_hours"], 9.5)  # rejected
        sp.vars["min_rest_hours"].delete(0, "end")
        sp.vars["min_rest_hours"].insert(0, "8")
        sp.vars["weekly_hours_goal"].delete(0, "end")
        sp.vars["weekly_hours_goal"].insert(0, "0")
        sp.save()
        self.no_errors()

    def test_18_state_jobs_are_serialized_and_reload_keeps_identity(self):
        import threading
        store = self.app.store
        started, release = threading.Event(), threading.Event()
        done, second = [], []

        def slow():
            started.set()
            release.wait(6)
            return 1

        self.app.run_job([], "slow", slow, lambda r: done.append(r), exclusive=True)
        self.assertTrue(started.wait(3))
        before = store.data
        self.app.reload_store()                      # must not touch the store while a writer is running
        self.assertIs(self.app.store, store)
        self.assertIs(store.data, before)
        self.app.run_job([], "second", lambda: second.append("ran") or 2, lambda r: None, exclusive=True)
        self.pump(0.6)
        self.assertEqual(second, [])                 # blocked until the first job finishes
        release.set()
        self.pump(2.0)
        self.assertEqual((done, second), ([1], ["ran"]))
        self.app.reload_store()                      # now allowed, and keeps the same object
        self.assertIs(self.app.store, store)
        self.no_errors()

    def test_19_dashboard_keeps_itself_fresh(self):
        import time
        d = self.app.pages["dashboard"]
        self.app.show_page("dashboard")
        d.refresh()
        self.pump(2.5)
        calls = []
        orig = self.env.cal.list
        self.env.cal.list = lambda **kw: (calls.append(1), orig(**kw))[1]
        try:
            d._maybe_auto_refresh()                       # just refreshed: nothing to do
            self.pump(1.0)
            self.assertEqual(calls, [])
            d.last_refresh = time.time() - 700            # stale: refreshes quietly by itself
            d._maybe_auto_refresh()
            self.pump(2.5)
            self.assertGreaterEqual(len(calls), 2)
            self.assertLess(time.time() - d.last_refresh, 10)
            # navigating back after a while refreshes too
            n = len(calls)
            d.last_refresh = time.time() - 300
            self.app.show_page("week")
            self.pump(0.5)
            self.app.show_page("dashboard")
            self.pump(2.5)
            self.assertGreater(len(calls), n)
            # failures during a background refresh are silent and retried soon
            self.env.cal.offline = True
            self.env.cal.events_by_id_backup = None
            toasts = []
            orig_toast = self.app.toast
            self.app.toast = lambda text, *a, **k: toasts.append(text)
            try:
                d.last_refresh = time.time() - 700
                d._maybe_auto_refresh()
                self.pump(2.5)
            finally:
                self.app.toast = orig_toast
                self.env.cal.offline = False
            self.assertFalse([t for t in toasts if "Can't reach" in t])
        finally:
            self.env.cal.list = orig
        # the countdown timer chain is not multiplied by refreshes
        before = len(self.app.tk.eval("after info").split())
        for _ in range(3):
            d._update_hero()
        self.assertEqual(len(self.app.tk.eval("after info").split()), before)
        self.no_errors()

    def test_19a_today_timeline(self):
        d = self.app.pages["dashboard"]
        self.app.show_page("dashboard")
        d.refresh()
        self.pump(2.5)
        tl = d.timeline
        today_timed = [e for e in tl.events]
        self.assertGreaterEqual(len(today_timed), 1)
        self.assertTrue(all(e["day"] == date.today() for e in today_timed))
        # one colored block + one label per event, plus the axis; the now-marker is drawn when today is shown
        fills = [tl.canvas.itemcget(i, "fill") for i in tl.canvas.find_all() if tl.canvas.type(i) == "polygon"]
        self.assertEqual(len(fills), len(today_timed))
        self.assertIn(self.g.C["danger"], [tl.canvas.itemcget(i, "fill") for i in tl.canvas.find_all()])
        # x positions grow with time
        lo, hi = tl.hour_range()
        a = tl.x_for(datetime.combine(date.today(), datetime.min.time()).replace(hour=9), 0, 1000, lo, hi)
        b = tl.x_for(datetime.combine(date.today(), datetime.min.time()).replace(hour=15), 0, 1000, lo, hi)
        self.assertLess(a, b)
        tl.set_events([], date.today())
        texts = [tl.canvas.itemcget(i, "text") for i in tl.canvas.find_all() if tl.canvas.type(i) == "text"]
        self.assertIn("Nothing scheduled today", texts)
        d.refresh()
        self.pump(2.0)
        self.no_errors()

    def test_19b_hero_moves_to_next_event_when_one_ends(self):
        from datetime import datetime as dt
        d = self.app.pages["dashboard"]
        now = dt.now()
        mk = lambda title, a, b: {"summary": title, "start": now + timedelta(minutes=a), "end": now + timedelta(minutes=b),
                                  "day": (now + timedelta(minutes=a)).date(), "all_day": False, "category": "Other"}
        d.future = [mk("Past", -120, -60), mk("Current", -10, 50), mk("Later", 90, 150)]
        d._update_hero()
        self.assertEqual(d.hero_title.cget("text"), "Current")
        self.assertEqual(d.hero_count.cget("text"), "Happening now")
        self.assertIn("Later", d.hero_then.cget("text"))
        d.future = [mk("Past", -120, -60)]
        d._update_hero()
        self.assertEqual(d.hero_title.cget("text"), "Nothing coming up")

    def test_20_single_instance_guard(self):
        name = "ScheduleManager.test.%d" % id(self)
        first, h1 = self.g.acquire_single_instance(name)
        second, h2 = self.g.acquire_single_instance(name)
        try:
            self.assertTrue(first)
            self.assertFalse(second)
        finally:
            import ctypes
            for h in (h1, h2):
                if h:
                    ctypes.windll.kernel32.CloseHandle(h)
        self.assertFalse(self.g.focus_existing_window("No such window title xyz"))

    def _drag(self, gv, x1, y1, x2, y2):
        """Real Tk mouse events: press at (x1,y1), drag to (x2,y2), release. Coordinates are canvas coordinates."""
        c = gv.canvas
        ox, oy = c.canvasx(0), c.canvasy(0)

        def gen(seq, x, y, state=0):
            c.event_generate(seq, x=int(x - ox), y=int(y - oy), state=state)
            self.app.update()
        gen("<ButtonPress-1>", x1, y1)
        gen("<B1-Motion>", (x1 + x2) / 2, (y1 + y2) / 2, 256)
        gen("<B1-Motion>", x2, y2, 256)
        gen("<ButtonRelease-1>", x2, y2)

    def _block(self, gv, title, weekday=None):
        for ex1, y1, ex2, y2, ev in gv._blocks:
            if ev["summary"] == title and (weekday is None or ev["day"].weekday() == weekday):
                return ex1, y1, ex2, y2, ev
        self.fail(f"no block for {title}")

    def _block_by_category(self, gv, category, weekday=None):
        for ex1, y1, ex2, y2, ev in gv._blocks:
            if ev["category"] == category and (weekday is None or ev["day"].weekday() == weekday):
                return ex1, y1, ex2, y2, ev
        self.fail(f"no block for category {category}")

    def _open_week(self, weeks_ahead=0):
        wp = self.app.pages["week"]
        wp.week_start = self.env.mon + timedelta(weeks=weeks_ahead)
        self.app.show_page("week")
        wp.load(force=True)
        self.pump(2.0)
        return wp, wp.grid_view

    def _start_of(self, event_id):
        return self.env.cal.events_by_id[event_id]["start"]["dateTime"]

    def test_21_drag_to_move_with_undo(self):
        wp, gv = self._open_week()
        ex1, y1, ex2, y2, ev = self._block(gv, "CIS 110", weekday=2)  # Wed 5:00-6:50 PM
        col_w, hour_h = gv._geo["col_w"], gv._geo["hour_h"]
        before = self._start_of(ev["id"])
        cx = (ex1 + ex2) / 2
        self._drag(gv, cx, y1 + 12, cx + col_w, y1 + 12 + 2 * hour_h)  # next day, two hours later
        self.pump(2.0)
        moved = self._start_of(ev["id"])
        self.assertIn(f"{(ev['day'] + timedelta(days=1)).isoformat()}T19:00:00", moved)
        self.assertIn("T20:50:00", self.env.cal.events_by_id[ev["id"]]["end"]["dateTime"])
        undo = find_widgets(self.app._toast, ctk.CTkButton, "Undo")
        self.assertEqual(len(undo), 1)
        undo[0].invoke()
        self.pump(2.0)
        self.assertEqual(self._start_of(ev["id"]), before)
        self.no_errors()

    def test_22_drag_bottom_edge_to_resize(self):
        wp, gv = self._open_week()
        ex1, y1, ex2, y2, ev = self._block(gv, "Staples", weekday=0)  # Mon 3:30-8:30 PM
        hour_h = gv._geo["hour_h"]
        cx = (ex1 + ex2) / 2
        self._drag(gv, cx, y2 - 3, cx, y2 - 3 + hour_h)
        self.pump(2.0)
        stored = self.env.cal.events_by_id[ev["id"]]
        self.assertIn("T15:30:00", stored["start"]["dateTime"])   # start untouched
        self.assertIn("T21:30:00", stored["end"]["dateTime"])     # end one hour later
        find_widgets(self.app._toast, ctk.CTkButton, "Undo")[0].invoke()
        self.pump(2.0)
        self.assertIn("T20:30:00", self.env.cal.events_by_id[ev["id"]]["end"]["dateTime"])
        self.no_errors()

    def test_23_drag_on_empty_space_creates_event(self):
        wp, gv = self._open_week()
        g = gv._geo
        # find a free 90-minute window somewhere this week (other tests leave events around)
        day = slot = None
        for d, evs in gv.days.items():
            free = core.find_free_slots(evs, d, 9, 21, 90)
            if free:
                day, slot = d, free[0]
                break
        self.assertIsNotNone(day, "no free slot found")
        start = slot[0]
        end = start + timedelta(minutes=90)
        col = (day - gv.week_start).days
        cx = g["gutter"] + col * g["col_w"] + g["col_w"] / 2
        y_at = lambda dt: ((dt.hour * 60 + dt.minute) / 60 - g["lo"]) * g["hour_h"]
        seen = {}

        def check(m):
            entries = find_widgets(m.body, ctk.CTkEntry)
            seen["day"], seen["start"], seen["end"] = entries[1].get(), entries[2].get(), entries[3].get()
            entries[0].insert(0, "Dragged event")
        before = len(self.env.cal.events_by_id)
        with auto_modal(self.g, "Add event", before=check):
            self._drag(gv, cx, y_at(start), cx, y_at(end))
            self.pump(2.5)
        self.assertEqual((seen["start"], seen["end"]), (core.fmt_time(start), core.fmt_time(end)))
        self.assertEqual(seen["day"], core.fmt_date(day))
        self.assertEqual(len(self.env.cal.events_by_id), before + 1)
        new = [e for e in self.env.cal.events_by_id.values() if e["summary"] == "Dragged event"][0]
        self.assertIn(f"{day.isoformat()}T{start:%H:%M}:00", new["start"]["dateTime"])
        self.assertIn(f"T{end:%H:%M}:00", new["end"]["dateTime"])
        self.no_errors()

    def test_24_a_plain_click_never_changes_anything(self):
        wp, gv = self._open_week()
        if self.app._toast is not None and self.app._toast.winfo_exists():
            self.app._toast.destroy()  # leftovers (like an Undo toast) from earlier tests
        ex1, y1, ex2, y2, ev = self._block(gv, "CIS 111")
        before = dict(self.env.cal.events_by_id[ev["id"]]["start"])
        n = len(self.env.cal.events_by_id)
        cx = (ex1 + ex2) / 2
        self._drag(gv, cx, y1 + 12, cx + 2, y1 + 14)   # under the drag threshold = a click
        self.pump(1.0)
        self.assertEqual(self.env.cal.events_by_id[ev["id"]]["start"], before)
        self.assertEqual(len(self.env.cal.events_by_id), n)
        self.assertEqual(wp.selected["id"], ev["id"])   # ...but it selects the event
        self.assertEqual(find_widgets(self.app._toast, ctk.CTkButton, "Undo") if self.app._toast and self.app._toast.winfo_exists() else [], [])

    def test_25_moving_an_imported_shift_asks_first(self):
        state = core.StateStore(core.STATE_PATH)
        core.perform_reset(self.env.cal, state, log=lambda *_: None)
        results = core.find_schedule_emails(self.env.gmail, self.env.cfg, 10)
        old = next(p for p in results if p.email_id == "old-week")
        core.perform_import(self.env.gmail, self.env.cal, self.env.cfg, state, old, send_report=False, log=lambda *_: None)
        self.app.reload_store()
        wp, gv = self._open_week(weeks_ahead=1)
        ex1, y1, ex2, y2, ev = self._block_by_category(gv, "Dominos")
        before = self._start_of(ev["id"])
        cx = (ex1 + ex2) / 2
        hour_h = gv._geo["hour_h"]
        with auto_modal(self.g, "Cancel"):
            self._drag(gv, cx, y1 + 12, cx, y1 + 12 + hour_h)
            self.pump(1.5)
        self.assertEqual(self._start_of(ev["id"]), before)          # declined: unchanged
        wp, gv = self._open_week(weeks_ahead=1)
        ex1, y1, ex2, y2, ev = self._block_by_category(gv, "Dominos")
        with auto_modal(self.g, "Move"):
            self._drag(gv, (ex1 + ex2) / 2, y1 + 12, (ex1 + ex2) / 2, y1 + 12 + hour_h)
            self.pump(2.0)
        self.assertNotEqual(self._start_of(ev["id"]), before)       # confirmed: moved
        self.no_errors()

    def test_26_pay_schedule_per_job(self):
        ep = self.app.pages["earnings"]
        self.app.show_page("earnings")
        self.pump(1.5)
        ep._pick("Pay periods")                       # nothing configured yet: politely refuses
        self.assertEqual(ep.mode[0], "weeks")
        sp = self.app.pages["settings"]
        self.app.show_page("settings")
        self.pump(1.0)
        rows = {r["name"].get(): r for r in sp.job_rows}
        start = self.env.mon - timedelta(days=7)
        rows["Dominos"]["pay_type"].set("biweekly")
        rows["Dominos"]["pay_start"].insert(0, core.fmt_date(start))
        rows["Dominos"]["pay_delay"].delete(0, "end")
        rows["Dominos"]["pay_delay"].insert(0, "5")
        rows["Staples"]["pay_type"].set("weekly")
        rows["Staples"]["pay_start"].insert(0, core.fmt_date(self.env.mon))
        sp.save()
        saved = json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["job_pay"]["Dominos"], {"type": "biweekly", "start": start.isoformat(), "delay_days": 5})
        self.assertEqual(saved["job_pay"]["Staples"]["type"], "weekly")
        self.assertEqual(saved["pay_schedule"]["type"], "off")
        self.app.show_page("earnings")
        ep.seg.set("Pay periods")
        ep._pick("Pay periods")
        self.pump(2.5)
        self.assertEqual((ep.period_job, len(ep.rows)), ("Dominos", 6))
        self.assertEqual((ep.rows[-1]["week_end"] - ep.rows[-1]["week_start"]).days, 13)
        ep._pick_job("Staples")
        self.pump(2.5)
        self.assertEqual((ep.rows[-1]["week_end"] - ep.rows[-1]["week_start"]).days, 6)
        self.assertTrue(all(set(r["category_hours"]) <= {"Staples"} for r in ep.rows))
        d = self.app.pages["dashboard"]
        self.app.show_page("dashboard")
        d.refresh()
        self.pump(2.5)
        text = d.pay_break.cget("text")
        self.assertIn("Dominos payday", text)
        self.assertIn("Staples payday", text)
        # a job paid biweekly needs a date, and nothing is saved when it is missing
        self.app.show_page("settings")
        self.pump(0.8)
        rows = {r["name"].get(): r for r in sp.job_rows}
        rows["Dominos"]["pay_start"].delete(0, "end")
        before = core.CONFIG_PATH.read_text(encoding="utf-8")
        sp.save()
        self.assertEqual(core.CONFIG_PATH.read_text(encoding="utf-8"), before)
        rows["Dominos"]["pay_type"].set("off")
        sp.save()
        self.assertEqual(self.app.config_data["job_pay"]["Dominos"]["type"], "off")
        self.assertEqual(core.scheduled_jobs(self.app.config_data), ["Staples"])
        self.app.show_page("earnings")
        ep.seg.set("8 weeks")
        ep._pick("8 weeks")
        self.pump(2.0)
        self.no_errors()

    def test_27_study_planner(self):
        pp = self.app.pages["plan"]
        self.app.show_page("plan")
        self.pump(2.5)
        self.assertGreater(len(pp.plan), 0)
        self.assertLessEqual(sum(b["hours"] for b in pp.plan), 8.01)
        self.assertEqual(len(pp.tree.selection()), len(pp.plan))
        self.assertRegex(pp.summary.cget("text"), r"of 8 hours planned")
        # every suggestion is clear of the calendar's events by at least 30 minutes
        cal_events = [core.normalize_event(e, core.ZoneInfo("America/Los_Angeles"), self.env.cfg, "primary")
                      for e in self.env.cal.events_by_id.values()]
        for b in pp.plan:
            for e in cal_events:
                if e["all_day"] or e["day"] != b["day"]:
                    continue
                self.assertTrue(b["end"] + timedelta(minutes=29) <= e["start"] or b["start"] >= e["end"] + timedelta(minutes=29))
        # add only the first two blocks
        pp.title_entry.delete(0, "end")
        pp.title_entry.insert(0, "Study block")
        pp.tree.selection_set(*pp.tree.get_children()[:2])
        chosen = [pp.plan[0], pp.plan[1]]
        before = len(self.env.cal.events_by_id)
        pp.add_selected()
        self.pump(3.0)
        self.assertEqual(len(self.env.cal.events_by_id), before + 2)
        made = [e for e in self.env.cal.events_by_id.values() if e["summary"] == "Study block"]
        self.assertEqual(len(made), 2)
        self.assertEqual(sorted(e["start"]["dateTime"][:16] for e in made),
                         sorted(b["start"].strftime("%Y-%m-%dT%H:%M") for b in chosen))
        # a new plan avoids the blocks just added
        self.assertTrue(all(not (b["start"] < c["end"] and b["end"] > c["start"] and b["day"] == c["day"]) for b in pp.plan for c in chosen))
        # bad input is rejected
        pp.goal.delete(0, "end")
        pp.goal.insert(0, "lots")
        pp.suggest()
        self.pump(0.5)
        pp.goal.delete(0, "end")
        pp.goal.insert(0, "8")
        pp.seg.set("Next week")
        pp.suggest()
        self.pump(2.5)
        self.assertGreater(len(pp.plan), 0)
        self.assertTrue(all(core.next_week_bounds()[0] <= b["day"] <= core.next_week_bounds()[1] for b in pp.plan))
        self.no_errors()

    def test_28_html_preview_and_email_setting(self):
        from unittest import mock
        rp = self.app.pages["report"]
        self.app.show_page("report")
        rp.preview()
        self.pump(2.0)
        with mock.patch.object(self.g.webbrowser, "open") as opened:
            rp.open_html()
        self.assertTrue(opened.called)
        html = core.CACHE_DIR.joinpath("report-preview.html").read_text(encoding="utf-8")
        self.assertIn("WEEKLY SCHEDULE", html)
        with auto_modal(self.g, "Send"):
            n = len(self.env.gmail.sent)
            rp.send()
            self.pump(2.0)
        self.assertIn("text/html", self.env.gmail.sent[n]["raw"])
        # switching styled email off sends plain text only
        sp = self.app.pages["settings"]
        self.app.show_page("settings")
        self.pump(0.8)
        sp.email_html.deselect()
        sp.save()
        self.assertFalse(self.app.config_data["email_html"])
        self.app.show_page("report")
        rp.preview()
        self.pump(2.0)
        with auto_modal(self.g, "Send"):
            rp.send()
            self.pump(2.0)
        self.assertNotIn("text/html", self.env.gmail.sent[n + 1]["raw"])
        self.app.show_page("settings")
        self.pump(0.8)
        sp.email_html.select()
        sp.save()
        self.assertTrue(self.app.config_data["email_html"])
        self.no_errors()

    def test_29_new_layout_has_no_theme_picker_or_history(self):
        g = self.g
        self.assertEqual([k for k, _t in g.App.NAV], ["home", "dashboard", "week", "month", "plan", "earnings", "import",
                                                       "manage", "report", "settings", "help"])
        self.assertFalse(hasattr(g, "ACCENTS"))
        self.assertFalse(hasattr(self.app, "set_accent"))
        self.assertNotIn("history", self.app.nav_buttons)
        with self.assertRaises(KeyError):
            self.app.pages["history"]
        self.app.show_page("settings")
        self.pump(0.8)
        sp = self.app.pages["settings"]
        texts = " ".join(str(getattr(w, "_text", "")) for w in find_widgets(sp, ctk.CTkLabel))
        self.assertNotIn("Appearance", texts)
        self.assertNotIn("accent", texts.lower())
        self.assertEqual(list(sp.tab_frames), ["General", "Jobs & pay", "Alerts", "Tablet", "Data"])
        self.no_errors()

    def test_30_pages_build_once_and_settings_only_rebuild_when_config_changes(self):
        self.app.show_page("settings")
        self.pump(0.6)
        sp = self.app.pages["settings"]
        first = sp.vars["gmail_query"]
        self.app.show_page("dashboard")
        self.app.show_page("settings")
        self.assertIs(sp.vars["gmail_query"], first)          # coming back doesn't rebuild the form
        sp.save()
        self.app.show_page("dashboard")
        self.app.show_page("settings")
        self.pump(0.4)
        self.assertIsNot(sp.vars["gmail_query"], first)       # saving changed the settings, so the form was refreshed
        self.no_errors()

    def test_31_pages_are_created_when_first_needed(self):
        pages = self.app.pages
        self.assertIsNone(pages.get("nonexistent"))
        self.assertIs(pages["week"], pages["week"])
        with self.assertRaises(KeyError):
            pages["nope"]

    def test_32_tab_switching_shows_one_group_at_a_time(self):
        self.app.show_page("settings")
        self.pump(0.5)
        sp = self.app.pages["settings"]
        for name in sp.TABS:
            sp._show_tab(name)
            self.pump(0.2)
            shown = [n for n, f in sp.tab_frames.items() if f.winfo_manager()]
            self.assertEqual(shown, [name])
        hp = self.app.pages["help"]
        self.app.show_page("help")
        self.pump(0.5)
        for name in hp.TABS:
            hp._show_tab(name)
            self.assertEqual([n for n, f in hp.tab_frames.items() if f.winfo_manager()], [name])
        sp._show_tab("General")
        self.no_errors()

    def test_33_window_opens_maximized_and_f11_is_fullscreen(self):
        import types
        calls = []
        fake = types.SimpleNamespace(wm_state=lambda state: calls.append(state))
        self.g.App._open_maximized(fake)
        self.assertEqual(calls, ["zoomed"])
        self.assertTrue(self.app.bind("<F11>"))

    def test_12_reports(self):
        self.assertRegex(self.g.fmt_stamp("2026-09-18T14:07:03"), r"^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d\d [AP]M$")
        self.assertEqual(self.g.fmt_stamp("garbage"), "garbage")
        rp = self.app.pages["report"]
        self.app.show_page("report")
        rp.preview()
        self.pump(2.0)
        body = rp.box.get("1.0", "end")
        self.assertIn("Weekly Schedule:", body)
        with auto_modal(self.g, "Send"):
            n = len(self.env.gmail.sent)
            rp.send()
            self.pump(2.0)
        self.assertEqual(len(self.env.gmail.sent), n + 1)
        self.no_errors()


if __name__ == "__main__":
    unittest.main()
