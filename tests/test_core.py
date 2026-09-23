"""Core logic tests. Run:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v"""
import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
from tests.fakes import FakeCalendar, FakeGmail, make_config, make_message

SAMPLE = """JORDAN SMITH, Domino's store 1234 just published a work schedule for the week of 9/21/2026 to 9/27/2026. Your schedule is:

Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM
Thursday, 9/24/2026, Insider, 4:45 PM to 7:15 PM
Friday, 9/25/2026, Insider, 4:30 PM to 8:45 PM
Friday, 9/25/2026, Insider, 9:15 PM to 11:30 PM
Saturday, 9/26/2026, Insider, 5:00 PM to 8:45 PM
Sunday, 9/27/2026, Insider, 3:15 PM to 10:30 PM

If you have questions, please call the store:
(555) 010-0100

Thanks!
"""


class ParserTests(unittest.TestCase):
    def parse(self, body):
        return core.parse_schedule_email("m1", datetime(2026, 9, 18), body)

    def test_real_email(self):
        p = self.parse(SAMPLE)
        self.assertEqual(len(p.shifts), 6)
        self.assertEqual(p.total_hours, 23.25)
        self.assertEqual((p.week_start, p.week_end), (date(2026, 9, 21), date(2026, 9, 27)))
        self.assertEqual(p.employee_name, "JORDAN SMITH")
        self.assertEqual(p.store_number, "1234")
        self.assertEqual(p.phone, "(555) 010-0100")
        self.assertEqual(p.shifts[0].role, "Insider")

    def test_crlf_and_nbsp(self):
        body = SAMPLE.replace("\n", "\r\n").replace("4:00 PM", "4:00\xa0PM")
        self.assertEqual(len(self.parse(body).shifts), 6)

    def test_curly_apostrophe_and_mixed_case_name(self):
        body = SAMPLE.replace("Domino's", "Domino’s").replace("JORDAN SMITH", "Jordan Smith")
        p = self.parse(body)
        self.assertEqual(p.employee_name, "Jordan Smith")
        self.assertEqual(len(p.shifts), 6)

    def test_lenient_times(self):
        body = SAMPLE.split("Tuesday")[0] + "Tuesday, 9/22/2026, Insider, 4pm to 8:30pm\nWednesday, 9/23/2026, Manager, 9 AM - 5 PM.\n"
        p = self.parse(body)
        self.assertEqual([s.hours for s in p.shifts], [4.5, 8.0])
        self.assertEqual(p.shifts[1].role, "Manager")

    def test_overnight_shift(self):
        body = SAMPLE.split("Tuesday")[0] + "Friday, 9/25/2026, Insider, 8:00 PM to 12:30 AM\n"
        s = self.parse(body).shifts[0]
        self.assertEqual(s.end_dt, datetime(2026, 9, 26, 0, 30))
        self.assertEqual(s.hours, 4.5)

    def test_duplicate_shift_lines_collapse(self):
        body = SAMPLE.split("Tuesday")[0] + "Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM\n" * 2
        self.assertEqual(len(self.parse(body).shifts), 1)

    def test_multiword_role(self):
        body = SAMPLE.split("Tuesday")[0] + "Monday, 9/21/2026, Shift Leader, 4:00 PM to 9:00 PM\n"
        self.assertEqual(self.parse(body).shifts[0].role, "Shift Leader")

    def test_reworded_header_still_parses(self):
        body = SAMPLE.replace("JORDAN SMITH, Domino's store 1234 just published a work schedule for the week of",
                              "Hi Jordan! Your new schedule for the week of")
        p = self.parse(body)
        self.assertEqual((len(p.shifts), p.week_start, p.week_end), (6, date(2026, 9, 21), date(2026, 9, 27)))
        self.assertEqual((p.employee_name, p.store_number), ("", ""))

    def test_no_header_derives_week_from_shifts(self):
        body = "Your shifts:\n" + "\n".join(l for l in SAMPLE.splitlines() if l[:3] in ("Tue", "Thu", "Fri", "Sat", "Sun"))
        p = self.parse(body)
        self.assertEqual((len(p.shifts), p.week_start, p.week_end), (6, date(2026, 9, 21), date(2026, 9, 27)))

    def test_not_a_schedule(self):
        self.assertIsNone(self.parse("Hi, your pizza is ready!"))
        self.assertIsNone(self.parse(""))
        self.assertIsNone(self.parse(SAMPLE.split("Tuesday")[0]))  # header but no shifts

    def test_html_only_email(self):
        html = SAMPLE.replace("\n", "<br>\n").replace("Domino's", "Domino&#39;s")
        html = f"<html><body><p>{html}</p><style>x{{}}</style></body></html>"
        payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}
        bodies = core.get_text_candidates(payload)
        parsed = core.parse_schedule_email("m", datetime.now(), bodies[0])
        self.assertEqual(len(parsed.shifts), 6)

    def test_multipart_prefers_plain(self):
        enc = lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": enc("PLAIN " + SAMPLE)}},
            {"mimeType": "text/html", "body": {"data": enc("<p>HTML</p>")}}]}
        self.assertTrue(core.get_text_candidates(payload)[0].startswith("PLAIN"))

    def test_shift_key_and_dict_roundtrip(self):
        p = self.parse(SAMPLE)
        again = core.schedule_from_dict(json.loads(json.dumps(core.schedule_to_dict(p))))
        self.assertEqual([s.key for s in again.shifts], [s.key for s in p.shifts])
        self.assertEqual(again.total_hours, p.total_hours)


class ClassifyTests(unittest.TestCase):
    cfg = make_config()

    def test_jobs_and_school(self):
        c = lambda t: core.classify_event(t, self.cfg)
        self.assertEqual(c("Dominos"), "Dominos")
        self.assertEqual(c("STAPLES"), "Staples")
        self.assertEqual(c("Kent staples"), "Staples")
        self.assertEqual(c("Work"), "Staples")
        self.assertEqual(c("Homework due"), "Other")
        self.assertEqual(c("CIS 111"), "School")
        self.assertEqual(c("cis110 lab"), "School")
        self.assertEqual(c("Dentist"), "Other")
        self.assertEqual(c(""), "Other")
        self.assertEqual(c(None), "Other")

    def test_overrides(self):
        cfg = make_config(school_extra_titles=["Study Group"], school_exceptions=["Rm 204"])
        self.assertEqual(core.classify_event("study group", cfg), "School")
        self.assertEqual(core.classify_event("RM 204", cfg), "Other")


def ev(title, start, end, category=None, cfg=None):
    cfg = cfg or make_config()
    return {"id": title + str(start), "summary": title, "start": start, "end": end, "all_day": False,
            "day": start.date(), "category": category or core.classify_event(title, cfg), "calendar_id": "primary"}


class ConflictTests(unittest.TestCase):
    D = datetime(2026, 9, 22)

    def at(self, h, m=0):
        return self.D.replace(hour=h, minute=m)

    def conflicts(self, events, cfg=None):
        cfg = cfg or make_config()
        return core.find_all_conflicts(core.group_events_by_day(events, self.D.date(), self.D.date()), cfg)[self.D.date()]

    def test_overlap(self):
        c = self.conflicts([ev("Dominos", self.at(16), self.at(19)), ev("Staples", self.at(18), self.at(21))])
        self.assertEqual([x["type"] for x in c], ["overlap"])

    def test_very_close_and_close(self):
        a = ev("CIS 111", self.at(13), self.at(15))
        self.assertEqual(self.conflicts([a, ev("Staples", self.at(15, 30), self.at(20))])[0]["type"], "very_close")
        self.assertEqual(self.conflicts([a, ev("Staples", self.at(15, 45), self.at(20))])[0]["type"], "close")
        self.assertEqual(self.conflicts([a, ev("Staples", self.at(16, 30), self.at(20))]), [])

    def test_boundaries_inclusive(self):
        a = ev("CIS 111", self.at(13), self.at(15))
        self.assertEqual(self.conflicts([a, ev("Staples", self.at(15, 30), self.at(20))])[0]["gap"], 30)
        self.assertEqual(self.conflicts([a, ev("Staples", self.at(16), self.at(20))])[0]["type"], "close")

    def test_touching_events_are_very_close_not_overlap(self):
        c = self.conflicts([ev("CIS 111", self.at(13), self.at(15)), ev("Staples", self.at(15), self.at(20))])
        self.assertEqual((c[0]["type"], c[0]["gap"]), ("very_close", 0))

    def test_split_dominos_shift_is_ignored(self):
        c = self.conflicts([ev("Dominos", self.at(16, 30), self.at(20, 45)), ev("Dominos", self.at(21, 15), self.at(23, 30))])
        self.assertEqual(c, [])

    def test_split_staples_flagged_unless_configured(self):
        e = [ev("Staples", self.at(9), self.at(12)), ev("Staples", self.at(12, 30), self.at(16))]
        self.assertEqual(len(self.conflicts(e)), 1)
        self.assertEqual(self.conflicts(e, make_config(break_ok_categories=["Dominos", "Staples"])), [])

    def test_same_job_overlap_still_flagged(self):
        c = self.conflicts([ev("Dominos", self.at(16), self.at(20)), ev("Dominos", self.at(19), self.at(22))])
        self.assertEqual(c[0]["type"], "overlap")

    def test_all_day_ignored(self):
        allday = {"id": "x", "summary": "Essay due", "start": None, "end": None, "all_day": True, "day": self.D.date(),
                  "category": "Other", "calendar_id": "primary"}
        self.assertEqual(self.conflicts([allday, ev("Dominos", self.at(16), self.at(19))]), [])

    def test_custom_thresholds(self):
        cfg = make_config(conflict_thresholds_minutes={"very_close": 10, "close": 20})
        a = ev("CIS 111", self.at(13), self.at(15))
        self.assertEqual(self.conflicts([a, ev("Staples", self.at(15, 25), self.at(20))], cfg), [])


