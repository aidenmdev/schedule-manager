"""The tablet page: layout helpers, old-browser-safe markup, and the little HTTP server.
Run:  .venv\\Scripts\\python.exe -m unittest tests.test_tablet -v"""
import re
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
import tablet_server as ts
from tests.fakes import FakeCalendar, make_config

MONDAY = date(2026, 9, 21)


def ev(title, day, sh, sm, eh, em, config, all_day=False):
    d = MONDAY + timedelta(days=day)
    start = None if all_day else datetime(d.year, d.month, d.day, sh, sm)
    end = None if all_day else datetime(d.year, d.month, d.day, eh, em)
    return {"id": title, "summary": title, "start": start, "end": end, "all_day": all_day, "day": d,
            "category": core.classify_event(title, config), "calendar_id": "primary"}


class HelperTests(unittest.TestCase):
    def test_short_time(self):
        self.assertEqual(ts.short_time(datetime(2026, 1, 1, 0, 0)), "12a")
        self.assertEqual(ts.short_time(datetime(2026, 1, 1, 12, 30)), "12:30p")
        self.assertEqual(ts.short_time(datetime(2026, 1, 1, 17, 0)), "5p")

    def test_text_color_contrast(self):
        self.assertEqual(ts.text_color_for("#3b82f6"), "#ffffff")
        self.assertEqual(ts.text_color_for("#a78bfa"), "#0b0f17")

    def test_hour_window_defaults_and_widens(self):
        cfg = make_config()
        self.assertEqual(ts.hour_window({MONDAY: []}), (8, 22))
        days = {MONDAY: [ev("Dominos", 0, 6, 0, 23, 30, cfg)]}
        self.assertEqual(ts.hour_window(days), (6, 24))
        overnight = ev("Dominos", 0, 20, 0, 23, 0, cfg)
        overnight["end"] += timedelta(hours=3)
        self.assertEqual(ts.hour_window({MONDAY: [overnight]}), (8, 24))

    def test_hour_window_never_tiny(self):
        lo, hi = ts.hour_window({}, default=(10, 12))
        self.assertGreaterEqual(hi - lo, 6)

    def test_lay_out_side_by_side_only_when_overlapping(self):
        cfg = make_config()
        a = ev("A", 0, 9, 0, 11, 0, cfg)
        b = ev("B", 0, 10, 0, 12, 0, cfg)
        c = ev("C", 0, 13, 0, 14, 0, cfg)
        placed = {e["summary"]: (lane, n) for e, lane, n in ts.lay_out([c, b, a])}
        self.assertEqual(placed["A"], (0, 2))
        self.assertEqual(placed["B"], (1, 2))
        self.assertEqual(placed["C"], (0, 1))

    def test_lay_out_reuses_lanes(self):
        cfg = make_config()
        evs = [ev("A", 0, 9, 0, 12, 0, cfg), ev("B", 0, 9, 30, 10, 0, cfg), ev("C", 0, 10, 0, 11, 0, cfg)]
        placed = {e["summary"]: (lane, n) for e, lane, n in ts.lay_out(evs)}
        self.assertEqual(placed["B"], (1, 2))
        self.assertEqual(placed["C"], (1, 2))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.now = datetime(2026, 9, 23, 14, 0)
        events = [
            ev("Dominos", 0, 16, 0, 22, 0, self.cfg),
            ev("Staples", 2, 9, 0, 13, 0, self.cfg),
            ev("CIS 111", 2, 12, 0, 13, 30, self.cfg),
            ev("<script>alert(1)</script>", 3, 10, 0, 11, 0, self.cfg),
            ev("Holiday", 4, 0, 0, 0, 0, self.cfg, all_day=True),
        ]
        self.entry = {"events": events, "stale_at": None}

    def page(self, **kw):
        return ts.render_week(self.entry, MONDAY, 0, self.cfg, self.now, **kw)

    def test_shows_events_totals_and_today(self):
        page = self.page(updated=datetime(2026, 9, 23, 13, 58))
        for text in ("Dominos", "Staples", "CIS 111", "Holiday", "Sep 21 – 27, 2026", "Updated 1:58p"):
            self.assertIn(text, page)
        self.assertIn("head today", page)
        self.assertIn('id="now"', page)
        self.assertIn("Dominos 6h", page)
        self.assertIn("Staples 4h", page)

    def test_titles_are_escaped(self):
        page = self.page()
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_only_finished_events_are_dimmed(self):
        page = self.page()
        self.assertEqual(page.count("ev done"), 3)  # Monday's shift plus Wednesday's two; Thursday hasn't happened

    def test_no_features_old_browsers_lack(self):
        page = self.page()
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        for banned in ("display:flex", "display:grid", "var(--", "calc(", "vh", "vw", "position:sticky", "@media"):
            self.assertNotIn(banned, style, banned)
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        for banned in ("=>", "let ", "const ", "`", "fetch(", "querySelector", "classList"):
            self.assertNotIn(banned, script, banned)

    def test_other_week_has_no_now_line_or_today(self):
        page = ts.render_week({"events": [], "stale_at": None}, MONDAY + timedelta(days=7), 1, self.cfg, self.now)
        self.assertNotIn('id="now"', page)
        self.assertNotIn("head today", page)
        self.assertIn("Nothing scheduled", page)
        self.assertIn("/?w=2", page)
        self.assertIn("/?w=0", page)

    def test_no_pay_or_payday_anywhere(self):
        self.cfg["pay_schedule"] = {"type": "biweekly", "start": "2026-09-11", "delay_days": 5}
        page = self.page()
        self.assertNotIn("$", page.split('id="foot"')[1])
        self.assertNotIn("Payday", page)

    def test_stale_and_problem_notices(self):
        self.entry["stale_at"] = datetime(2026, 9, 22, 8, 5)
        self.assertIn("Offline, showing the copy saved 9/22 8:05a", self.page())
        self.entry["stale_at"] = None
        self.assertIn("Can&#x27;t reach Google", self.page(problem="Can't reach Google"))

    def test_month_boundary_title(self):
        page = ts.render_week({"events": [], "stale_at": None}, date(2026, 9, 28), 1, self.cfg, self.now)
        self.assertIn("Sep 28 – Oct 4, 2026", page)

    def test_event_running_past_midnight_stays_inside_grid(self):
        e = ev("Dominos", 1, 20, 0, 23, 0, self.cfg)
        e["end"] += timedelta(hours=3)
        page = ts.render_week({"events": [e], "stale_at": None}, MONDAY, 0, self.cfg, self.now)
        top, height = re.search(r'class="ev[^"]*".*?top:([\d.]+)%;height:([\d.]+)%', page, re.S).groups()
        self.assertLessEqual(float(top) + float(height), 100.01)

    def test_overlapping_events_share_the_column(self):
        page = self.page()
        widths = re.findall(r'class="ev[^"]*"[^>]*style="left:[\d.]+%;width:([\d.]+)%', page)
        self.assertEqual(len(widths), 4)
        self.assertEqual(len({w for w in widths if float(w) < ts.DAY_W / 2}), 1)  # Staples + CIS 111 are half-width


class FeedAndServerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.cal = FakeCalendar()
        today = datetime.now(ZoneInfo(self.cfg["timezone"])).date()
        self.ws = today - timedelta(days=today.weekday())
        base = datetime(self.ws.year, self.ws.month, self.ws.day)
        self.cal.add_raw("Dominos", base + timedelta(hours=16), base + timedelta(hours=21))
        self.feed = ts.Feed(self.cfg, interval=30)
        self.server = ts.Server(("127.0.0.1", 0), ts.make_handler(self.feed))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.read().decode("utf-8"), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode("utf-8"), e.headers

    def test_waits_until_data_exists_then_serves_it(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Loading", body)
        self.feed.refresh(self.cal)
        status, body, headers = self.get("/")
        self.assertIn("Dominos", body)
        self.assertIn("Updated", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("utf-8", headers["Content-Type"])

    def test_a_second_copy_cannot_take_the_same_port(self):
        with self.assertRaises(OSError):
            ts.Server(("127.0.0.1", self.port), ts.make_handler(self.feed))
        self.assertTrue(ts.already_running(self.port))

    def test_health_and_unknown_paths(self):
        self.assertEqual(self.get("/health")[:2], (200, "ok"))
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/../../etc/passwd")[0], 404)

    def test_bad_week_parameters_are_clamped(self):
        self.feed.refresh(self.cal)
        for q in ("?w=abc", "?w=99999", "?w=-99999", "?w=", "?w=1&w=2"):
            self.assertEqual(self.get("/" + q)[0], 200, q)

    def test_all_browsable_weeks_load(self):
        self.feed.refresh(self.cal)
        for off in ts.OFFSETS:
            status, body, _ = self.get(f"/?w={off}")
            self.assertEqual(status, 200)
            self.assertIn('id="grid"', body)

    def test_offline_keeps_last_data_and_says_so(self):
        self.feed.refresh(self.cal)
        self.cal.offline = True
        self.feed.refresh(self.cal)
        body = self.get("/")[1]
        self.assertIn("Dominos", body)
        self.assertIn("Offline, showing the copy saved", body)

    def test_bad_data_gives_an_error_page_not_a_dead_server(self):
        self.feed.refresh(self.cal)
        self.feed.weeks[self.ws]["events"] = [{"summary": "broken"}]
        status, body, _ = self.get("/")
        self.assertEqual(status, 500)
        self.assertIn("Something went wrong", body)
        self.assertEqual(self.get("/health")[0], 200)


if __name__ == "__main__":
    unittest.main()


class ControlTests(unittest.TestCase):
    """Starting, stopping and asking about the display, as the Settings page does."""

    def test_configured_port(self):
        self.assertEqual(ts.configured_port({}), ts.DEFAULT_PORT)
        self.assertEqual(ts.configured_port({"tablet_port": 9000}), 9000)
        self.assertEqual(ts.configured_port({"tablet_port": "abc"}), ts.DEFAULT_PORT)
        self.assertEqual(ts.configured_port(None), ts.DEFAULT_PORT)

    def test_background_command_names_the_port(self):
        command = ts.background_command(9123)
        self.assertEqual(command[-2:], ["--port", "9123"])
        self.assertTrue(command[1].endswith("tablet_server.py"))

    def test_status_reports_the_address_and_whether_it_answers(self):
        feed = ts.Feed(make_config(), interval=30)
        server = ts.Server(("127.0.0.1", 0), ts.make_handler(feed))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            up = ts.tablet_status(port)
            self.assertTrue(up["running"])
            self.assertEqual(up["url"], f"http://{up['ip']}:{port}")
            self.assertEqual(up["port"], port)
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(ts.tablet_status(port)["running"])

    def test_the_address_is_an_ipv4_address(self):
        parts = ts.lan_address().split(".")
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))

    def test_stopping_when_nothing_runs_says_so(self):
        self.assertFalse(ts.stop_processes(59321))

    def test_start_and_stop_a_real_background_server(self):
        """Uses an unusual port and an isolated data folder, so a display that is really running is left alone."""
        import os
        import shutil
        import socket
        tmp = tempfile.TemporaryDirectory()
        data = Path(tmp.name)
        shutil.copy(Path(__file__).resolve().parent.parent / "config.example.json", data / "config.json")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        saved_env, saved_status = os.environ.get("SCHEDULE_MANAGER_DATA"), ts.STATUS_PATH
        os.environ["SCHEDULE_MANAGER_DATA"] = str(data)
        ts.STATUS_PATH = data / "tablet_status.txt"
        try:
            self.assertFalse(ts.already_running(port))
            self.assertTrue(ts.start_background(port))
            self.assertTrue(ts.tablet_status(port)["running"])
            self.assertTrue(ts.start_background(port))          # already running is fine
            self.assertTrue(ts.stop_processes(port))
            end = time.time() + 10
            while ts.already_running(port) and time.time() < end:
                time.sleep(0.3)
            self.assertFalse(ts.already_running(port))
            self.assertFalse(ts.stop_processes(port))
        finally:
            ts.stop_processes(port)
            ts.STATUS_PATH = saved_status
            if saved_env is None:
                os.environ.pop("SCHEDULE_MANAGER_DATA", None)
            else:
                os.environ["SCHEDULE_MANAGER_DATA"] = saved_env
            time.sleep(0.5)
            tmp.cleanup()
