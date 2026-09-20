"""Runs the real GUI against fake Google services and an isolated temp config/state (nothing real is touched)."""
import json
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dominos_schedule as core
from tests.fakes import FakeCalendar, FakeGmail, make_config, make_message


def schedule_body(week_start: date, shifts):
    """shifts: list of (day_offset, 'H:MM PM', 'H:MM PM')"""
    end = week_start + timedelta(days=6)
    lines = [f"JORDAN SMITH, Domino's store 1234 just published a work schedule for the week of "
             f"{week_start.month}/{week_start.day}/{week_start.year} to {end.month}/{end.day}/{end.year}. Your schedule is:", ""]
    for off, a, b in shifts:
        d = week_start + timedelta(days=off)
        lines.append(f"{d.strftime('%A')}, {d.month}/{d.day}/{d.year}, Insider, {a} to {b}")
    lines += ["", "If you have questions, please call the store:", "(555) 010-0100", "", "Thanks!"]
    return "\n".join(lines)


class Env:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="schedule_gui_test_"))
        self.cfg = make_config()
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        self.cal = FakeCalendar()
        today = date.today()
        self.mon = today - timedelta(days=today.weekday())
        self._seed_calendar()
        self._seed_emails()
        self._saved = {}

    # ---- data ----
    def ev(self, title, off, sh, sm, eh, em, week=0):
        d = self.mon + timedelta(days=off, weeks=week)
        return self.cal.add_raw(title, datetime(d.year, d.month, d.day, sh, sm), datetime(d.year, d.month, d.day, eh, em))

    def _seed_calendar(self):
        E = self.ev
        E("Staples", 0, 15, 30, 20, 30); E("CIS 111", 1, 13, 0, 15, 15); E("Dominos", 1, 16, 0, 19, 15)
        E("CIS 110", 2, 17, 0, 18, 50); E("Dominos", 3, 16, 45, 19, 15); E("Dentist", 3, 18, 45, 19, 30)
        E("Dominos", 4, 16, 30, 20, 45); E("Dominos", 4, 21, 15, 23, 30); E("Dominos", 5, 17, 0, 20, 45)
        E("Dominos", 6, 15, 15, 22, 30); E("Work", 2, 9, 0, 14, 0)
        E("Staples", 7, 16, 0, 21, 0); E("CIS 111", 8, 13, 0, 15, 15); E("CIS 110", 9, 17, 0, 18, 50)
        E("Staples", 10, 12, 0, 18, 0)
        E("Staples", 14, 15, 0, 17, 0)  # overlaps the first shift of the not-yet-imported week
        for w, (a, b, c, d_) in {-1: (0, 16, 22, "Dominos"), -2: (1, 16, 21, "Dominos"), -3: (2, 16, 20, "Dominos")}.items():
            E(d_, a, b, 0, c, 0, w)
            E("Staples", 3, 12, 0, 17, 0, w)

    def _seed_emails(self):
        next_mon = self.mon + timedelta(days=14)
        shifts = [(0, "4:00 PM", "7:15 PM"), (2, "4:45 PM", "7:15 PM"), (4, "4:30 PM", "8:45 PM"), (5, "5:00 PM", "8:45 PM")]
        older = [(1, "4:00 PM", "7:00 PM"), (3, "4:00 PM", "8:00 PM")]
        now_ms = int(time.time() * 1000)
        self.gmail = FakeGmail(messages=[
            make_message("new-week", schedule_body(next_mon, shifts), now_ms),
            make_message("old-week", schedule_body(self.mon + timedelta(days=7), older), now_ms - 7 * 86400_000),
            make_message("noise", "Your pizza is on its way", now_ms - 86400_000),
        ])

    # ---- patching ----
    def install(self):
        import schedule_gui as g
        self.g = g
        pairs = [(core, "BASE_DIR", self.tmp), (core, "CONFIG_PATH", self.cfg_path), (core, "STATE_PATH", self.tmp / "state.json"),
                 (core, "CACHE_DIR", self.tmp / ".cache"), (core, "EMAIL_CACHE_PATH", self.tmp / ".cache" / "emails.json"),
                 (core, "EVENTS_CACHE_PATH", self.tmp / ".cache" / "events.json"), (core, "BACKUP_DIR", self.tmp / "backups"),
                 (core, "TOKEN_PATH", self.tmp / "token.json"), (core, "PREFS_PATH", self.tmp / "gui_prefs.json"), (g, "PREFS_PATH", self.tmp / "gui_prefs.json"),
                 (g, "LOG_PATH", self.tmp / "app.log"), (g, "BASE", self.tmp), (core, "build_services", lambda: (self.gmail, self.cal))]
        for mod, name, val in pairs:
            self._saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, val)
        return self

    def uninstall(self):
        for (mod, name), val in self._saved.items():
            setattr(mod, name, val)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def errors(self):
        p = self.tmp / "app.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def find_widgets(root, cls, text=None):
    out = []
    for w in root.winfo_children():
        if isinstance(w, cls) and (text is None or getattr(w, "_text", None) == text):
            out.append(w)
        out += find_widgets(w, cls, text)
    return out