class RestTests(unittest.TestCase):
    def days(self, events, start=date(2026, 9, 21), n=7):
        return core.group_events_by_day(events, start, start + timedelta(days=n - 1))

    def issues(self, events, cfg=None):
        cfg = cfg or make_config()
        conflicts = core.find_all_conflicts(self.days(events), cfg)
        return [(d, c) for d, cs in conflicts.items() for c in cs if c["type"] == "short_rest"]

    def test_late_close_then_early_class_flagged(self):
        e = [ev("Dominos", datetime(2026, 9, 25, 21, 15), datetime(2026, 9, 25, 23, 30)),
             ev("CIS 111", datetime(2026, 9, 26, 7, 0), datetime(2026, 9, 26, 9, 0))]
        (d, c), = self.issues(e)
        self.assertEqual(d, date(2026, 9, 26))  # filed under the later day
        self.assertAlmostEqual(c["gap"], 450)
        text = core.describe_conflict(d, c)
        self.assertEqual(text, "SHORT REST (7.5h) Sat 9/26: Dominos ends 11:30 PM Fri, CIS 111 starts 7:00 AM")

    def test_enough_rest_not_flagged(self):
        e = [ev("Dominos", datetime(2026, 9, 25, 21, 15), datetime(2026, 9, 25, 23, 30)),
             ev("CIS 111", datetime(2026, 9, 26, 8, 0), datetime(2026, 9, 26, 9, 0))]  # exactly 8.5h
        self.assertEqual(self.issues(e), [])

    def test_boundary_and_setting(self):
        e = [ev("Dominos", datetime(2026, 9, 25, 22, 0), datetime(2026, 9, 25, 23, 0)),
             ev("CIS 111", datetime(2026, 9, 26, 7, 0), datetime(2026, 9, 26, 8, 0))]  # exactly 8h
        self.assertEqual(self.issues(e), [])
        self.assertEqual(len(self.issues(e, make_config(min_rest_hours=8.5))), 1)
        self.assertEqual(self.issues(e, make_config(min_rest_hours=0)), [])
        self.assertEqual(self.issues(e, make_config(min_rest_hours="abc")), [])

    def test_uses_last_and_first_events_and_skips_gaps(self):
        e = [ev("Staples", datetime(2026, 9, 22, 9), datetime(2026, 9, 22, 23, 0)),
             ev("Dentist", datetime(2026, 9, 22, 12), datetime(2026, 9, 22, 13)),     # ends earlier: irrelevant
             ev("CIS 111", datetime(2026, 9, 23, 6, 30), datetime(2026, 9, 23, 8)),
             ev("Dominos", datetime(2026, 9, 25, 7), datetime(2026, 9, 25, 9))]       # 24h+ after the last event
        found = self.issues(e)
        self.assertEqual([(d, c["a"]["summary"], c["b"]["summary"]) for d, c in found],
                         [(date(2026, 9, 23), "Staples", "CIS 111")])

    def test_all_day_and_overnight(self):
        allday = {"id": "x", "summary": "Trip", "start": None, "end": None, "all_day": True, "day": date(2026, 9, 26),
                  "category": "Other", "calendar_id": "primary"}
        e = [ev("Dominos", datetime(2026, 9, 25, 21), datetime(2026, 9, 25, 23, 30)), allday]
        self.assertEqual(self.issues(e), [])
        # an event that runs past midnight counts from its real end
        e = [ev("Dominos", datetime(2026, 9, 25, 21), datetime(2026, 9, 26, 1, 30)),
             ev("CIS 111", datetime(2026, 9, 26, 8, 0), datetime(2026, 9, 26, 9))]
        self.assertEqual(len(self.issues(e)), 1)

    def test_report_lists_short_rest_and_goal(self):
        cfg = make_config(weekly_hours_goal=5)
        e = [ev("Dominos", datetime(2026, 9, 25, 21, 15), datetime(2026, 9, 25, 23, 30)),
             ev("CIS 111", datetime(2026, 9, 26, 7, 0), datetime(2026, 9, 26, 9, 0)),
             ev("Staples", datetime(2026, 9, 24, 9), datetime(2026, 9, 24, 15))]
        days = self.days(e)
        _, body = core.build_weekly_report_email(date(2026, 9, 21), date(2026, 9, 27), days, core.find_all_conflicts(days, cfg),
                                                 core.compute_category_hours(e), cfg)
        self.assertIn("SHORT REST (7.5h) Sat 9/26", body)
        self.assertIn("Over your 5h goal by 3.25h", body)
        _, quiet = core.build_weekly_report_email(date(2026, 9, 21), date(2026, 9, 27), days, {}, core.compute_category_hours(e), make_config())
        self.assertNotIn("goal", quiet)


class HelperTests(unittest.TestCase):
    def test_free_slots(self):
        d = date(2026, 9, 22)
        evs = [ev("A", datetime(2026, 9, 22, 10), datetime(2026, 9, 22, 12)),
               ev("B", datetime(2026, 9, 22, 11), datetime(2026, 9, 22, 13))]
        slots = core.find_free_slots(evs, d, 8, 22, 60)
        self.assertEqual([(a.hour, b.hour) for a, b in slots], [(8, 10), (13, 22)])

    def test_free_slots_min_length(self):
        d = date(2026, 9, 22)
        evs = [ev("A", datetime(2026, 9, 22, 8, 30), datetime(2026, 9, 22, 21, 30))]
        self.assertEqual(core.find_free_slots(evs, d, 8, 22, 60), [])

    def test_reminders(self):
        f = lambda v: core.reminder_list({"reminder_minutes_before": v})
        self.assertEqual(f([60, 30]), [60, 30])
        self.assertEqual(f(30), [30])
        self.assertEqual(f("30, 60; 60"), [60, 30])
        self.assertEqual(f("abc"), [30])
        self.assertEqual(f([1, 2, 3, 4, 5, 6, 7]), [7, 6, 5, 4, 3])
        self.assertEqual(core.describe_reminders([60, 30]), "1 hour and 30 min")
        self.assertEqual(core.describe_reminders([120]), "2 hours")

    def test_fmt_range(self):
        d = datetime(2026, 9, 22)
        self.assertEqual(core.fmt_range(d.replace(hour=16), d.replace(hour=19, minute=15)), "4:00-7:15 PM")
        self.assertEqual(core.fmt_range(d.replace(hour=11), d.replace(hour=15)), "11:00 AM-3:00 PM")
        self.assertEqual(core.fmt_range(d.replace(hour=20), d + timedelta(days=1, minutes=30)), "8:00 PM-12:30 AM")

    def test_clock_and_date_parsing(self):
        p = lambda s: core.parse_clock(s)
        self.assertEqual((p("4pm").hour, p("4:30 PM").minute, p("16:00").hour, p("12am").hour, p("12 PM").hour),
                         (16, 30, 16, 0, 12))
        self.assertRaises(ValueError, p, "nope")
        t = date(2026, 9, 18)
        self.assertEqual(core.parse_date_flexible("9/22", t), date(2026, 9, 22))
        self.assertEqual(core.parse_date_flexible("today", t), t)
        self.assertEqual(core.parse_date_flexible("Tomorrow", t), date(2026, 9, 19))
        self.assertEqual(core.parse_date_flexible("9/22/26", t), date(2026, 9, 22))
        self.assertRaises(ValueError, core.parse_date_flexible, "31/31", t)

    def test_week_bounds(self):
        self.assertEqual(core.this_week_bounds(date(2026, 9, 18)), (date(2026, 9, 14), date(2026, 9, 20)))
        self.assertEqual(core.next_week_bounds(date(2026, 9, 20)), (date(2026, 9, 21), date(2026, 9, 27)))
        self.assertEqual(core.this_week_bounds(date(2026, 9, 21))[0], date(2026, 9, 21))

    def test_paid_totals(self):
        cfg = make_config()
        self.assertEqual(core.paid_totals({"Dominos": 2, "Staples": 1, "School": 5, "Other": 9}, cfg), (3, 2 * 17.58 + 17.2))

    def test_normalize_event_timezones(self):
        cfg = make_config()
        tz = ZoneInfo("America/Los_Angeles")
        e = core.normalize_event({"id": "1", "summary": "Dominos", "start": {"dateTime": "2026-09-23T02:00:00Z"},
                                  "end": {"dateTime": "2026-09-23T05:15:00Z"}}, tz, cfg, "primary")
        self.assertEqual(e["start"], datetime(2026, 9, 22, 19, 0))
        self.assertEqual(e["day"], date(2026, 9, 22))

    def test_declined_events_skipped(self):
        self.assertTrue(core._declined_by_me({"attendees": [{"self": True, "responseStatus": "declined"}]}))
        self.assertFalse(core._declined_by_me({"attendees": [{"self": True, "responseStatus": "accepted"}]}))
        self.assertFalse(core._declined_by_me({}))


class FileSafetyTests(unittest.TestCase):
    def test_roundtrip_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            core.write_json_atomic(p, {"a": 1})
            core.write_json_atomic(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text()), {"a": 2})
            self.assertEqual(json.loads((Path(d) / "x.json.bak").read_text()), {"a": 1})
            self.assertFalse((Path(d) / "x.json.tmp").exists())

    def test_damaged_file_restored_from_bak(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            core.write_json_atomic(p, {"a": 1})
            core.write_json_atomic(p, {"a": 2})
            p.write_text("{ broken", encoding="utf-8")
            self.assertEqual(core.read_json_safe(p), {"a": 1})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})  # repaired on disk

    def test_damaged_without_bak_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text("nope", encoding="utf-8")
            self.assertRaises(core.DataFileError, core.read_json_safe, p)
            self.assertRaises(core.DataFileError, core.StateStore, p)

    def test_damaged_state_never_overwrites_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            core.write_json_atomic(p, {"good": True})
            core.write_json_atomic(p, {"good": True, "n": 2})
            p.write_text("{ broken", encoding="utf-8")
            core.write_json_atomic(p, {"fresh": 1})  # must not copy the broken file over .bak
            self.assertEqual(json.loads((Path(d) / "s.json.bak").read_text()), {"good": True})

    def test_state_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text("{}", encoding="utf-8")
            st = core.StateStore(p)
            self.assertEqual((st.data["weeks"], st.data["history"], st.data["imported_shift_keys"]), ({}, [], []))


class ImportFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = core.StateStore(Path(self.tmp.name) / "state.json")
        self.cfg = make_config()
        self.cal = FakeCalendar()
        self.gmail = FakeGmail()
        self.parsed = core.parse_schedule_email("m1", datetime(2026, 9, 18), SAMPLE)
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, **kw):
        kw.setdefault("send_report", False)
        return core.perform_import(self.gmail, self.cal, self.cfg, self.state, self.parsed, log=self.log.append, **kw)

    def test_normal_import(self):
        r = self.run_import()
        self.assertEqual(len(r["created_events"]), 6)
        self.assertEqual(len(self.cal.events_by_id), 6)
        entry = self.state.week_entry("2026-09-21")
        self.assertEqual(len(entry["event_ids"]), 6)
        self.assertEqual(len(entry["shift_keys"]), 6)
        self.assertAlmostEqual(entry["estimated_pay"], 23.25 * 17.58)
        # persisted to disk
        self.assertEqual(len(core.StateStore(self.state.path).data["imported_shift_keys"]), 6)
        ev0 = next(iter(self.cal.events_by_id.values()))
        self.assertEqual(ev0["summary"], core.event_title_for_shift(self.cfg, self.parsed.shifts[0].hours))
        self.assertEqual([o["minutes"] for o in ev0["reminders"]["overrides"]], [60, 30])

    def test_reimport_is_idempotent(self):
        self.run_import()
        r = self.run_import()
        self.assertEqual((len(r["created_events"]), r["skipped"]), (0, 6))
        self.assertEqual(len(self.cal.events_by_id), 6)

    def test_existing_calendar_events_not_duplicated(self):
        # events from an older script / manual entry, not tracked in state
        for s in self.parsed.shifts[:3]:
            self.cal.add_raw(core.event_title_for_shift(self.cfg, s.hours), s.start_dt, s.end_dt)
        r = self.run_import()
        self.assertEqual((len(r["created_events"]), r["on_calendar"]), (3, 3))
        self.assertEqual(len(self.cal.events_by_id), 6)

    def test_force_creates_duplicates(self):
        self.run_import()
        r = self.run_import(force=True)
        self.assertEqual(len(r["created_events"]), 6)
        self.assertEqual(len(self.cal.events_by_id), 12)

    def test_partial_failure_is_saved_and_reported(self):
        self.cal.fail_insert_after = 2
        with self.assertRaises(RuntimeError) as cm:
            self.run_import()
        self.assertIn("2 of 6", str(cm.exception))
        self.assertEqual(len(self.state.week_entry("2026-09-21")["event_ids"]), 2)
        self.assertEqual(len(core.StateStore(self.state.path).data["imported_shift_keys"]), 2)
        # retry finishes the remaining 4 without duplicating the first 2
        self.cal.fail_insert_after = None
        r = self.run_import()
        self.assertEqual((len(r["created_events"]), r["skipped"]), (4, 2))
        self.assertEqual(len(self.cal.events_by_id), 6)

    def test_email_failure_keeps_calendar_state(self):
        self.gmail.fail_send = True
        r = self.run_import(send_report=True)
        self.assertEqual(len(r["created_events"]), 6)
        self.assertIn("boom", r["email_error"])
        self.assertEqual(len(self.state.week_entry("2026-09-21")["event_ids"]), 6)

    def test_email_sent_and_recorded(self):
        r = self.run_import(send_report=True)
        self.assertIsNone(r["email_error"])
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.state.week_entry("2026-09-21")["summary_email_id"], "sent-1")
        self.assertIn("Weekly Schedule", self.gmail.sent[0]["subject"])

    def test_diff_reports_new_removed(self):
        self.run_import()
        d = core.diff_schedule(self.state, self.parsed)
        self.assertEqual((len(d["new"]), len(d["already"]), d["removed"]), (0, 6, []))
        changed = core.parse_schedule_email("m2", datetime(2026, 9, 19), SAMPLE.replace(
            "Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM", "Tuesday, 9/22/2026, Insider, 5:00 PM to 8:15 PM"))
        d = core.diff_schedule(self.state, changed)
        self.assertEqual((len(d["new"]), len(d["already"]), len(d["removed"])), (1, 5, 1))

    def test_updated_schedule_syncs_and_removes_stale(self):
        self.run_import()
        changed = core.parse_schedule_email("m2", datetime(2026, 9, 19), SAMPLE.replace(
            "Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM", "Tuesday, 9/22/2026, Insider, 5:00 PM to 8:15 PM"))
        self.parsed = changed
        r = self.run_import(remove_stale=True)
        self.assertEqual((len(r["created_events"]), r["removed"]), (1, 1))
        self.assertEqual(len(self.cal.events_by_id), 6)
        entry = self.state.week_entry("2026-09-21")
        self.assertEqual((len(entry["event_ids"]), len(entry["shift_keys"])), (6, 6))
        self.assertEqual(len(set(entry["shift_keys"])), 6)
        self.assertEqual(len(self.state.data["imported_shift_keys"]), 6)
        starts = sorted(e["start"]["dateTime"] for e in self.cal.events_by_id.values())
        self.assertTrue(any("T17:00:00" in x and "09-22" in x for x in starts))
        self.assertFalse(any("T16:00:00" in x and "09-22" in x for x in starts))

    def test_stale_not_removed_by_default(self):
        self.run_import()
        self.parsed = core.parse_schedule_email("m2", datetime(2026, 9, 19), SAMPLE.replace(
            "Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM", "Tuesday, 9/22/2026, Insider, 5:00 PM to 8:15 PM"))
        r = self.run_import()
        self.assertEqual(r["removed"], 0)
        self.assertEqual(len(self.cal.events_by_id), 7)

    def test_delete_week(self):
        self.run_import()
        self.assertEqual(core.perform_delete_week(self.cal, self.state, "2026-09-21", log=self.log.append), 6)
        self.assertEqual(len(self.cal.events_by_id), 0)
        self.assertEqual((self.state.data["weeks"], self.state.data["imported_shift_keys"]), ({}, []))
        self.assertIsNone(core.perform_delete_week(self.cal, self.state, "2026-09-21"))

    def test_delete_failure_keeps_tracking(self):
        self.run_import()
        victim = self.state.week_entry("2026-09-21")["event_ids"][0]
        self.cal.fail_delete_ids.add(victim)
        with self.assertRaises(RuntimeError):
            core.perform_delete_week(self.cal, self.state, "2026-09-21", log=self.log.append)
        entry = self.state.week_entry("2026-09-21")
        self.assertEqual(entry["event_ids"], [victim])
        self.assertEqual(len(entry["shift_keys"]), 1)
        self.assertEqual(len(self.cal.events_by_id), 1)
        self.cal.fail_delete_ids.clear()
        self.assertEqual(core.perform_delete_week(self.cal, self.state, "2026-09-21", log=self.log.append), 1)
        self.assertEqual(len(self.cal.events_by_id), 0)

    def test_deleting_already_gone_event_is_fine(self):
        self.run_import()
        for eid in list(self.cal.events_by_id)[:2]:
            del self.cal.events_by_id[eid]
        core.perform_delete_week(self.cal, self.state, "2026-09-21", log=self.log.append)
        self.assertEqual(self.state.data["weeks"], {})

    def test_undo_last_import(self):
        self.assertIsNone(core.perform_undo(self.cal, self.state))
        self.run_import()
        self.assertEqual(core.perform_undo(self.cal, self.state, log=self.log.append), ("2026-09-21", 6))
        self.assertEqual(len(self.cal.events_by_id), 0)
        self.assertIsNone(core.perform_undo(self.cal, self.state))

    def test_reset_everything(self):
        self.run_import()
        self.assertEqual(core.perform_reset(self.cal, self.state, log=self.log.append), (1, 6))
        self.assertEqual((self.state.data["weeks"], len(self.state.data["history"])), ({}, 1))
        self.assertEqual(len(self.cal.events_by_id), 0)

    def test_reset_failure_is_not_silent(self):
        self.run_import()
        self.cal.fail_delete_ids.add(self.state.week_entry("2026-09-21")["event_ids"][0])
        with self.assertRaises(RuntimeError):
            core.perform_reset(self.cal, self.state, log=self.log.append)
        self.assertEqual(len(self.state.data["weeks"]), 1)

    def test_forget_event(self):
        self.run_import()
        eid = self.state.week_entry("2026-09-21")["event_ids"][2]
        del self.cal.events_by_id[eid]  # the GUI deletes the calendar event first, then forgets it
        self.assertTrue(core.forget_event(self.state, eid))
        self.assertEqual(len(self.state.week_entry("2026-09-21")["shift_keys"]), 5)
        self.assertFalse(core.forget_event(self.state, "nope"))
        self.assertEqual(len(self.run_import()["created_events"]), 1)  # only the forgotten shift comes back

    def test_delete_with_snapshot_and_undo(self):
        self.run_import()
        entry = self.state.week_entry("2026-09-21")
        eid, key = entry["event_ids"][1], entry["shift_keys"][1]
        snap = core.delete_event_with_snapshot(self.cal, "primary", eid)
        self.assertEqual(snap["summary"], core.event_title_for_shift(self.cfg, self.parsed.shifts[1].hours))
        self.assertNotIn("id", snap)
        self.assertNotIn(eid, self.cal.events_by_id)
        info = core.forget_event(self.state, eid)
        self.assertEqual(info["key"], key)
        new = core.restore_event(self.cal, "primary", snap)
        core.restore_tracking(self.state, info, new["id"])
        entry = self.state.week_entry("2026-09-21")
        self.assertEqual((len(entry["event_ids"]), len(entry["shift_keys"])), (6, 6))
        self.assertIn(key, self.state.data["imported_shift_keys"])
        self.assertEqual(len(self.cal.events_by_id), 6)
        # restored event is a faithful copy
        restored = self.cal.events_by_id[new["id"]]
        self.assertEqual([o["minutes"] for o in restored["reminders"]["overrides"]], [60, 30])
        # nothing duplicated on the next import
        self.assertEqual(len(self.run_import()["created_events"]), 0)

    def test_undo_when_last_shift_removed_recreates_week(self):
        self.parsed = core.parse_schedule_email("m9", datetime(2026, 9, 19), SAMPLE.split("Wednesday")[0].split("Thursday")[0] + "Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM\n")
        self.run_import()
        entry = self.state.week_entry("2026-09-21")
        eid = entry["event_ids"][0]
        snap = core.delete_event_with_snapshot(self.cal, "primary", eid)
        info = core.forget_event(self.state, eid)
        self.assertIsNone(self.state.week_entry("2026-09-21"))
        core.restore_tracking(self.state, info, core.restore_event(self.cal, "primary", snap)["id"])
        self.assertEqual(len(self.state.week_entry("2026-09-21")["event_ids"]), 1)

    def test_snapshot_of_missing_event(self):
        self.assertIsNone(core.delete_event_with_snapshot(self.cal, "primary", "nope"))

    def test_apply_reminders(self):
        self.run_import()
        cfg = make_config(reminder_minutes_before=[120, 60, 30])
        # week is in the future relative to the fake "today"
        u, f = core.apply_reminders_to_imported(self.cal, cfg, self.state, today=date(2026, 9, 18))
        self.assertEqual((u, f), (6, 0))
        self.assertEqual([o["minutes"] for o in next(iter(self.cal.events_by_id.values()))["reminders"]["overrides"]], [120, 60, 30])
        self.assertEqual(core.apply_reminders_to_imported(self.cal, cfg, self.state, today=date(2026, 10, 30)), (0, 0))


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = core.StateStore(Path(self.tmp.name) / "state.json")
        self.cfg = make_config()
        self.cal = FakeCalendar()
        self.parsed = core.parse_schedule_email("m1", datetime(2026, 9, 18), SAMPLE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_transitions(self):
        self.assertEqual(core.schedule_status(self.state, self.parsed), "new")
        core.perform_import(FakeGmail(), self.cal, self.cfg, self.state, self.parsed, send_report=False, log=lambda *_: None)
        self.assertEqual(core.schedule_status(self.state, self.parsed), "imported")
        changed = core.parse_schedule_email("m2", datetime(2026, 9, 19), SAMPLE.replace("4:00 PM to 7:15 PM", "5:00 PM to 8:15 PM"))
        self.assertEqual(core.schedule_status(self.state, changed), "changed")
        self.parsed.superseded = True
        self.assertEqual(core.schedule_status(self.state, self.parsed), "older")

    def test_conflict_preview_with_existing_events(self):
        # class ends 45 minutes before Tuesday's 4:00 PM shift; Staples overlaps Thursday's shift
        self.cal.add_raw("CIS 111", datetime(2026, 9, 22, 13), datetime(2026, 9, 22, 15, 15))
        self.cal.add_raw("Staples", datetime(2026, 9, 24, 15), datetime(2026, 9, 24, 17))
        a = core.analyze_import(self.cal, self.cfg, self.state, self.parsed)
        self.assertEqual(len(a["diff"]["new"]), 6)
        kinds = sorted((c["type"], d.day) for d, c in a["conflicts"])
        self.assertEqual(kinds, [("close", 22), ("overlap", 24)])

    def test_unrelated_conflicts_not_reported(self):
        self.cal.add_raw("CIS 111", datetime(2026, 9, 22, 9), datetime(2026, 9, 22, 10))
        self.cal.add_raw("Dentist", datetime(2026, 9, 22, 9, 30), datetime(2026, 9, 22, 10, 30))  # overlaps, but not shift-related
        a = core.analyze_import(self.cal, self.cfg, self.state, self.parsed)
        self.assertEqual(a["conflicts"], [])

    def test_already_on_calendar_not_counted_as_new(self):
        for s in self.parsed.shifts[:2]:
            self.cal.add_raw(core.event_title_for_shift(self.cfg, s.hours), s.start_dt, s.end_dt)
        a = core.analyze_import(self.cal, self.cfg, self.state, self.parsed)
        self.assertEqual((len(a["diff"]["new"]), len(a["diff"]["already"])), (4, 2))


class EmailSearchTests(unittest.TestCase):
    def test_cache_and_superseded(self):
        with tempfile.TemporaryDirectory() as d:
            old_cache, old_dir = core.EMAIL_CACHE_PATH, core.CACHE_DIR
            core.CACHE_DIR, core.EMAIL_CACHE_PATH = Path(d), Path(d) / "emails.json"
            try:
                newer = SAMPLE.replace("Tuesday, 9/22/2026, Insider, 4:00 PM", "Tuesday, 9/22/2026, Insider, 5:00 PM")
                gmail = FakeGmail(messages=[make_message("a", SAMPLE, 1_000), make_message("b", newer, 2_000),
                                            make_message("c", "hello there", 3_000)])
                res = core.find_schedule_emails(gmail, make_config(), 10)
                self.assertEqual([p.email_id for p in res], ["b", "a"])
                self.assertEqual([p.superseded for p in res], [False, True])
                self.assertEqual(gmail.get_calls, 3)
                res2 = core.find_schedule_emails(gmail, make_config(), 10)
                self.assertEqual(gmail.get_calls, 3)  # served from cache, even the non-schedule one
                self.assertEqual([p.email_id for p in res2], ["b", "a"])
                self.assertEqual(res2[0].shifts[0].start_dt.hour, 17)
            finally:
                core.EMAIL_CACHE_PATH, core.CACHE_DIR = old_cache, old_dir


class ReportTests(unittest.TestCase):
    def build(self, events, week=date(2026, 9, 21)):
        cfg = make_config()
        days = core.group_events_by_day(events, week, week + timedelta(days=6))
        conflicts = core.find_all_conflicts(days, cfg)
        hours = core.compute_category_hours(events)
        return core.build_weekly_report_email(week, week + timedelta(days=6), days, conflicts, hours, cfg)

    def test_blank_line_after_each_day_and_total(self):
        d = datetime(2026, 9, 22)
        subject, body = self.build([ev("Dominos", d.replace(hour=16), d.replace(hour=19, minute=15)),
                                    ev("Staples", datetime(2026, 9, 21, 15, 30), datetime(2026, 9, 21, 20, 30))])
        lines = body.split("\n")
        day_idx = [i for i, l in enumerate(lines) if l[:3] in core.DAY_ABBR and l[3] == " "]
        self.assertEqual(len(day_idx), 7)
        self.assertTrue(all(lines[i + 1] == "" for i in day_idx))
        self.assertIn("Total: 8.25h", body)
        self.assertIn("No conflicts.", body)
        self.assertEqual(subject, "Weekly Schedule 9/21/2026-9/27/2026")

    def test_conflict_section(self):
        d = datetime(2026, 9, 22)
        _, body = self.build([ev("Dominos", d.replace(hour=16), d.replace(hour=19)), ev("Staples", d.replace(hour=18), d.replace(hour=21))])
        self.assertIn("CONFLICTS:", body)
        self.assertIn("OVERLAP Tue 9/22", body)

    def test_no_total_line_for_one_job(self):
        d = datetime(2026, 9, 22)
        _, body = self.build([ev("Dominos", d.replace(hour=16), d.replace(hour=19))])
        self.assertNotIn("Total:", body)


class BackupTests(unittest.TestCase):
    def test_backup_and_rotation(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            saved = (core.BASE_DIR, core.BACKUP_DIR)
            core.BASE_DIR, core.BACKUP_DIR = Path(d), Path(d) / "backups"
            try:
                (Path(d) / "config.json").write_text("{}", encoding="utf-8")
                (Path(d) / "state.json").write_text("{}", encoding="utf-8")
                (Path(d) / "token.json").write_text("SECRET", encoding="utf-8")
                first = core.auto_backup(keep=3, today=date(2026, 9, 1))
                self.assertEqual(sorted(zipfile.ZipFile(first).namelist()), ["config.json", "state.json"])  # no token!
                self.assertIsNone(core.auto_backup(keep=3, today=date(2026, 9, 1)))  # once per day
                for day in (2, 3, 4, 5):
                    core.auto_backup(keep=3, today=date(2026, 9, day))
                names = sorted(p.name for p in core.BACKUP_DIR.glob("auto-*.zip"))
                self.assertEqual(names, ["auto-20260903.zip", "auto-20260904.zip", "auto-20260905.zip"])
                manual = core.make_backup("manual")
                self.assertTrue(manual.exists())
                import time as _t
                os.utime(manual, (_t.time() + 60, _t.time() + 60))
                self.assertEqual(core.latest_backup(), manual)
            finally:
                core.BASE_DIR, core.BACKUP_DIR = saved


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (core.CACHE_DIR, core.EMAIL_CACHE_PATH, core.EVENTS_CACHE_PATH)
        core.CACHE_DIR = Path(self.tmp.name)
        core.EMAIL_CACHE_PATH = Path(self.tmp.name) / "emails.json"
        core.EVENTS_CACHE_PATH = Path(self.tmp.name) / "events.json"
        self.cfg = make_config()
        self.cal = FakeCalendar()
        self.cal.add_raw("Dominos", datetime(2026, 9, 22, 16), datetime(2026, 9, 22, 19, 15))
        self.cal.add_raw("Essay", datetime(2026, 9, 23), datetime(2026, 9, 24))

    def tearDown(self):
        core.CACHE_DIR, core.EMAIL_CACHE_PATH, core.EVENTS_CACHE_PATH = self.saved
        self.tmp.cleanup()

    WEEK = (date(2026, 9, 21), date(2026, 9, 27))

    def test_offline_falls_back_to_last_saved_week(self):
        live = core.generate_weekly_report(self.cal, self.cfg, *self.WEEK)
        self.assertIsNone(live["stale_at"])
        self.cal.offline = True
        stale = core.generate_weekly_report(self.cal, self.cfg, *self.WEEK)
        self.assertIsNotNone(stale["stale_at"])
        self.assertEqual(stale["body"], live["body"])
        self.assertEqual(stale["category_hours"], live["category_hours"])
        self.assertEqual([e["summary"] for e in stale["events"]], [e["summary"] for e in live["events"]])

    def test_analysis_works_offline_from_saved_calendar(self):
        state = core.StateStore(Path(self.tmp.name) / "state.json")
        parsed = core.parse_schedule_email("m1", datetime(2026, 9, 18), SAMPLE)
        core.analyze_import(self.cal, self.cfg, state, parsed)  # online run saves the week
        self.cal.offline = True
        a = core.analyze_import(self.cal, self.cfg, state, parsed)
        self.assertIsNotNone(a["stale_at"])
        self.assertEqual(len(a["diff"]["already"]) + len(a["diff"]["new"]), 6)

    def test_offline_without_cache_raises(self):
        self.cal.offline = True
        with self.assertRaises(OSError):
            core.generate_weekly_report(self.cal, self.cfg, *self.WEEK)

    def test_http_errors_are_not_treated_as_offline(self):
        core.generate_weekly_report(self.cal, self.cfg, *self.WEEK)

        class Boom(FakeCalendar):
            def list(self, **kw):
                from tests.fakes import Req, http_error
                return Req(lambda: (_ for _ in ()).throw(http_error(403)))
        with self.assertRaises(Exception) as cm:
            core.generate_weekly_report(Boom(), self.cfg, *self.WEEK)
        self.assertNotIsInstance(cm.exception, OSError)

    def test_stale_can_be_disabled(self):
        core.generate_weekly_report(self.cal, self.cfg, *self.WEEK)
        self.cal.offline = True
        with self.assertRaises(OSError):
            core.generate_weekly_report(self.cal, self.cfg, *self.WEEK, allow_stale=False)

    def test_emails_served_from_cache_when_offline(self):
        gmail = FakeGmail(messages=[make_message("a", SAMPLE, 1_000)])
        core.find_schedule_emails(gmail, self.cfg, 10)
        gmail.offline = True
        res = core.find_schedule_emails(gmail, self.cfg, 10)
        self.assertEqual([p.email_id for p in res], ["a"])
        self.assertTrue(res[0].from_cache)

    def test_emails_offline_without_cache_raises(self):
        gmail = FakeGmail(messages=[make_message("a", SAMPLE, 1_000)])
        gmail.offline = True
        with self.assertRaises(OSError):
            core.find_schedule_emails(gmail, self.cfg, 10)

    def test_network_error_detection(self):
        self.assertTrue(core.is_network_error(OSError("x")))
        self.assertTrue(core.is_network_error(ConnectionError("x")))
        self.assertTrue(core.is_network_error(RuntimeError("getaddrinfo failed")))
        self.assertFalse(core.is_network_error(ValueError("x")))
        from tests.fakes import http_error
        self.assertFalse(core.is_network_error(http_error(500)))


class CliTests(unittest.TestCase):
    """cmd_import / cmd_search against fake Google services, with prompts answered by the test."""

    def setUp(self):
        import argparse
        from unittest import mock
        self.mock = mock
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = make_config()
        (t / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        self.saved = {n: getattr(core, n) for n in ("CONFIG_PATH", "STATE_PATH", "CACHE_DIR", "EMAIL_CACHE_PATH", "EVENTS_CACHE_PATH", "build_services")}
        core.CONFIG_PATH, core.STATE_PATH = t / "config.json", t / "state.json"
        core.CACHE_DIR, core.EMAIL_CACHE_PATH, core.EVENTS_CACHE_PATH = t / "c", t / "c" / "e.json", t / "c" / "ev.json"
        self.cal, self.gmail = FakeCalendar(), FakeGmail(messages=[make_message("a", SAMPLE, 1_000)])
        core.build_services = lambda: (self.gmail, self.cal)
        self.ns = lambda **kw: argparse.Namespace(**{**dict(index=None, week=None, yes=True, dry_run=False, no_email=False,
                                                            force=False, limit=None, keep_dropped=False), **kw})

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(core, n, v)
        self.tmp.cleanup()

    def run_cli(self, fn, args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(args)
        return buf.getvalue()

    def test_import_dry_run_writes_nothing(self):
        out = self.run_cli(core.cmd_import, self.ns(dry_run=True))
        self.assertIn("What will happen: 6 new shift(s)", out)
        self.assertIn("[dry run]", out)
        self.assertEqual(len(self.cal.events_by_id), 0)
        self.assertFalse(core.STATE_PATH.exists())

    def test_import_then_reimport(self):
        out = self.run_cli(core.cmd_import, self.ns())
        self.assertIn("Done. 6 event(s) added", out)
        self.assertEqual(len(self.cal.events_by_id), 6)
        self.assertEqual(len(self.gmail.sent), 1)
        out = self.run_cli(core.cmd_import, self.ns(no_email=True))
        self.assertIn("0 new shift(s), 6 already", out)
        self.assertEqual(len(self.cal.events_by_id), 6)

    def test_import_shows_conflicts(self):
        self.cal.add_raw("Staples", datetime(2026, 9, 22, 15), datetime(2026, 9, 22, 17))
        out = self.run_cli(core.cmd_import, self.ns(dry_run=True))
        self.assertIn("! OVERLAP Tue 9/22", out)

    def test_confirmation_can_cancel(self):
        with self.mock.patch("builtins.input", return_value="n"):
            out = self.run_cli(core.cmd_import, self.ns(yes=False))
        self.assertIn("Cancelled.", out)
        self.assertEqual(len(self.cal.events_by_id), 0)

    def test_updated_schedule_removes_dropped_shift(self):
        self.run_cli(core.cmd_import, self.ns(no_email=True))
        changed = SAMPLE.replace("Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM\n", "")
        self.gmail.messages_list.append(make_message("b", changed, 2_000))
        out = self.run_cli(core.cmd_import, self.ns(no_email=True))
        self.assertIn("1 dropped from the schedule (removed)", out)
        self.assertEqual(len(self.cal.events_by_id), 5)

    def test_keep_dropped(self):
        self.run_cli(core.cmd_import, self.ns(no_email=True))
        self.gmail.messages_list.append(make_message("b", SAMPLE.replace("Tuesday, 9/22/2026, Insider, 4:00 PM to 7:15 PM\n", ""), 2_000))
        self.run_cli(core.cmd_import, self.ns(no_email=True, keep_dropped=True))
        self.assertEqual(len(self.cal.events_by_id), 6)

    def test_search_command(self):
        self.cal.add_raw("Dentist", datetime.now().replace(hour=9, minute=0, second=0, microsecond=0),
                         datetime.now().replace(hour=10, minute=0, second=0, microsecond=0))
        out = self.run_cli(core.cmd_search, self.mock.Mock(text=["dent"]))
        self.assertIn("Dentist", out)
        self.assertIn("1 match(es).", out)
        self.assertIn("No matches.", self.run_cli(core.cmd_search, self.mock.Mock(text=["zzz"])))

    def test_parser_knows_new_commands(self):
        parser = core.build_parser()
        self.assertEqual(parser.parse_args(["search", "a", "b"]).text, ["a", "b"])
        self.assertTrue(parser.parse_args(["import", "--keep-dropped"]).keep_dropped)


class EarningsTests(unittest.TestCase):
    def test_weekly_rows(self):
        cfg = make_config()
        cal = FakeCalendar()
        cal.add_raw("Dominos", datetime(2026, 9, 22, 16), datetime(2026, 9, 22, 20))      # this week: 4h
        cal.add_raw("Staples", datetime(2026, 9, 16, 12), datetime(2026, 9, 16, 17))      # last week: 5h
        cal.add_raw("CIS 111", datetime(2026, 9, 22, 9), datetime(2026, 9, 22, 11))       # not paid
        rows = core.compute_weekly_earnings(cal, cfg, weeks=3, today=date(2026, 9, 23))
        self.assertEqual([r["week_start"] for r in rows], [date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21)])
        self.assertEqual([r["current"] for r in rows], [False, False, True])
        self.assertEqual([r["label"] for r in rows], ["9/7", "9/14", "9/21"])
        self.assertEqual([r["hours"] for r in rows], [0, 5, 4])
        self.assertAlmostEqual(rows[1]["pay"], 5 * 17.2)
        self.assertAlmostEqual(rows[2]["pay"], 4 * 17.58)

    def test_monthly_rows(self):
        cfg = make_config()
        cal = FakeCalendar()
        cal.add_raw("Dominos", datetime(2026, 9, 30, 16), datetime(2026, 9, 30, 20))      # Sept: 4h
        cal.add_raw("Staples", datetime(2026, 8, 31, 12), datetime(2026, 8, 31, 17))      # Aug: 5h (boundary day)
        cal.add_raw("Dominos", datetime(2026, 10, 1, 16), datetime(2026, 10, 1, 19))      # Oct: outside the window
        cal.add_raw("Dominos", datetime(2026, 4, 30, 16), datetime(2026, 4, 30, 19))      # before the window
        rows = core.compute_monthly_earnings(cal, cfg, months=6, today=date(2026, 9, 18))
        self.assertEqual([r["label"] for r in rows], ["Apr", "May", "Jun", "Jul", "Aug", "Sep"][-6:])
        self.assertEqual([r["month_start"] for r in rows][0], date(2026, 4, 1))
        self.assertEqual([r["hours"] for r in rows], [3, 0, 0, 0, 5, 4])
        self.assertEqual([r["current"] for r in rows], [False] * 5 + [True])

    def test_monthly_year_rollover(self):
        rows = core.compute_monthly_earnings(FakeCalendar(), make_config(), months=3, today=date(2026, 1, 15))
        self.assertEqual([r["month_start"] for r in rows], [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1)])

    def test_take_home(self):
        self.assertEqual(core.take_home(100, {"tax_rate_percent": 20}), 80)
        self.assertEqual(core.take_home(100, {}), 100)
        self.assertEqual(core.take_home(100, {"tax_rate_percent": "abc"}), 100)
        self.assertEqual(core.take_home(100, {"tax_rate_percent": -5}), 100)
        self.assertAlmostEqual(core.take_home(100, {"tax_rate_percent": 500}), 10)  # capped at 90%

    def test_report_take_home_line(self):
        d = datetime(2026, 9, 22)
        events = [ev("Dominos", d.replace(hour=16), d.replace(hour=20))]
        cfg = make_config(tax_rate_percent=15)
        days = core.group_events_by_day(events, date(2026, 9, 21), date(2026, 9, 27))
        _, body = core.build_weekly_report_email(date(2026, 9, 21), date(2026, 9, 27), days, {}, core.compute_category_hours(events), cfg)
        self.assertIn("Take-home ~$59.77 after 15% tax", body)
        _, none = core.build_weekly_report_email(date(2026, 9, 21), date(2026, 9, 27), days, {}, core.compute_category_hours(events), make_config())
        self.assertNotIn("Take-home", none)


class HtmlEmailTests(unittest.TestCase):
    def report(self, cfg=None, events=None):
        cfg = cfg or make_config()
        d = datetime(2026, 9, 22)
        events = events if events is not None else [
            ev("Dominos", d.replace(hour=16), d.replace(hour=19, minute=15)),
            ev("Staples <b>&</b>", datetime(2026, 9, 21, 15), datetime(2026, 9, 21, 20)),
            ev("CIS 111", d.replace(hour=13), d.replace(hour=15, minute=15))]
        days = core.group_events_by_day(events, date(2026, 9, 21), date(2026, 9, 27))
        conflicts = core.find_all_conflicts(days, cfg)
        return core.build_weekly_report_html(date(2026, 9, 21), date(2026, 9, 27), days, conflicts,
                                             core.compute_category_hours(events), cfg)

    def test_content_and_escaping(self):
        html = self.report()
        self.assertIn("Sep 21 - Sep 27, 2026", html)
        self.assertIn("Dominos 3.25h", html)
        self.assertIn("$57.13", html)                        # 3.25h at 17.58
        self.assertIn("Staples &lt;b&gt;&amp;&lt;/b&gt;", html)  # user text is escaped, never injected as markup
        self.assertNotIn("<b>&</b>", html)
        self.assertIn(core.category_color("Dominos"), html)
        self.assertIn("CLOSE (45m)", html)                   # the class/shift turnaround is called out
        self.assertIn("Nothing scheduled", html)
        for day in core.DAY_ABBR:
            self.assertIn(f">{day}</div>", html)

    def test_well_formed(self):
        from html.parser import HTMLParser

        class Balanced(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.ok = [], True
            def handle_starttag(self, tag, attrs):
                if tag not in ("br", "meta", "img"):
                    self.stack.append(tag)
            def handle_endtag(self, tag):
                if not self.stack or self.stack.pop() != tag:
                    self.ok = False
        p = Balanced()
        p.feed(self.report())
        self.assertTrue(p.ok and not p.stack)

    def test_no_conflicts_message_and_tax_goal_lines(self):
        cfg = make_config(tax_rate_percent=10, weekly_hours_goal=2)
        d = datetime(2026, 9, 22)
        html = self.report(cfg, [ev("Dominos", d.replace(hour=16), d.replace(hour=19)), ev("Staples", d.replace(hour=9), d.replace(hour=12))])
        self.assertIn("No conflicts this week.", html)
        self.assertIn("after 10% tax", html)
        self.assertIn("Over your 2h goal", html)

    def test_multipart_message_and_toggle(self):
        import email
        raw = core.build_raw_message("a@b.c", "Weekly", "plain text", "<p>html</p>")["raw"]
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual([p.get_content_type() for p in msg.walk()][1:], ["text/plain", "text/html"])
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        plain_only = email.message_from_bytes(base64.urlsafe_b64decode(core.build_raw_message("a@b.c", "W", "plain")["raw"]))
        self.assertEqual(plain_only.get_content_type(), "text/plain")
        rep = {"html": "<p>x</p>"}
        self.assertEqual(core.email_html({"email_html": True}, rep), "<p>x</p>")
        self.assertIsNone(core.email_html({"email_html": False}, rep))

    def test_report_and_import_send_both_parts(self):
        cfg = make_config()
        cal, gmail = FakeCalendar(), FakeGmail()
        rep = core.generate_weekly_report(cal, cfg, date(2026, 9, 21), date(2026, 9, 27))
        self.assertIn("<html>", rep["html"])
        core.send_email(gmail, "me@example.com", rep["subject"], rep["body"], core.email_html(cfg, rep))
        self.assertIn("text/html", gmail.sent[0]["raw"])
        self.assertIn("text/plain", gmail.sent[0]["raw"])


class StudyPlannerTests(unittest.TestCase):
    NOW = datetime(2026, 9, 20, 12, 0)   # a Sunday; the plan covers the week after

    def week(self, events, start=date(2026, 9, 21)):
        return core.group_events_by_day(events, start, start + timedelta(days=6))

    def test_empty_day_gets_one_block_from_window_start(self):
        plan = core.suggest_study_blocks(self.week([]), 2, now=self.NOW)
        self.assertEqual([(b["day"], b["start"].hour, b["end"].hour) for b in plan], [(date(2026, 9, 21), 9, 11)])
        self.assertEqual(plan[0]["hours"], 2)

    def test_blocks_keep_a_buffer_from_events(self):
        d = datetime(2026, 9, 21)
        events = [ev("CIS 111", d.replace(hour=9), d.replace(hour=11)), ev("Dominos", d.replace(hour=16), d.replace(hour=19))]
        days = self.week(events)
        plan = core.suggest_study_blocks({date(2026, 9, 21): days[date(2026, 9, 21)]}, 2, now=self.NOW)
        b = plan[0]
        self.assertEqual((b["start"], b["end"]), (d.replace(hour=11, minute=30), d.replace(hour=13, minute=30)))
        for e in events:  # never closer than 30 minutes to anything
            gap = min(abs((b["start"] - e["end"]).total_seconds()), abs((e["start"] - b["end"]).total_seconds())) / 60
            self.assertTrue(gap >= 30 or b["end"] <= e["start"] - timedelta(minutes=30) or b["start"] >= e["end"] + timedelta(minutes=30))

    def test_spreads_over_days_and_prefers_light_days(self):
        d = datetime(2026, 9, 21)
        busy_monday = [ev("Dominos", d.replace(hour=9), d.replace(hour=13))]
        days = {date(2026, 9, 21): self.week(busy_monday)[date(2026, 9, 21)], date(2026, 9, 22): [], date(2026, 9, 23): []}
        plan = core.suggest_study_blocks(days, 4, now=self.NOW)
        self.assertEqual([b["day"] for b in plan], [date(2026, 9, 22), date(2026, 9, 23)])   # light days first, one block each
        self.assertAlmostEqual(sum(b["hours"] for b in plan), 4)

    def test_goal_is_respected_and_last_block_trimmed(self):
        plan = core.suggest_study_blocks(self.week([]), 3.25, now=self.NOW)
        self.assertAlmostEqual(sum(b["hours"] for b in plan), 3.25)
        self.assertTrue(all(b["hours"] >= 0.5 for b in plan))

    def test_not_enough_free_time_plans_what_fits(self):
        d = datetime(2026, 9, 21)
        packed = [ev("Staples", d.replace(hour=8, minute=30), d.replace(hour=21, minute=30))]
        plan = core.suggest_study_blocks({date(2026, 9, 21): self.week(packed)[date(2026, 9, 21)]}, 5, now=self.NOW)
        self.assertEqual(plan, [])

    def test_past_days_skipped_and_today_starts_later(self):
        now = datetime(2026, 9, 22, 10, 10)
        days = self.week([])
        plan = core.suggest_study_blocks({d: days[d] for d in (date(2026, 9, 21), date(2026, 9, 22))}, 1, now=now)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["day"], date(2026, 9, 22))
        self.assertEqual(plan[0]["start"], datetime(2026, 9, 22, 10, 45))   # now + 30 min, rounded up to the quarter hour

    def test_respects_max_block_and_no_overlaps(self):
        plan = core.suggest_study_blocks(self.week([]), 12, max_minutes=90, now=self.NOW)
        self.assertTrue(all(b["hours"] <= 1.5 for b in plan))
        by_day = {}
        for b in plan:
            by_day.setdefault(b["day"], []).append(b)
        for blocks in by_day.values():
            blocks.sort(key=lambda b: b["start"])
            for x, y in zip(blocks, blocks[1:]):
                self.assertGreaterEqual((y["start"] - x["end"]).total_seconds() / 60, 30)
        self.assertAlmostEqual(sum(b["hours"] for b in plan), 12)


class PayPeriodTests(unittest.TestCase):
    BI = {"type": "biweekly", "start": "2026-09-07", "delay_days": 5}

    def test_biweekly_bounds_and_before_anchor(self):
        self.assertEqual(core.pay_period_bounds(date(2026, 9, 7), self.BI), (date(2026, 9, 7), date(2026, 9, 20)))
        self.assertEqual(core.pay_period_bounds(date(2026, 9, 20), self.BI), (date(2026, 9, 7), date(2026, 9, 20)))
        self.assertEqual(core.pay_period_bounds(date(2026, 9, 21), self.BI), (date(2026, 9, 21), date(2026, 10, 4)))
        self.assertEqual(core.pay_period_bounds(date(2026, 9, 6), self.BI), (date(2026, 8, 24), date(2026, 9, 6)))

    def test_weekly_semimonthly_monthly(self):
        self.assertEqual(core.pay_period_bounds(date(2026, 9, 18), {"type": "weekly", "start": "2026-09-14"}), (date(2026, 9, 14), date(2026, 9, 20)))
        semi = {"type": "semimonthly"}
        self.assertEqual(core.pay_period_bounds(date(2026, 2, 15), semi), (date(2026, 2, 1), date(2026, 2, 15)))
        self.assertEqual(core.pay_period_bounds(date(2026, 2, 16), semi), (date(2026, 2, 16), date(2026, 2, 28)))
        self.assertEqual(core.pay_period_bounds(date(2028, 2, 20), semi), (date(2028, 2, 16), date(2028, 2, 29)))
        self.assertEqual(core.pay_period_bounds(date(2026, 12, 31), {"type": "monthly"}), (date(2026, 12, 1), date(2026, 12, 31)))

    def test_not_configured(self):
        for sched in ({}, {"type": "off"}, {"type": "biweekly"}, {"type": "biweekly", "start": "garbage"}, None):
            self.assertIsNone(core.pay_period_bounds(date(2026, 9, 18), sched))
        self.assertIsNone(core.next_payday({"type": "off"}))

    def test_each_job_has_its_own_schedule(self):
        weekly = {"type": "weekly", "start": "2026-09-14", "delay_days": 3}
        cfg = make_config(job_pay={"Dominos": self.BI, "Staples": weekly}, pay_schedule={"type": "off"})
        self.assertEqual(core.job_pay_schedule(cfg, "Staples"), weekly)
        self.assertEqual(core.scheduled_jobs(cfg), ["Dominos", "Staples"])
        paydays = core.upcoming_paydays(cfg, date(2026, 9, 18))
        self.assertEqual([(j, d) for j, d, _ in paydays], [("Staples", date(2026, 9, 23)), ("Dominos", date(2026, 9, 25))])

    def test_the_older_single_schedule_still_applies_to_jobs_without_their_own(self):
        cfg = make_config(pay_schedule=self.BI, job_pay={"Staples": {"type": "off"}})
        self.assertEqual(core.scheduled_jobs(cfg), ["Dominos"])          # Staples opted out, Dominos inherits
        self.assertEqual(core.job_pay_schedule(cfg, "Dominos"), self.BI)
        self.assertEqual(core.upcoming_paydays(make_config(), date(2026, 9, 18)), [])

    def test_pay_periods_follow_the_chosen_job(self):
        cal = FakeCalendar()
        cal.add_raw("Dominos", datetime(2026, 9, 21, 16, 0), datetime(2026, 9, 21, 20, 0))
        cal.add_raw("Staples", datetime(2026, 9, 22, 9, 0), datetime(2026, 9, 22, 14, 0))
        cfg = make_config(job_pay={"Dominos": self.BI, "Staples": {"type": "weekly", "start": "2026-09-14", "delay_days": 0}})
        dom = core.compute_pay_periods(cal, cfg, count=2, today=date(2026, 9, 23), job="Dominos")
        sta = core.compute_pay_periods(cal, cfg, count=2, today=date(2026, 9, 23), job="Staples")
        self.assertEqual((dom[-1]["week_end"] - dom[-1]["week_start"]).days, 13)
        self.assertEqual((sta[-1]["week_end"] - sta[-1]["week_start"]).days, 6)
        self.assertEqual(dom[-1]["hours"], 4.0)
        self.assertEqual(sta[-1]["hours"], 5.0)
        default = core.compute_pay_periods(cal, cfg, count=2, today=date(2026, 9, 23))
        self.assertEqual(default[-1]["job"], "Dominos")

    def test_next_payday(self):
        # period 9/7-9/20 pays 9/25; period 9/21-10/4 pays 10/9
        self.assertEqual(core.next_payday(self.BI, date(2026, 9, 18)), (date(2026, 9, 25), (date(2026, 9, 7), date(2026, 9, 20))))
        self.assertEqual(core.next_payday(self.BI, date(2026, 9, 25))[0], date(2026, 9, 25))   # payday itself counts
        # after the period ended but before it pays, that period is still the next paycheck
        self.assertEqual(core.next_payday(self.BI, date(2026, 9, 22))[0], date(2026, 9, 25))
        self.assertEqual(core.next_payday(self.BI, date(2026, 9, 26))[0], date(2026, 10, 9))

    def test_compute_periods(self):
        cfg = make_config(pay_schedule=self.BI)
        cal = FakeCalendar()
        cal.add_raw("Dominos", datetime(2026, 9, 8, 16), datetime(2026, 9, 8, 20))     # period 9/7-9/20: 4h
        cal.add_raw("Staples", datetime(2026, 9, 20, 12), datetime(2026, 9, 20, 17))   # last day of that period: 5h
        cal.add_raw("Dominos", datetime(2026, 9, 22, 16), datetime(2026, 9, 22, 19))   # period 9/21-10/4: 3h
        rows = core.compute_pay_periods(cal, cfg, count=3, today=date(2026, 9, 23))
        self.assertEqual([r["label"] for r in rows], ["8/24-9/6", "9/7-9/20", "9/21-10/4"])
        self.assertEqual([r["hours"] for r in rows], [0, 9, 3])
        self.assertEqual([r["current"] for r in rows], [False, False, True])
        self.assertEqual(rows[1]["payday"], date(2026, 9, 25))
        self.assertAlmostEqual(rows[1]["pay"], 4 * 17.58 + 5 * 17.2)
        with self.assertRaises(ValueError):
            core.compute_pay_periods(cal, make_config(), today=date(2026, 9, 23))


class IcsTests(unittest.TestCase):
    def test_export(self):
        d = datetime(2026, 9, 22)
        events = [ev("Dominos", d.replace(hour=16), d.replace(hour=19, minute=15)),
                  {"id": "a1", "summary": "Essay, due; today", "start": None, "end": None, "all_day": True, "day": date(2026, 9, 23),
                   "category": "Other", "calendar_id": "primary"}]
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.ics"
            n = core.export_events_ics(p, events, "America/Los_Angeles", now=datetime(2026, 9, 18, 12))
            raw = p.read_bytes().decode("utf-8")
        self.assertEqual(n, 2)
        self.assertTrue(raw.startswith("BEGIN:VCALENDAR\r\n") and raw.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(raw.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART:20260922T230000Z", raw)      # 4 PM PDT == 23:00 UTC
        self.assertIn("DTEND:20260923T021500Z", raw)        # crosses midnight in UTC
        self.assertIn("DTSTART;VALUE=DATE:20260923", raw)
        self.assertIn("DTEND;VALUE=DATE:20260924", raw)
        self.assertIn("SUMMARY:Essay\\, due\\; today", raw)
        self.assertIn("DTSTAMP:20260918T120000Z", raw)

    def test_line_folding(self):
        long_title = "x" * 200
        d = datetime(2026, 9, 22)
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.ics"
            core.export_events_ics(p, [ev(long_title, d.replace(hour=9), d.replace(hour=10))], "America/Los_Angeles")
            lines = p.read_bytes().decode("utf-8").split("\r\n")
        self.assertTrue(all(len(l.encode("utf-8")) <= 75 for l in lines))
        unfolded = "".join(l[1:] if l.startswith(" ") else "\n" + l for l in lines)
        self.assertIn("SUMMARY:" + long_title, unfolded.replace("\n", ""))


class MoveTests(unittest.TestCase):
    def test_move_and_move_back(self):
        cal = FakeCalendar()
        e = cal.add_raw("Dentist", datetime(2026, 9, 22, 9), datetime(2026, 9, 22, 10))
        core.move_event(cal, "primary", e["id"], datetime(2026, 9, 23, 14, 15), datetime(2026, 9, 23, 15, 45), "America/Los_Angeles")
        got = core.normalize_event(cal.events_by_id[e["id"]], ZoneInfo("America/Los_Angeles"), make_config(), "primary")
        self.assertEqual((got["start"], got["end"], got["day"]), (datetime(2026, 9, 23, 14, 15), datetime(2026, 9, 23, 15, 45), date(2026, 9, 23)))
        core.move_event(cal, "primary", e["id"], datetime(2026, 9, 22, 9), datetime(2026, 9, 22, 10), "America/Los_Angeles")
        got = core.normalize_event(cal.events_by_id[e["id"]], ZoneInfo("America/Los_Angeles"), make_config(), "primary")
        self.assertEqual(got["start"], datetime(2026, 9, 22, 9))

    def test_rejects_backwards_range(self):
        with self.assertRaises(ValueError):
            core.move_event(FakeCalendar(), "primary", "x", datetime(2026, 9, 22, 10), datetime(2026, 9, 22, 9), "America/Los_Angeles")


class SearchTests(unittest.TestCase):
    def test_search(self):
        cfg = make_config()
        cal = FakeCalendar()
        cal.add_raw("Dentist appointment", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 10))
        cal.add_raw("Dominos", datetime(2026, 9, 22, 16), datetime(2026, 9, 22, 19))
        cal.add_raw("dentist follow-up", datetime(2026, 9, 1, 9), datetime(2026, 9, 1, 10))
        cal.add_raw("Dentist (old)", datetime(2024, 1, 1, 9), datetime(2024, 1, 1, 10))  # outside the window
        res = core.search_events(cal, cfg, "DENTIST", today=date(2026, 9, 18))
        self.assertEqual([e["summary"] for e in res], ["dentist follow-up", "Dentist appointment"])  # oldest first
        self.assertEqual(res[0]["category"], "Other")
        self.assertEqual(core.search_events(cal, cfg, "   ", today=date(2026, 9, 18)), [])
        self.assertEqual(core.search_events(cal, cfg, "zzz", today=date(2026, 9, 18)), [])
        self.assertEqual([e["category"] for e in core.search_events(cal, cfg, "dominos", today=date(2026, 9, 18))], ["Dominos"])


class MonthTests(unittest.TestCase):
    def test_grid_bounds(self):
        from month_view import month_grid_bounds
        self.assertEqual(month_grid_bounds(date(2026, 9, 18)), (date(2026, 8, 31), date(2026, 10, 4)))  # 5 rows
        self.assertEqual(month_grid_bounds(date(2026, 2, 1)), (date(2026, 1, 26), date(2026, 3, 1)))  # exactly 5 weeks
        s, e = month_grid_bounds(date(2026, 8, 5))  # Aug 2026 starts on Saturday: 6 rows
        self.assertEqual((s, e, ((e - s).days + 1) // 7), (date(2026, 7, 27), date(2026, 9, 6), 6))
        self.assertEqual(month_grid_bounds(date(2027, 2, 10))[0].weekday(), 0)

    def test_daily_paid_hours(self):
        cfg = make_config()
        d = datetime(2026, 9, 22)
        events = [ev("Dominos", d.replace(hour=16), d.replace(hour=19, minute=30)), ev("Dominos", d.replace(hour=21), d.replace(hour=23)),
                  ev("CIS 111", d.replace(hour=9), d.replace(hour=11)), ev("Staples", datetime(2026, 9, 23, 12), datetime(2026, 9, 23, 17))]
        self.assertEqual(core.daily_paid_hours(events, cfg), {date(2026, 9, 22): 5.5, date(2026, 9, 23): 5.0})


class ExportTests(unittest.TestCase):
    def test_csv(self):
        d = datetime(2026, 9, 22)
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.csv"
            core.export_events_csv(p, [ev("Dominos", d.replace(hour=16), d.replace(hour=19, minute=30))])
            rows = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(rows[0], "Date,Title,Category,Start,End,Hours")
            self.assertEqual(rows[1], "2026-09-22,Dominos,Dominos,16:00,19:30,3.50")


if __name__ == "__main__":
    unittest.main()
