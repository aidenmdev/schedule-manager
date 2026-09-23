#!/usr/bin/env python3
"""
Domino's schedule -> Google Calendar importer.

Run with no arguments for the normal weekly flow:
    python dominos_schedule.py

Other commands:
    python dominos_schedule.py find                 # list schedule emails found in Gmail
    python dominos_schedule.py import                # (default) import a schedule, asks to confirm
    python dominos_schedule.py report                 # email this week's full schedule report (all calendars)
    python dominos_schedule.py search dentist         # find events on your calendar
    python dominos_schedule.py delete --week 9/21/2026
    python dominos_schedule.py undo                  # undo the most recent import
    python dominos_schedule.py reset                 # undo EVERYTHING this tool has ever done
    python dominos_schedule.py history                # show a log of everything imported/undone
    python dominos_schedule.py sync                   # sync settings and history with your other computers
    python dominos_schedule.py check                  # verify auth + config are working

Also see schedule_gui.py for a point-and-click version of all of the above.

Run `python dominos_schedule.py <command> -h` for command-specific options.
"""
from __future__ import annotations

import argparse
import base64
import html as htmllib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo



def resolve_dirs(frozen: bool, executable: str = "", bundle_dir: str = "", env=None, source_dir=None) -> tuple:
    """(data folder, read-only resource folder). Running from source, both are this folder, so the whole
    thing stays portable. The installed app keeps its data in %LOCALAPPDATA%\\Schedule Manager so
    updating or moving the program files never touches it. SCHEDULE_MANAGER_DATA overrides the data folder."""
    env = os.environ if env is None else env
    override = env.get("SCHEDULE_MANAGER_DATA")
    if frozen:
        resources = Path(bundle_dir or Path(executable).parent)
        data = Path(override) if override else Path(env.get("LOCALAPPDATA") or Path.home()) / "Schedule Manager"
    else:
        resources = Path(source_dir or Path(__file__).resolve().parent)
        data = Path(override) if override else resources
    return data, resources


BASE_DIR, RESOURCE_DIR = resolve_dirs(getattr(sys, "frozen", False), sys.executable, getattr(sys, "_MEIPASS", ""))
try:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_EXAMPLE_PATH = RESOURCE_DIR / "config.example.json"
TOKEN_PATH = BASE_DIR / "token.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
STATE_PATH = BASE_DIR / "state.json"
PREFS_PATH = BASE_DIR / "gui_prefs.json"
CACHE_DIR = BASE_DIR / ".cache"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_FILES = ("config.json", "state.json", "gui_prefs.json")
EMAIL_CACHE_PATH = CACHE_DIR / "emails.json"
EVENTS_CACHE_PATH = CACHE_DIR / "events.json"
PARSER_VERSION = 2
APP_VERSION = "2.0"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]

HEADER_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z '.\-]+),\s*Domino[\u2019']?s\s+[Ss]tore\s+(?P<store>\d+)\s+just published a work schedule "
    r"for the week of (?P<start>\d{1,2}/\d{1,2}/\d{4}) to (?P<end>\d{1,2}/\d{1,2}/\d{4})"
)
_TIME = r"\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?"
SHIFT_RE = re.compile(
    rf"^\s*(?P<day>[A-Za-z]+day),?\s*(?P<date>\d{{1,2}}/\d{{1,2}}/\d{{4}}),\s*(?P<role>[^,\n]+?),\s*"
    rf"(?P<start>{_TIME})\s*(?:to|-|\u2013)\s*(?P<end>{_TIME})[\s.]*$",
    re.MULTILINE | re.IGNORECASE,
)
WEEK_RE = re.compile(
    r"week of\s+(?P<start>\d{1,2}/\d{1,2}/\d{4})\s+(?:to|-|\u2013)\s+(?P<end>\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
PHONE_RE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")


def fmt_date(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_range(a: datetime, b: datetime) -> str:
    """'4:00-7:15 PM' (the AM/PM is shown once when both times share it), else '11:00 AM-3:00 PM'."""
    first, second = fmt_time(a), fmt_time(b)
    if first[-2:] == second[-2:]:
        first = first[:-3]
    return f"{first}-{second}"


def parse_mmddyyyy(s: str) -> date:
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def parse_date_flexible(text: str, today: Optional[date] = None) -> date:
    """Accepts 9/22/2026, 9/22 (this year), 'today', 'tomorrow'."""
    today = today or date.today()
    s = text.strip().lower()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(f"{s}/{today.year}", "%m/%d/%Y").date()
    except ValueError:
        raise ValueError(f"Can't read date '{text}' (try 9/22 or 9/22/2026)")


def parse_clock(text: str):
    """Accepts 4pm, 4:30 PM, 4:30pm, 16:00."""
    s = text.strip().lower().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Can't read time '{text}' (try 4:30 PM)")


class DataFileError(Exception):
    """A settings/data file exists but can't be read."""


def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_json_safe(path: Path):
    """Read JSON, falling back to path.bak if the file is damaged. Raises DataFileError if neither works."""
    path = Path(path)
    try:
        return _read_json(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as first:
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            try:
                data = _read_json(bak)
                shutil.copy2(bak, path)  # restore
                return data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        raise DataFileError(
            f"{path.name} is damaged ({first}). Fix it, or restore it from {path.name}.bak / the backups folder."
        ) from first


def write_json_atomic(path: Path, data) -> None:
    """Write via a temp file + rename so a crash can never leave a half-written file; keep a .bak of the last good copy."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        try:
            _read_json(path)  # only back up a copy that is itself valid
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
    os.replace(tmp, path)


def save_config(raw: dict) -> None:
    write_json_atomic(CONFIG_PATH, raw)


def make_backup(label: str = "manual", stamp: Optional[str] = None) -> Path:
    """Zip config/state/prefs into backups/. Login files (token/credentials) are deliberately not included."""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{label}-{stamp}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in BACKUP_FILES:
            if (BASE_DIR / name).exists():
                z.write(BASE_DIR / name, name)
    return path


def auto_backup(keep: int = 14, today: Optional[date] = None) -> Optional[Path]:
    """At most one automatic backup per day; older automatic ones beyond `keep` are pruned."""
    today = today or date.today()
    stamp = today.strftime("%Y%m%d")
    if (BACKUP_DIR / f"auto-{stamp}.zip").exists():
        return None
    path = make_backup("auto", stamp)
    for old in sorted(BACKUP_DIR.glob("auto-*.zip"), reverse=True)[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def latest_backup() -> Optional[Path]:
    files = sorted(BACKUP_DIR.glob("*.zip"), key=lambda f: f.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
    return files[0] if files else None


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"Missing {CONFIG_PATH.name}.\n"
            f"Copy {CONFIG_EXAMPLE_PATH.name} to config.json and edit it, then run this again."
        )
    try:
        cfg = read_json_safe(CONFIG_PATH)
    except DataFileError as e:
        sys.exit(str(e))
    cfg.setdefault("gmail_query", "from:dominos.com")
    cfg.setdefault("gmail_search_window_days", 120)
    cfg.setdefault("calendar_id", "primary")
    cfg.setdefault("event_title", "Dominos")
    cfg.setdefault("timezone", "America/Los_Angeles")
    cfg.setdefault("event_color_id", {})
    cfg.setdefault("reminder_minutes_before", [60, 30])
    cfg.setdefault("summary_email_to", None)
    cfg.setdefault("summary_email_subject_prefix", "Weekly Schedule")
    cfg.setdefault("store_name", "Domino's")

    # all-schedule weekly report settings
    cfg.setdefault("report_calendars", [cfg["calendar_id"]])
    cfg.setdefault("job_match", {"Dominos": "dominos", "Staples": "staples"})
    cfg.setdefault("job_wages", {"Dominos": 0, "Staples": 0})
    cfg.setdefault("school_course_code_regex", r"^[A-Za-z]{2,4}\s?-?\d{2,4}[A-Za-z]?\b")
    cfg.setdefault("school_extra_titles", [])
    cfg.setdefault("school_exceptions", [])
    cfg.setdefault("conflict_thresholds_minutes", {"very_close": 30, "close": 60})
    cfg.setdefault("break_ok_categories", ["Dominos"])
    cfg.setdefault("display_name", "")
    cfg.setdefault("tax_rate_percent", 0)
    cfg.setdefault("min_rest_hours", 8)
    cfg.setdefault("weekly_hours_goal", 0)
    cfg.setdefault("email_html", True)
    cfg.setdefault("pay_schedule", {"type": "off", "start": "", "delay_days": 0})  # the older single schedule
    cfg.setdefault("job_pay", {})  # {job: {"type", "start", "delay_days"}}: each job is paid on its own schedule
    return cfg


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.on_save = None  # the app sets this so a save can trigger a sync
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            data = read_json_safe(self.path)  # raises DataFileError rather than silently starting over
            for key, default in (("imported_shift_keys", []), ("weeks", {}), ("history", [])):
                data.setdefault(key, default)
            return data
        return {"imported_shift_keys": [], "weeks": {}, "history": []}

    def save(self):
        write_json_atomic(self.path, self.data)
        if self.on_save:
            self.on_save()

    def is_imported(self, week_key: str) -> bool:
        return week_key in self.data["weeks"]

    def week_entry(self, week_key: str) -> Optional[dict]:
        return self.data["weeks"].get(week_key)

    def add_week(self, week_key: str, entry: dict):
        self.data["weeks"][week_key] = entry
        self.data["imported_shift_keys"].extend(entry["shift_keys"])

    def log(self, action: str, **kwargs):
        self.data["history"].append(
            {"action": action, "timestamp": datetime.now().isoformat(timespec="seconds"), **kwargs}
        )

    def last_import(self) -> Optional[dict]:
        for entry in reversed(self.data["history"]):
            if entry["action"] == "import":
                if not entry.get("undone"):
                    return entry
        return None


def is_network_error(exc: BaseException) -> bool:
    """True for 'can't reach Google' problems (offline, DNS, timeouts), not for Google saying no (HTTP errors)."""
    try:
        import httplib2
        from google.auth.exceptions import TransportError
        kinds = (OSError, httplib2.HttpLib2Error, TransportError)
    except ImportError:
        kinds = (OSError,)
    if isinstance(exc, kinds):
        return True
    return "getaddrinfo" in str(exc).lower()


def get_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit(
            "Missing Google API packages. Run:\n"
            "    pip install -r requirements.txt"
        )

    bundled = RESOURCE_DIR / "credentials.json"
    if not CREDENTIALS_PATH.exists() and bundled.exists() and bundled != CREDENTIALS_PATH:
        shutil.copy(bundled, CREDENTIALS_PATH)  # the installer ships it so a new computer only has to sign in

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                if not is_network_error(e):
                    raise
                return creds  # offline: keep the saved login; requests will fail cleanly and fall back to cached data
        else:
            if not CREDENTIALS_PATH.exists():
                sys.exit(
                    f"Missing {CREDENTIALS_PATH.name}.\n"
                    "Download your OAuth client secret from Google Cloud Console "
                    "and save it as credentials.json in this folder. See setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_services():
    """Gmail + Calendar clients that give up after 30s instead of hanging forever."""
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    creds = get_credentials()

    def authed():
        return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))

    gmail = build("gmail", "v1", http=authed(), cache_discovery=False)
    calendar = build("calendar", "v3", http=authed(), cache_discovery=False)
    return gmail, calendar


RETRIES = 3  # transient network/5xx errors are retried for safe (read/idempotent) calls


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", "\n", markup)
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", markup))


def get_text_candidates(payload: dict) -> list:
    """Every text body in a message: plain-text parts first, then HTML parts converted to text."""
    plain, rich = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(html_to_text(_b64(data)))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    return plain + rich


@dataclass
class Shift:
    day_name: str
    shift_date: date
    role: str
    start_dt: datetime
    end_dt: datetime

    @property
    def hours(self) -> float:
        return round((self.end_dt - self.start_dt).total_seconds() / 3600, 2)

    @property
    def key(self) -> str:
        return f"{self.shift_date.isoformat()}|{self.start_dt.isoformat()}|{self.end_dt.isoformat()}|{self.role}"


@dataclass
class ParsedSchedule:
    email_id: str
    email_date: datetime
    employee_name: str
    store_number: str
    phone: Optional[str]
    week_start: date
    week_end: date
    shifts: list = field(default_factory=list)
    superseded: bool = False  # a newer email exists for the same week
    from_cache: bool = False  # Gmail couldn't be reached; this is the saved copy

    @property
    def week_key(self) -> str:
        return self.week_start.isoformat()

    @property
    def total_hours(self) -> float:
        return round(sum(s.hours for s in self.shifts), 2)


def parse_schedule_email(msg_id: str, email_date: datetime, body: str) -> Optional[ParsedSchedule]:
    body = (body or "").replace("\xa0", " ").replace("\r", "")
    header = HEADER_RE.search(body)
    week_phrase = WEEK_RE.search(body)
    phone_match = PHONE_RE.search(body)

    shifts = []
    for m in SHIFT_RE.finditer(body):
        shift_date = parse_mmddyyyy(m.group("date"))
        try:
            start_dt = datetime.combine(shift_date, parse_clock(m.group("start")))
            end_dt = datetime.combine(shift_date, parse_clock(m.group("end")))
        except ValueError:
            continue
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)  # shift crosses midnight
        shift = Shift(
            day_name=m.group("day").capitalize(),
            shift_date=shift_date,
            role=m.group("role").strip(),
            start_dt=start_dt,
            end_dt=end_dt,
        )
        if all(shift.key != other.key for other in shifts):  # an identical line listed twice is one shift
            shifts.append(shift)

    if not shifts:
        return None
    # If the header wording changes, fall back to any "week of X to Y" phrase, then to the week containing the first shift.
    if header:
        name, store = header.group("name").strip(), header.group("store")
        start, end = parse_mmddyyyy(header.group("start")), parse_mmddyyyy(header.group("end"))
    else:
        name, store = "", ""
        if week_phrase:
            start, end = parse_mmddyyyy(week_phrase.group("start")), parse_mmddyyyy(week_phrase.group("end"))
        else:
            first = min(sh.shift_date for sh in shifts)
            start = first - timedelta(days=first.weekday())
            end = start + timedelta(days=6)

    return ParsedSchedule(
        email_id=msg_id,
        email_date=email_date,
        employee_name=name,
        store_number=store,
        phone=phone_match.group(0) if phone_match else None,
        week_start=start,
        week_end=end,
        shifts=shifts,
    )


def schedule_to_dict(p: ParsedSchedule) -> dict:
    return {
        "email_id": p.email_id, "email_date": p.email_date.isoformat(), "employee_name": p.employee_name,
        "store_number": p.store_number, "phone": p.phone, "week_start": p.week_start.isoformat(),
        "week_end": p.week_end.isoformat(),
        "shifts": [{"day_name": s.day_name, "shift_date": s.shift_date.isoformat(), "role": s.role,
                    "start": s.start_dt.isoformat(), "end": s.end_dt.isoformat()} for s in p.shifts],
    }


def schedule_from_dict(d: dict) -> ParsedSchedule:
    return ParsedSchedule(
        email_id=d["email_id"], email_date=datetime.fromisoformat(d["email_date"]),
        employee_name=d["employee_name"], store_number=d["store_number"], phone=d.get("phone"),
        week_start=date.fromisoformat(d["week_start"]), week_end=date.fromisoformat(d["week_end"]),
        shifts=[Shift(day_name=x["day_name"], shift_date=date.fromisoformat(x["shift_date"]), role=x["role"],
                      start_dt=datetime.fromisoformat(x["start"]), end_dt=datetime.fromisoformat(x["end"]))
                for x in d["shifts"]],
    )


def _load_email_cache() -> dict:
    try:
        data = read_json_safe(EMAIL_CACHE_PATH)
        if data.get("version") == PARSER_VERSION:
            return data
    except (OSError, DataFileError):
        pass
    return {"version": PARSER_VERSION, "messages": {}}


def _save_email_cache(cache: dict) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        write_json_atomic(EMAIL_CACHE_PATH, cache)
    except OSError:
        pass  # the cache is only a speed-up


def find_schedule_emails(gmail, config: dict, limit: int) -> list:
    """Schedule emails, newest first. Older emails for a week that has a newer one are flagged `superseded`."""
    query = config["gmail_query"]
    window = config.get("gmail_search_window_days")
    if window:
        query = f"{query} newer_than:{int(window)}d"

    cache = _load_email_cache()
    offline = False
    try:
        resp = gmail.users().messages().list(userId="me", q=query, maxResults=limit).execute(num_retries=RETRIES)
        message_ids = [m["id"] for m in resp.get("messages", [])]
    except Exception as e:
        if not (is_network_error(e) and cache["messages"]):
            raise
        offline = True
        message_ids = list(cache["messages"])

    
    dirty = False
    results = []
    for mid in message_ids:
        entry = cache["messages"].get(mid)
        if entry is None and offline:
            continue
        if entry is None:
            msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute(num_retries=RETRIES)
            email_dt = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000)
            parsed = None
            for body in get_text_candidates(msg["payload"]):
                parsed = parse_schedule_email(mid, email_dt, body)
                if parsed:
                    break
            entry = {"sched": schedule_to_dict(parsed) if parsed else None}
            cache["messages"][mid] = entry
            dirty = True
        if entry["sched"]:
            sched = schedule_from_dict(entry["sched"])
            sched.from_cache = offline
            results.append(sched)
    if dirty:
        _save_email_cache(cache)

    results.sort(key=lambda p: p.email_date, reverse=True)
    seen = set()
    for p in results:
        if p.week_key in seen:
            p.superseded = True
        seen.add(p.week_key)
    return results


def build_raw_message(to: str, subject: str, body_text: str, html_body: Optional[str] = None) -> dict:
    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body_text, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject if subject.isascii() else Header(subject, "utf-8")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


def email_html(config: dict, report: dict) -> Optional[str]:
    """The HTML part of a report email, or None if the user turned styled emails off."""
    return report.get("html") if config.get("email_html", True) else None


def send_email(gmail, to: str, subject: str, body_text: str, html_body: Optional[str] = None) -> str:
    message = build_raw_message(to, subject, body_text, html_body)
    sent = gmail.users().messages().send(userId="me", body=message).execute()
    return sent["id"]


def event_title_for_shift(config: dict, hours: float) -> str:
    """The calendar title for a shift: the configured name plus its length, e.g. 'Dominos 3.5hr'."""
    base = (config.get("event_title") or "Dominos").strip()
    return f"{base} {hours:g}hr"


def shift_title_pattern(config: dict):
    """Matches a calendar title created by `event_title_for_shift` for this config, whatever the shift length."""
    base = re.escape((config.get("event_title") or "Dominos").strip())
    return re.compile(rf"^{base}\s+[\d.]+\s*hr$", re.IGNORECASE)


def create_calendar_event(calendar, config: dict, parsed: ParsedSchedule, shift: Shift) -> dict:
    color_id = (config.get("event_color_id") or {}).get(shift.role)
    store_name = config.get("store_name") or "Domino's"
    body = {
        "summary": event_title_for_shift(config, shift.hours),
        "description": (
            f"{shift.role} shift at {store_name}"
            + f"\nImported by Schedule Manager on {datetime.now():%m/%d/%Y %I:%M %p}"
            + f"\nSource email ID: {parsed.email_id}"
        ),
        "start": {"dateTime": shift.start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": config["timezone"]},
        "end": {"dateTime": shift.end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": config["timezone"]},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminder_list(config)],
        },
    }
    if color_id:
        body["colorId"] = str(color_id)
    return calendar.events().insert(calendarId=config["calendar_id"], body=body).execute()


def delete_calendar_event(calendar, calendar_id: str, event_id: str) -> bool:
    from googleapiclient.errors import HttpError

    try:
        calendar.events().delete(calendarId=calendar_id, eventId=event_id).execute(num_retries=RETRIES)
        return True
    except HttpError as e:
        return e.resp.status in (404, 410)  # already gone counts as deleted


DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CATEGORY_COLORS = {"Dominos": "#3b82f6", "Staples": "#ef4444", "School": "#a78bfa", "Other": "#64748b"}
EXTRA_CATEGORY_COLORS = ["#14b8a6", "#f59e0b", "#ec4899", "#84cc16", "#06b6d4"]


def category_color(label: str) -> str:
    if label in CATEGORY_COLORS:
        return CATEGORY_COLORS[label]
    return EXTRA_CATEGORY_COLORS[sum(map(ord, label)) % len(EXTRA_CATEGORY_COLORS)]


def fmt_date_short(d: date) -> str:
    return f"{d.month}/{d.day}"


def reminder_list(config: dict) -> list:
    """Reminder minutes from config: accepts 30, [60, 30] or '60, 30'. Largest first, max 5 (Google's limit)."""
    raw = config.get("reminder_minutes_before", [60, 30])
    if isinstance(raw, (int, float)):
        raw = [raw]
    elif isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    out = []
    for item in raw:
        try:
            v = int(float(str(item).strip()))
        except ValueError:
            continue
        if 0 <= v <= 40320 and v not in out:
            out.append(v)
    return sorted(out, reverse=True)[:5] or [30]


def describe_reminders(minutes: list) -> str:
    """[60, 30] -> '1 hour and 30 min'."""
    def one(m):
        if m and m % 60 == 0:
            h = m // 60
            return f"{h} hour" + ("s" if h != 1 else "")
        return f"{m} min"
    parts = [one(m) for m in minutes]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def split_needles(spec) -> list:
    """'staples, work' -> ['staples', 'work'] (lower-cased, blanks dropped)."""
    return [x.strip().lower() for x in str(spec or "").split(",") if x.strip()]


def classify_event(summary: str, config: dict) -> str:
    s = (summary or "").strip()
    sl = s.lower()

    if sl in {x.lower() for x in config.get("school_exceptions", [])}:
        return "Other"
    if sl in {x.lower() for x in config.get("school_extra_titles", [])}:
        return "School"

    for label, spec in (config.get("job_match") or {}).items():
        for needle in split_needles(spec):
            if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", sl):
                return label

    pattern = config.get("school_course_code_regex")
    if pattern:
        try:
            if re.match(pattern, s, re.IGNORECASE):
                return "School"
        except re.error:
            pass

    return "Other"


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_event(event: dict, tz: ZoneInfo, config: dict, calendar_id: str) -> dict:
    start = event.get("start", {})
    end = event.get("end", {})
    summary = event.get("summary") or "(no title)"

    if "dateTime" in start:
        start_dt = _parse_dt(start["dateTime"]).astimezone(tz).replace(tzinfo=None)
        end_dt = _parse_dt(end["dateTime"]).astimezone(tz).replace(tzinfo=None)
        all_day = False
        day = start_dt.date()
    else:
        start_dt = None
        end_dt = None
        all_day = True
        day = date.fromisoformat(start["date"])

    return {
        "id": event.get("id"),
        "summary": summary,
        "start": start_dt,
        "end": end_dt,
        "all_day": all_day,
        "day": day,
        "category": classify_event(summary, config),
        "calendar_id": calendar_id,
    }


def _declined_by_me(item: dict) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees") or [])


def fetch_week_events(calendar_service, config: dict, start_date: date, end_date: date) -> list:
    tz = ZoneInfo(config["timezone"])
    time_min = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).isoformat()
    time_max = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=tz).isoformat()

    events = []
    for cal_id in config.get("report_calendars") or ["primary"]:
        page_token = None
        while True:
            resp = (
                calendar_service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                )
                .execute(num_retries=RETRIES)
            )
            for item in resp.get("items", []):
                if item.get("status") == "cancelled" or _declined_by_me(item):
                    continue
                events.append(normalize_event(item, tz, config, cal_id))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return events


def search_events(calendar, config: dict, query: str, days_back: int = 120, days_forward: int = 365,
                  today: Optional[date] = None) -> list:
    """Events whose title/description/location contain `query`, oldest first (Google does the matching)."""
    query = (query or "").strip()
    if not query:
        return []
    today = today or date.today()
    tz = ZoneInfo(config["timezone"])
    time_min = datetime.combine(today - timedelta(days=days_back), datetime.min.time(), tzinfo=tz).isoformat()
    time_max = datetime.combine(today + timedelta(days=days_forward), datetime.min.time(), tzinfo=tz).isoformat()
    found = []
    for cal_id in config.get("report_calendars") or ["primary"]:
        page_token = None
        while True:
            resp = (
                calendar.events()
                .list(calendarId=cal_id, q=query, timeMin=time_min, timeMax=time_max, singleEvents=True,
                      orderBy="startTime", maxResults=250, pageToken=page_token)
                .execute(num_retries=RETRIES)
            )
            for item in resp.get("items", []):
                if item.get("status") != "cancelled" and not _declined_by_me(item):
                    found.append(normalize_event(item, tz, config, cal_id))
            page_token = resp.get("nextPageToken")
            if not page_token or len(found) >= 500:
                break
    found.sort(key=lambda e: (e["day"], e["start"] or datetime.min))
    return found


def group_events_by_day(events: list, start_date: date, end_date: date) -> dict:
    days = {}
    d = start_date
    while d <= end_date:
        days[d] = []
        d += timedelta(days=1)
    for e in events:
        days.setdefault(e["day"], []).append(e)
    for d in days:
        days[d].sort(key=lambda e: (0 if e["all_day"] else 1, e["start"] or datetime.min))
    return days


def find_day_conflicts(events_for_day: list, thresholds: dict, break_ok_categories: frozenset = frozenset()) -> list:
    timed = sorted([e for e in events_for_day if not e["all_day"]], key=lambda e: e["start"])
    very_close = thresholds.get("very_close", 30)
    close = thresholds.get("close", 60)
    conflicts = []
    for i in range(len(timed)):
        for j in range(i + 1, len(timed)):
            a, b = timed[i], timed[j]
            if b["start"] < a["end"]:
                conflicts.append({"type": "overlap", "a": a, "b": b, "gap": 0})
                continue
            same_job_break = a["category"] == b["category"] and a["category"] in break_ok_categories
            if same_job_break:
                continue  # e.g. two Dominos shifts split by a scheduled break, not a real conflict
            gap = (b["start"] - a["end"]).total_seconds() / 60
            if gap <= very_close:
                conflicts.append({"type": "very_close", "a": a, "b": b, "gap": gap})
            elif gap <= close:
                conflicts.append({"type": "close", "a": a, "b": b, "gap": gap})
    return conflicts


def find_rest_issues(days: dict, min_rest_hours: float) -> dict:
    """Short rest between one day's last event and the next day's first (e.g. a late close, then an 8 AM class)."""
    out = {d: [] for d in days}
    if not min_rest_hours or min_rest_hours <= 0:
        return out
    previous = None
    for d in sorted(days):
        timed = sorted([e for e in days[d] if not e["all_day"]], key=lambda e: e["start"])
        if not timed:
            continue
        if previous is not None and (d - previous["day"]).days == 1:
            gap_h = (timed[0]["start"] - previous["end"]).total_seconds() / 3600
            if 0 <= gap_h < min_rest_hours:
                out[d].append({"type": "short_rest", "a": previous, "b": timed[0], "gap": gap_h * 60})
        previous = max(timed, key=lambda e: e["end"])
    return out


def find_all_conflicts(days: dict, config: dict) -> dict:
    thresholds = config.get("conflict_thresholds_minutes", {"very_close": 30, "close": 60})
    break_ok = frozenset(config.get("break_ok_categories", ["Dominos"]))
    result = {d: find_day_conflicts(evs, thresholds, break_ok) for d, evs in days.items()}
    try:
        min_rest = float(config.get("min_rest_hours", 8) or 0)
    except (TypeError, ValueError):
        min_rest = 0
    for d, issues in find_rest_issues(days, min_rest).items():
        result[d] = result.get(d, []) + issues
    return result


def compute_category_hours(events: list) -> dict:
    totals = {}
    for e in events:
        if e["all_day"] or e["category"] == "Other":
            continue
        hours = (e["end"] - e["start"]).total_seconds() / 3600
        totals[e["category"]] = totals.get(e["category"], 0.0) + hours
    return totals


def describe_conflict(d: date, c: dict) -> str:
    a, b = c["a"], c["b"]
    dlabel = f"{DAY_ABBR[d.weekday()]} {fmt_date_short(d)}"
    if c["type"] == "overlap":
        return (f"OVERLAP {dlabel}: {a['summary']} {fmt_time(a['start'])}-{fmt_time(a['end'])} "
                f"/ {b['summary']} {fmt_time(b['start'])}-{fmt_time(b['end'])}")
    if c["type"] == "short_rest":
        return (f"SHORT REST ({c['gap'] / 60:.1f}h) {dlabel}: {a['summary']} ends {fmt_time(a['end'])} "
                f"{DAY_ABBR[a['end'].weekday()]}, {b['summary']} starts {fmt_time(b['start'])}")
    tag = "VERY CLOSE" if c["type"] == "very_close" else "CLOSE"
    return (f"{tag} ({int(c['gap'])}m) {dlabel}: {a['summary']} ends {fmt_time(a['end'])}, "
            f"{b['summary']} starts {fmt_time(b['start'])}")


def forget_event(state: "StateStore", event_id: str):
    """Drop a deleted calendar event from the import tracker so a re-import can add it back."""
    for week_key, entry in list(state.data["weeks"].items()):
        ids = entry.get("event_ids", [])
        if event_id not in ids:
            continue
        i = ids.index(event_id)
        ids.pop(i)
        keys = entry.get("shift_keys", [])
        key = keys.pop(i) if i < len(keys) else None
        if key and key in state.data["imported_shift_keys"]:
            state.data["imported_shift_keys"].remove(key)
        base = {k: v for k, v in entry.items() if k not in ("event_ids", "shift_keys")}
        if not ids:
            state.data["weeks"].pop(week_key)
        state.log("delete_event", week=week_key, event_ids=[event_id])
        state.save()
        return {"week_key": week_key, "key": key, "base": base}
    return None


def restore_tracking(state: "StateStore", info: dict, new_event_id: str) -> None:
    """Undo of forget_event: the restored calendar event is tracked again under its week."""
    entry = state.data["weeks"].get(info["week_key"])
    if entry is None:
        entry = dict(info["base"])
        entry["event_ids"], entry["shift_keys"] = [], []
        state.data["weeks"][info["week_key"]] = entry
    entry["event_ids"].append(new_event_id)
    if info.get("key"):
        entry["shift_keys"].append(info["key"])
        state.data["imported_shift_keys"].append(info["key"])
    state.log("restore_event", week=info["week_key"], event_ids=[new_event_id])
    state.save()


_SNAPSHOT_KEYS = ("summary", "description", "location", "start", "end", "reminders", "colorId", "transparency",
                  "visibility")


def delete_event_with_snapshot(calendar, calendar_id: str, event_id: str) -> Optional[dict]:
    """Delete an event but return a copy of it (for undo). None if it was already gone."""
    from googleapiclient.errors import HttpError

    try:
        original = calendar.events().get(calendarId=calendar_id, eventId=event_id).execute(num_retries=RETRIES)
    except HttpError as e:
        if e.resp.status in (404, 410):
            return None
        raise
    if not delete_calendar_event(calendar, calendar_id, event_id):
        raise RuntimeError("Google Calendar refused to delete that event. Try again in a moment.")
    return {k: original[k] for k in _SNAPSHOT_KEYS if k in original}


def move_event(calendar, calendar_id: str, event_id: str, start: datetime, end: datetime, tzname: str) -> dict:
    """Change an event's start/end (naive local times). Used by drag-to-move / drag-to-resize."""
    if end <= start:
        raise ValueError("The end must be after the start.")
    body = {"start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tzname},
            "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tzname}}
    return calendar.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute(num_retries=RETRIES)


def restore_event(calendar, calendar_id: str, snapshot: dict) -> dict:
    """Re-create a deleted event from delete_event_with_snapshot(). (Attendees are never re-invited.)"""
    return calendar.events().insert(calendarId=calendar_id, body=dict(snapshot)).execute()


def take_home(pay: float, config: dict) -> float:
    """Estimated pay after the configured tax withholding (tax_rate_percent, 0 = none)."""
    try:
        rate = min(max(float(config.get("tax_rate_percent") or 0), 0.0), 90.0)
    except (TypeError, ValueError):
        rate = 0.0
    return pay * (1 - rate / 100)


def paid_totals(category_hours: dict, config: dict) -> tuple:
    """(hours, pay) summed across job categories only (not School/Other)."""
    wages = config.get("job_wages") or {}
    jobs = set(config.get("job_match", {}).keys())
    hours = sum(h for label, h in category_hours.items() if label in jobs)
    pay = sum(h * wages.get(label, 0) for label, h in category_hours.items() if label in jobs)
    return hours, pay


def suggest_study_blocks(days: dict, goal_hours: float, config: Optional[dict] = None, min_minutes: int = 60,
                         max_minutes: int = 120, buffer_minutes: int = 30, window: tuple = (9, 21),
                         now: Optional[datetime] = None) -> list:
    """Pick study blocks in the free time around your events."""
    now = now or datetime.now()
    buffer = timedelta(minutes=buffer_minutes)
    candidates = []
    for day in sorted(days):
        if day < now.date():
            continue
        w_start = datetime.combine(day, datetime.min.time()).replace(hour=window[0])
        w_end = datetime.combine(day, datetime.min.time()).replace(hour=window[1])
        if day == now.date():
            soon = now + timedelta(minutes=30)
            soon += timedelta(minutes=(15 - soon.minute % 15) % 15)
            w_start = max(w_start, soon.replace(second=0, microsecond=0))
        if w_start >= w_end:
            continue
        timed = [e for e in days[day] if not e["all_day"]]
        load = sum((e["end"] - e["start"]).total_seconds() for e in timed) / 3600
        busy = sorted((max(e["start"] - buffer, w_start), min(e["end"] + buffer, w_end))
                      for e in timed if e["end"] + buffer > w_start and e["start"] - buffer < w_end)
        free, cursor = [], w_start
        for a, b in busy:
            if a > cursor:
                free.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < w_end:
            free.append((cursor, w_end))
        for a, b in free:
            a += timedelta(minutes=(15 - a.minute % 15) % 15)
            while (b - a).total_seconds() / 60 >= min_minutes:
                end = min(a + timedelta(minutes=max_minutes), b)
                candidates.append({"day": day, "start": a, "end": end, "load": load})
                a = end + buffer

    chosen, remaining, per_day = [], goal_hours * 60, {}
    while remaining >= 30 and candidates:
        candidates.sort(key=lambda c: (per_day.get(c["day"], 0), c["load"], c["start"]))
        pick = candidates.pop(0)
        minutes = (pick["end"] - pick["start"]).total_seconds() / 60
        if minutes > remaining:
            pick["end"] = pick["start"] + timedelta(minutes=max(30, -(-int(remaining) // 15) * 15))
            minutes = (pick["end"] - pick["start"]).total_seconds() / 60
        chosen.append(pick)
        per_day[pick["day"]] = per_day.get(pick["day"], 0) + 1
        remaining -= minutes
        candidates = [c for c in candidates if c["day"] != pick["day"] or c["start"] >= pick["end"] + buffer
                      or c["end"] <= pick["start"] - buffer]
    chosen.sort(key=lambda c: c["start"])
    return [{"day": c["day"], "start": c["start"], "end": c["end"],
             "hours": (c["end"] - c["start"]).total_seconds() / 3600} for c in chosen]


def find_free_slots(day_events: list, day: date, start_hour: int = 8, end_hour: int = 22,
                    min_minutes: int = 45) -> list:
    """Free gaps between start_hour and end_hour that aren't covered by any timed event."""
    window_start = datetime.combine(day, datetime.min.time()).replace(hour=start_hour)
    window_end = datetime.combine(day, datetime.min.time()).replace(hour=end_hour)
    busy = sorted(
        [(max(e["start"], window_start), min(e["end"], window_end))
         for e in day_events if not e["all_day"] and e["end"] > window_start and e["start"] < window_end],
        key=lambda x: x[0],
    )
    slots = []
    cursor = window_start
    for b_start, b_end in busy:
        if (b_start - cursor).total_seconds() / 60 >= min_minutes:
            slots.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if (window_end - cursor).total_seconds() / 60 >= min_minutes:
        slots.append((cursor, window_end))
    return slots


def compute_weekly_earnings(calendar_service, config: dict, weeks: int = 8, today: Optional[date] = None) -> list:
    """Per-week paid hours/pay for the last `weeks` weeks (including this one), oldest first."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    first_monday = this_monday - timedelta(weeks=weeks - 1)
    events, stale_at = fetch_events_or_cached(calendar_service, config, first_monday, this_monday + timedelta(days=6))

    rows = []
    for i in range(weeks):
        monday = first_monday + timedelta(weeks=i)
        sunday = monday + timedelta(days=6)
        week_events = [e for e in events if monday <= e["day"] <= sunday]
        cat_hours = compute_category_hours(week_events)
        hours, pay = paid_totals(cat_hours, config)
        rows.append({"week_start": monday, "week_end": sunday, "hours": hours, "pay": pay, "category_hours": cat_hours,
                     "label": fmt_date_short(monday), "current": monday == this_monday, "stale_at": stale_at})
    return rows


PAY_TYPES = ("off", "weekly", "biweekly", "semimonthly", "monthly")


def _pay_anchor(sched: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str(sched.get("start", "")))
    except ValueError:
        return None


def pay_period_bounds(day: date, sched: dict) -> Optional[tuple]:
    """(start, end) of the pay period containing `day`, or None if no pay schedule is set up."""
    kind = (sched or {}).get("type", "off")
    if kind in ("weekly", "biweekly"):
        anchor = _pay_anchor(sched)
        if anchor is None:
            return None
        length = 7 if kind == "weekly" else 14
        start = anchor + timedelta(days=((day - anchor).days // length) * length)
        return start, start + timedelta(days=length - 1)
    first = day.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if kind == "monthly":
        return first, last
    if kind == "semimonthly":
        return (first, first.replace(day=15)) if day.day <= 15 else (first.replace(day=16), last)
    return None


def payday_for(period_end: date, sched: dict) -> date:
    return period_end + timedelta(days=int(sched.get("delay_days") or 0))


def job_pay_schedule(config: dict, job: str) -> dict:
    """The pay schedule for one job. A schedule saved before schedules were per job applies to jobs that have none of their own."""
    own = (config.get("job_pay") or {}).get(job)
    return own if own is not None else (config.get("pay_schedule") or {})


def scheduled_jobs(config: dict) -> list:
    """Jobs that have a pay schedule, in the order they appear in settings."""
    return [j for j in config.get("job_match", {}) if job_pay_schedule(config, j).get("type", "off") != "off"]


def upcoming_paydays(config: dict, today: Optional[date] = None) -> list:
    """[(job, payday, (period_start, period_end))] for each job with a pay schedule, soonest payday first."""
    found = []
    for job in scheduled_jobs(config):
        nxt = next_payday(job_pay_schedule(config, job), today)
        if nxt:
            found.append((job, nxt[0], nxt[1]))
    return sorted(found, key=lambda item: item[1])


def next_payday(sched: dict, today: Optional[date] = None) -> Optional[tuple]:
    """(payday, (period_start, period_end)) for the next paycheck, or None."""
    today = today or date.today()
    bounds = pay_period_bounds(today, sched)
    if bounds is None:
        return None
    candidates = []
    for _ in range(4):  # the current period and a few before it (their paydays may still be ahead)
        candidates.append((payday_for(bounds[1], sched), bounds))
        bounds = pay_period_bounds(bounds[0] - timedelta(days=1), sched)
    upcoming = [c for c in candidates if c[0] >= today]
    return min(upcoming, key=lambda c: c[0]) if upcoming else None


def compute_pay_periods(calendar_service, config: dict, count: int = 6, today: Optional[date] = None,
                        job: Optional[str] = None) -> list:
    """Paid hours/pay for the last `count` pay periods (including the current one), oldest first, for one job's schedule
    (the first job that has one if none is named)."""
    today = today or date.today()
    if job is None:  # with only the older single schedule, every job is paid together on it
        job = next((j for j in scheduled_jobs(config) if j in (config.get("job_pay") or {})), None)
    sched = job_pay_schedule(config, job) if job else (config.get("pay_schedule") or {})
    current = pay_period_bounds(today, sched)
    if current is None:
        raise ValueError("Set up your pay schedule in Settings first.")
    periods = [current]
    while len(periods) < count:
        periods.insert(0, pay_period_bounds(periods[0][0] - timedelta(days=1), sched))
    events, stale_at = fetch_events_or_cached(calendar_service, config, periods[0][0], current[1])
    rows = []
    for start, end in periods:
        cat_hours = compute_category_hours([e for e in events if start <= e["day"] <= end and (job is None or e["category"] == job)])
        hours, pay = paid_totals(cat_hours, config)
        rows.append({"week_start": start, "week_end": end, "hours": hours, "pay": pay, "category_hours": cat_hours, "job": job,
                     "label": f"{start.month}/{start.day}-{end.month}/{end.day}", "current": (start, end) == current,
                     "payday": payday_for(end, sched), "stale_at": stale_at})
    return rows


def compute_monthly_earnings(calendar_service, config: dict, months: int = 6, today: Optional[date] = None) -> list:
    """Per-calendar-month paid hours/pay for the last `months` months (including this one), oldest first."""
    today = today or date.today()
    this_month = today.replace(day=1)

    def add_months(d: date, n: int) -> date:
        idx = d.year * 12 + d.month - 1 + n
        return date(idx // 12, idx % 12 + 1, 1)

    first = add_months(this_month, -(months - 1))
    last_day = add_months(this_month, 1) - timedelta(days=1)
    events, stale_at = fetch_events_or_cached(calendar_service, config, first, last_day)
    rows = []
    for i in range(months):
        ms = add_months(first, i)
        me = add_months(ms, 1) - timedelta(days=1)
        month_events = [e for e in events if ms <= e["day"] <= me]
        cat_hours = compute_category_hours(month_events)
        hours, pay = paid_totals(cat_hours, config)
        rows.append({"month_start": ms, "week_start": ms, "week_end": me, "hours": hours, "pay": pay,
                     "category_hours": cat_hours, "label": ms.strftime("%b"), "current": ms == this_month,
                     "stale_at": stale_at})
    return rows


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _ics_fold(line: str) -> list:
    """RFC 5545: lines are at most 75 octets; continuation lines start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, cur = [], ""
    limit = 75
    for ch in line:
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur, limit = " " + ch, 75
        else:
            cur += ch
    out.append(cur)
    return out


def export_events_ics(path, events: list, tzname: str, now: Optional[datetime] = None) -> int:
    """Write events as an .ics calendar file (times in UTC so no timezone definitions are needed)."""
    tz = ZoneInfo(tzname)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")

    def utc(dt: datetime) -> str:
        return dt.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Schedule Manager//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    n = 0
    for i, e in enumerate(sorted(events, key=lambda e: (e["day"], e["start"] or datetime.min))):
        uid = f"{e.get('id') or i}-{e['day'].isoformat()}@schedule-manager"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"]
        if e["all_day"]:
            lines += [f"DTSTART;VALUE=DATE:{e['day']:%Y%m%d}", f"DTEND;VALUE=DATE:{e['day'] + timedelta(days=1):%Y%m%d}"]
        else:
            lines += [f"DTSTART:{utc(e['start'])}", f"DTEND:{utc(e['end'])}"]
        lines += [f"SUMMARY:{_ics_escape(e['summary'])}", f"CATEGORIES:{_ics_escape(e['category'])}", "END:VEVENT"]
        n += 1
    lines.append("END:VCALENDAR")
    folded = [piece for line in lines for piece in _ics_fold(line)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(folded) + "\r\n")
    return n


def apply_reminders_to_imported(calendar, config: dict, state: "StateStore", today: Optional[date] = None) -> tuple:
    """Re-apply the configured reminders to upcoming shifts this app imported. Returns (updated, failed)."""
    from googleapiclient.errors import HttpError

    today = today or date.today()
    overrides = [{"method": "popup", "minutes": m} for m in reminder_list(config)]
    updated = failed = 0
    for entry in state.data["weeks"].values():
        if date.fromisoformat(entry["week_end"]) < today:
            continue
        for event_id in entry.get("event_ids", []):
            try:
                calendar.events().patch(
                    calendarId=entry.get("calendar_id", config["calendar_id"]), eventId=event_id,
                    body={"reminders": {"useDefault": False, "overrides": overrides}},
                ).execute(num_retries=RETRIES)
                updated += 1
            except HttpError:
                failed += 1
    return updated, failed


def create_manual_event(calendar, config: dict, title: str, start_dt: datetime, end_dt: datetime,
                        description: str = "", reminders: Optional[list] = None) -> dict:
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": config["timezone"]},
        "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": config["timezone"]},
    }
    if reminders:
        body["reminders"] = {"useDefault": False,
                             "overrides": [{"method": "popup", "minutes": int(m)} for m in reminders]}
    return calendar.events().insert(calendarId=config["calendar_id"], body=body).execute()


def export_events_csv(path, events: list):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Title", "Category", "Start", "End", "Hours"])
        for e in sorted(events, key=lambda e: (e["day"], e["start"] or datetime.min)):
            if e["all_day"]:
                writer.writerow([e["day"].isoformat(), e["summary"], e["category"], "all day", "", ""])
            else:
                hours = (e["end"] - e["start"]).total_seconds() / 3600
                writer.writerow([e["day"].isoformat(), e["summary"], e["category"],
                                 e["start"].strftime("%H:%M"), e["end"].strftime("%H:%M"), f"{hours:.2f}"])


def daily_paid_hours(events: list, config: dict) -> dict:
    """{day: hours worked at jobs} for the month view."""
    jobs = set(config.get("job_match", {}).keys())
    out: dict = {}
    for e in events:
        if not e["all_day"] and e["category"] in jobs:
            out[e["day"]] = round(out.get(e["day"], 0) + (e["end"] - e["start"]).total_seconds() / 3600, 2)
    return out


def next_week_bounds(today: Optional[date] = None) -> tuple:
    today = today or date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    return monday, monday + timedelta(days=6)


def this_week_bounds(today: Optional[date] = None) -> tuple:
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def build_weekly_report_email(week_start: date, week_end: date, days: dict, conflicts_by_day: dict,
                               category_hours: dict, config: dict) -> tuple:
    lines = [f"Weekly Schedule: {fmt_date(week_start)} - {fmt_date(week_end)}", ""]

    job_wages = config.get("job_wages") or {}
    job_labels = set(config.get("job_match", {}).keys())

    hour_bits = []
    for label, hrs in category_hours.items():
        wage = job_wages.get(label, 0)
        if wage:
            hour_bits.append(f"{label} {hrs:.2f}h (${hrs * wage:.2f})")
        else:
            hour_bits.append(f"{label} {hrs:.2f}h")
    paid_hours_total, paid_pay_total = paid_totals(category_hours, config)
    paid_categories_present = sum(1 for label in category_hours if label in job_labels)
    if hour_bits:
        lines.append(" | ".join(hour_bits))
        if paid_categories_present >= 2:
            lines.append(f"Total: {paid_hours_total:.2f}h (${paid_pay_total:.2f})")
        goal = float(config.get("weekly_hours_goal") or 0)
        if goal > 0 and paid_hours_total > goal:
            lines.append(f"Over your {goal:g}h goal by {paid_hours_total - goal:.2f}h")
        rate = float(config.get("tax_rate_percent") or 0)
        if rate > 0 and paid_pay_total > 0:
            lines.append(f"Take-home ~${take_home(paid_pay_total, config):.2f} after {rate:g}% tax")
        lines.append("")

    for d, evs in days.items():
        label = f"{DAY_ABBR[d.weekday()]} {fmt_date_short(d)}"
        if not evs:
            lines.append(f"{label}: -")
        else:
            parts = []
            for e in evs:
                if e["all_day"]:
                    parts.append(f"{e['summary']} (all day)")
                else:
                    parts.append(f"{e['summary']} {fmt_time(e['start'])}-{fmt_time(e['end'])}")
            lines.append(f"{label}: " + " | ".join(parts))
        lines.append("")

    any_conflicts = any(conflicts_by_day.values())
    if any_conflicts:
        lines.append("CONFLICTS:")
        for d in sorted(conflicts_by_day):
            for c in conflicts_by_day[d]:
                lines.append(f"- {describe_conflict(d, c)}")
    else:
        lines.append("No conflicts.")

    subject = f"{config.get('summary_email_subject_prefix', 'Weekly Schedule')} {fmt_date(week_start)}-{fmt_date(week_end)}"
    return subject, "\n".join(lines)


def print_schedule(parsed: ParsedSchedule, config: dict, already_imported: set):
    store_name = config.get("store_name") or "Domino's"
    print(f"\nEmployee: {parsed.employee_name}")
    print(f"Store:    {store_name} (#{parsed.store_number})")
    print(f"Week of:  {fmt_date(parsed.week_start)} to {fmt_date(parsed.week_end)}")
    print(f"Email received: {parsed.email_date:%m/%d/%Y %I:%M %p}")
    print()
    for s in parsed.shifts:
        dup = "  [already on calendar]" if s.key in already_imported else ""
        print(
            f"  {s.day_name:<10} {fmt_date(s.shift_date):<10} {s.role:<10} "
            f"{fmt_time(s.start_dt)} - {fmt_time(s.end_dt)}  ({s.hours:.2f} hrs){dup}"
        )
    print()
    print(f"Total hours: {parsed.total_hours:.2f}")
    wage = (config.get("job_wages") or {}).get("Dominos", 0)
    if wage:
        print(f"Estimated pay: ${parsed.total_hours * wage:.2f} (before taxes)")


def confirm(prompt: str, default_yes: bool = False) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    answer = input(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def cmd_sync(args):
    import sync
    config = load_config()
    if not sync.enabled(config):
        print("Sync is turned off (sync_enabled is false in config.json).")
        return
    _, calendar = build_services()
    result = sync.sync_once(calendar, tzname=config["timezone"])
    if result.first_upload:
        print("Uploaded this computer's settings and history. Other computers will pick them up when they sync.")
    elif result.pulled:
        print("Updated from your other computers: " + ", ".join(result.pulled) + ".")
    elif result.pushed:
        print("Sent this computer's changes to your other computers.")
    else:
        print("Already in sync.")
    if result.interrupted:
        print("(A file changed while syncing; run it again in a moment.)")


def cmd_check(args):
    config = load_config()
    print("Config loaded OK.")
    gmail, calendar = build_services()
    profile = gmail.users().getProfile(userId="me").execute()
    print(f"Gmail auth OK - signed in as {profile['emailAddress']}")
    cal = calendar.calendarList().get(calendarId=config["calendar_id"]).execute()
    print(f"Calendar auth OK - target calendar: {cal.get('summary', config['calendar_id'])}")
    print("\nEverything is set up correctly.")


def cmd_find(args):
    config = load_config()
    state = StateStore(STATE_PATH)
    gmail, _ = build_services()
    limit = args.limit or 15
    results = find_schedule_emails(gmail, config, limit)

    if not results:
        print("No Domino's schedule emails found matching your config's gmail_query.")
        return

    print(f"Found {len(results)} schedule email(s):\n")
    for i, p in enumerate(results):
        tag = " [imported]" if state.is_imported(p.week_key) else ""
        print(
            f"  [{i}] Week of {fmt_date(p.week_start)}-{fmt_date(p.week_end)}  "
            f"({p.total_hours:.2f} hrs, {len(p.shifts)} shifts, received {p.email_date:%m/%d/%Y}){tag}"
        )


def _select_schedule(results: list, state: StateStore, args) -> ParsedSchedule:
    if args.index is not None:
        if not (0 <= args.index < len(results)):
            sys.exit(f"--index {args.index} is out of range (0-{len(results) - 1}).")
        return results[args.index]

    if args.week:
        target = parse_mmddyyyy(args.week)
        for p in results:
            if p.week_start == target:
                return p
        sys.exit(f"No schedule email found for the week of {args.week}.")

    if len(results) == 1 or args.yes:
        return results[0]

    print("Multiple schedule emails found:\n")
    for i, p in enumerate(results):
        tag = " [already imported]" if state.is_imported(p.week_key) else ""
        print(
            f"  [{i}] Week of {fmt_date(p.week_start)}-{fmt_date(p.week_end)}  "
            f"({p.total_hours:.2f} hrs, received {p.email_date:%m/%d/%Y}){tag}"
        )
    choice = input(f"\nSelect a week [0-{len(results) - 1}] (Enter = newest, index 0): ").strip()
    if not choice:
        return results[0]
    try:
        idx = int(choice)
        return results[idx]
    except (ValueError, IndexError):
        sys.exit("Invalid selection.")


def _events_cache_key(config: dict, week_start: date, week_end: date) -> str:
    return f"{','.join(config.get('report_calendars') or ['primary'])}|{week_start.isoformat()}|{week_end.isoformat()}"


def _save_events_cache(config: dict, week_start: date, week_end: date, events: list) -> None:
    try:
        try:
            cache = read_json_safe(EVENTS_CACHE_PATH)
        except (OSError, DataFileError):
            cache = {}
        packed = [{**e, "start": e["start"].isoformat() if e["start"] else None,
                   "end": e["end"].isoformat() if e["end"] else None, "day": e["day"].isoformat()} for e in events]
        cache[_events_cache_key(config, week_start, week_end)] = {"saved": datetime.now().isoformat(timespec="seconds"),
                                                                 "events": packed}
        for key in sorted(cache, key=lambda k: cache[k]["saved"], reverse=True)[40:]:
            del cache[key]
        CACHE_DIR.mkdir(exist_ok=True)
        write_json_atomic(EVENTS_CACHE_PATH, cache)
    except OSError:
        pass  # only a convenience


def _load_events_cache(config: dict, week_start: date, week_end: date):
    try:
        entry = read_json_safe(EVENTS_CACHE_PATH).get(_events_cache_key(config, week_start, week_end))
    except (OSError, DataFileError):
        return None
    if not entry:
        return None
    events = [{**e, "start": datetime.fromisoformat(e["start"]) if e["start"] else None,
               "end": datetime.fromisoformat(e["end"]) if e["end"] else None, "day": date.fromisoformat(e["day"])}
              for e in entry["events"]]
    return events, datetime.fromisoformat(entry["saved"])


def build_weekly_report_html(week_start: date, week_end: date, days: dict, conflicts_by_day: dict,
                             category_hours: dict, config: dict) -> str:
    """The weekly report as an email-safe HTML page (inline styles, tables, no external files)."""
    esc = htmllib.escape
    muted, text, card, line = "#8b93a9", "#e6e9f2", "#171c2b", "#252c40"
    wages = config.get("job_wages") or {}

    def dot(color, size=8):
        return (f'<span style="display:inline-block;width:{size}px;height:{size}px;border-radius:{size // 2}px;'
                f'background:{color};margin-right:8px"></span>')

    chips = ""
    for label, hrs in category_hours.items():
        wage = wages.get(label, 0)
        detail = f"{hrs:.2f}h" + (f" &middot; ${hrs * wage:,.2f}" if wage else "")
        chips += (f'<span style="display:inline-block;margin:0 8px 8px 0;padding:6px 12px;border-radius:14px;'
                  f'background:{line};font-size:13px">{dot(category_color(label))}{esc(label)} {detail}</span>')

    hours, pay = paid_totals(category_hours, config)
    notes = []
    jobs_with_hours = sum(1 for label in category_hours if label in (config.get("job_match") or {}))
    if jobs_with_hours >= 2:
        notes.append(f"Total {hours:.2f}h &middot; ${pay:,.2f}")
    rate = float(config.get("tax_rate_percent") or 0)
    if rate > 0 and pay > 0:
        notes.append(f"Take-home about ${take_home(pay, config):,.2f} after {rate:g}% tax")
    goal = float(config.get("weekly_hours_goal") or 0)
    if goal > 0 and hours > goal:
        notes.append(f'<span style="color:#fbbf24">Over your {goal:g}h goal by {hours - goal:.2f}h</span>')
    notes_html = "".join(f'<div style="font-size:13px;color:{muted};margin-top:2px">{n}</div>' for n in notes)

    rows = ""
    for d, evs in days.items():
        if evs:
            body = ""
            for e in evs:
                when = "All day" if e["all_day"] else fmt_range(e["start"], e["end"])
                body += (f'<div style="margin:0 0 6px;font-size:14px">{dot(category_color(e["category"]))}'
                         f'<b>{esc(e["summary"])}</b> <span style="color:{muted}">{when}</span></div>')
        else:
            body = f'<div style="color:{muted};font-size:14px">Nothing scheduled</div>'
        rows += (f'<tr><td valign="top" width="86" style="padding:12px 0 6px 24px;border-top:1px solid {line}">'
                 f'<div style="font-weight:700;font-size:14px">{DAY_ABBR[d.weekday()]}</div>'
                 f'<div style="color:{muted};font-size:12px">{fmt_date_short(d)}</div></td>'
                 f'<td valign="top" style="padding:12px 24px 6px 8px;border-top:1px solid {line}">{body}</td></tr>')

    issues = [(d, c) for d in sorted(conflicts_by_day) for c in conflicts_by_day[d]]
    if issues:
        items = ""
        for d, c in issues:
            color = "#f87171" if c["type"] == "overlap" else "#fbbf24"
            items += (f'<div style="border-left:3px solid {color};padding:2px 0 2px 10px;margin:6px 0;font-size:13px">'
                      f'{esc(describe_conflict(d, c))}</div>')
        conflicts_html = (f'<div style="font-weight:700;margin-bottom:4px">Heads up</div>{items}')
    else:
        conflicts_html = '<div style="color:#34d399;font-size:14px">No conflicts this week.</div>'

    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#0d1017">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0d1017">'
        f'<tr><td align="center" style="padding:24px 10px;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:{text}">'
        f'<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background:{card};border-radius:14px;'
        'max-width:100%">'
        f'<tr><td colspan="2" style="padding:24px 24px 6px"><div style="font-size:12px;letter-spacing:1.5px;color:{muted}">'
        f'WEEKLY SCHEDULE</div><div style="font-size:22px;font-weight:700;margin-top:4px">'
        f'{week_start:%b} {week_start.day} - {week_end:%b} {week_end.day}, {week_end.year}</div></td></tr>'
        f'<tr><td colspan="2" style="padding:12px 24px 12px">{chips}{notes_html}</td></tr>'
        f'{rows}'
        f'<tr><td colspan="2" style="padding:16px 24px 24px;border-top:1px solid {line}">{conflicts_html}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def fetch_events_or_cached(calendar, config: dict, week_start: date, week_end: date, allow_stale: bool = True) -> tuple:
    """(events, stale_at). Live data normally; if Google can't be reached, the last saved copy of that week (stale_at says when it was saved). Raises if offline with nothing saved."""
    try:
        events = fetch_week_events(calendar, config, week_start, week_end)
        _save_events_cache(config, week_start, week_end, events)
        return events, None
    except Exception as e:
        cached = _load_events_cache(config, week_start, week_end) if allow_stale and is_network_error(e) else None
        if cached is None:
            raise
        return cached


def generate_weekly_report(calendar, config: dict, week_start: date, week_end: date, allow_stale: bool = True) -> dict:
    """The full weekly report. If Google can't be reached, falls back to the last saved copy of that week (report['stale_at'] says when it was saved; None means live data)."""
    events, stale_at = fetch_events_or_cached(calendar, config, week_start, week_end, allow_stale)
    days = group_events_by_day(events, week_start, week_end)
    conflicts = find_all_conflicts(days, config)
    category_hours = compute_category_hours(events)
    subject, body = build_weekly_report_email(week_start, week_end, days, conflicts, category_hours, config)
    html = build_weekly_report_html(week_start, week_end, days, conflicts, category_hours, config)
    return {
        "events": events, "days": days, "conflicts": conflicts,
        "category_hours": category_hours, "subject": subject, "body": body, "html": html, "stale_at": stale_at,
    }


def existing_shift_slots(calendar, config: dict, week_start: date, week_end: date) -> set:
    """(start, end) of every event already on the calendar titled like a Dominos shift, for duplicate protection."""
    pattern = shift_title_pattern(config)
    events = fetch_week_events(calendar, config, week_start - timedelta(days=1), week_end + timedelta(days=1))
    return {(e["start"], e["end"]) for e in events if not e["all_day"] and pattern.match(e["summary"].strip())}


def diff_schedule(state: StateStore, parsed: ParsedSchedule, existing_slots: frozenset = frozenset()) -> dict:
    """What importing `parsed` would do: new shifts, shifts already there, and tracked shifts the email dropped."""
    tracked = set(state.data["imported_shift_keys"])
    entry = state.week_entry(parsed.week_key) or {}
    parsed_keys = {s.key for s in parsed.shifts}
    new, already = [], []
    for s in parsed.shifts:
        (already if s.key in tracked or (s.start_dt, s.end_dt) in existing_slots else new).append(s)
    removed = [k for k in entry.get("shift_keys", []) if k not in parsed_keys]
    return {"new": new, "already": already, "removed": removed}


def schedule_status(state: StateStore, parsed: ParsedSchedule) -> str:
    """'older' (a newer email exists for that week), 'new', 'changed' (imported but the email differs), 'imported'."""
    if parsed.superseded:
        return "older"
    if not state.is_imported(parsed.week_key):
        return "new"
    d = diff_schedule(state, parsed)
    return "changed" if d["new"] or d["removed"] else "imported"


def analyze_import(calendar, config: dict, state: StateStore, parsed: ParsedSchedule) -> dict:
    """Preview of importing `parsed`: what is new / already there / dropped, and which conflicts the new shifts would create with everything else on the calendar. Read-only."""
    events, stale_at = fetch_events_or_cached(calendar, config, parsed.week_start, parsed.week_end)
    pattern = shift_title_pattern(config)
    slots = frozenset((e["start"], e["end"]) for e in events if not e["all_day"] and pattern.match(e["summary"].strip()))
    diff = diff_schedule(state, parsed, slots)
    synthetic = [
        {"id": f"new:{i}", "summary": event_title_for_shift(config, s.hours), "start": s.start_dt, "end": s.end_dt,
         "all_day": False, "day": s.shift_date, "category": "Dominos", "calendar_id": config["calendar_id"]}
        for i, s in enumerate(diff["new"])
    ]
    days = group_events_by_day(events + synthetic, parsed.week_start, parsed.week_end)
    conflicts = find_all_conflicts(days, config)
    involved = [
        (d, c) for d in sorted(conflicts) for c in conflicts[d]
        if str(c["a"]["id"]).startswith("new:") or str(c["b"]["id"]).startswith("new:")
    ]
    return {"diff": diff, "conflicts": involved, "events": events, "stale_at": stale_at}


def _forget_shift(state: StateStore, entry: dict, index: int) -> None:
    entry["event_ids"].pop(index)
    key = entry["shift_keys"].pop(index)
    if key in state.data["imported_shift_keys"]:
        state.data["imported_shift_keys"].remove(key)


def perform_import(gmail, calendar, config: dict, state: StateStore, parsed: ParsedSchedule,
                    force: bool = False, send_report: bool = True, log=print, remove_stale: bool = False) -> dict:
    """Create calendar events for a parsed schedule and (optionally) email the full weekly report."""
    slots = frozenset() if force else frozenset(existing_shift_slots(calendar, config, parsed.week_start, parsed.week_end))
    tracked = set(state.data["imported_shift_keys"])
    new_shifts, skipped, on_calendar = [], 0, 0
    for s in parsed.shifts:
        if force:
            new_shifts.append(s)
        elif s.key in tracked:
            skipped += 1
        elif (s.start_dt, s.end_dt) in slots:
            on_calendar += 1
            log(f"= Already on calendar: {s.day_name} {fmt_date(s.shift_date)} {fmt_time(s.start_dt)}-{fmt_time(s.end_dt)}")
        else:
            new_shifts.append(s)

    created_events, failure = [], None
    try:
        for s in new_shifts:
            ev = create_calendar_event(calendar, config, parsed, s)
            created_events.append({"event_id": ev["id"], "htmlLink": ev.get("htmlLink"), "shift_key": s.key})
            log(f"+ Created: {s.day_name} {fmt_date(s.shift_date)} {fmt_time(s.start_dt)}-{fmt_time(s.end_dt)}")
    except Exception as e:
        failure = e

    removed = 0
    entry_now = state.week_entry(parsed.week_key)
    if remove_stale and failure is None and entry_now:
        parsed_keys = {s.key for s in parsed.shifts}
        for i in reversed(range(len(entry_now["shift_keys"]))):
            if entry_now["shift_keys"][i] not in parsed_keys:
                eid = entry_now["event_ids"][i]
                if delete_calendar_event(calendar, entry_now["calendar_id"], eid):
                    _forget_shift(state, entry_now, i)
                    removed += 1
                    log("- Removed a shift that is no longer in the schedule")
        if not entry_now["event_ids"]:
            state.data["weeks"].pop(parsed.week_key, None)

    if created_events or removed:
        entry = state.week_entry(parsed.week_key)
        wage = (config.get("job_wages") or {}).get("Dominos", 0)
        if entry:
            entry["event_ids"].extend(e["event_id"] for e in created_events)
            entry["shift_keys"].extend(e["shift_key"] for e in created_events)
            state.data["imported_shift_keys"].extend(e["shift_key"] for e in created_events)
            entry["imported_at"] = datetime.now().isoformat(timespec="seconds")
            entry["total_hours"] = parsed.total_hours
            entry["estimated_pay"] = parsed.total_hours * wage
        elif created_events:
            state.add_week(parsed.week_key, {
                "email_id": parsed.email_id,
                "week_start": parsed.week_start.isoformat(),
                "week_end": parsed.week_end.isoformat(),
                "imported_at": datetime.now().isoformat(timespec="seconds"),
                "calendar_id": config["calendar_id"],
                "event_ids": [e["event_id"] for e in created_events],
                "shift_keys": [e["shift_key"] for e in created_events],
                "summary_email_id": None,
                "total_hours": parsed.total_hours,
                "estimated_pay": parsed.total_hours * wage,
            })
        state.log("import", week=parsed.week_key, event_ids=[e["event_id"] for e in created_events],
                  removed=removed)
        state.save()

    if failure is not None:
        raise RuntimeError(
            f"Only {len(created_events)} of {len(new_shifts)} shifts were added before an error "
            f"({failure}). The ones that were added are saved and can be undone."
        ) from failure

    report, summary_email_id, email_error = None, None, None
    if send_report:
        to = config.get("summary_email_to")
        if to:
            try:
                report = generate_weekly_report(calendar, config, parsed.week_start, parsed.week_end, allow_stale=False)
                log(report["body"])
                summary_email_id = send_email(gmail, to, report["subject"], report["body"], email_html(config, report))
                log(f"Report emailed to {to}.")
                entry = state.week_entry(parsed.week_key)
                if entry and summary_email_id:
                    entry["summary_email_id"] = summary_email_id
                    state.save()
            except Exception as e:
                email_error = str(e)
                log(f"Shifts were added, but the report email failed: {e}")
        else:
            log("summary_email_to not set in config.json - skipping email.")

    return {"created_events": created_events, "skipped": skipped, "on_calendar": on_calendar, "removed": removed,
            "report": report, "email_error": email_error}


def cmd_import(args):
    config = load_config()
    state = StateStore(STATE_PATH)
    gmail, calendar = build_services()

    results = find_schedule_emails(gmail, config, args.limit or 15)
    if not results:
        print("No Domino's schedule emails found matching your config's gmail_query.")
        return

    parsed = _select_schedule(results, state, args)
    already_imported = set(state.data["imported_shift_keys"])

    print_schedule(parsed, config, already_imported)
    if parsed.superseded:
        print("\nNOTE: a newer schedule email exists for this week.")

    # Look at the calendar first: what is new, what is already there, what an updated schedule dropped
    analysis = analyze_import(calendar, config, state, parsed)
    diff = analysis["diff"]
    remove_stale = not getattr(args, "keep_dropped", False)
    new_count = len(parsed.shifts) if args.force else len(diff["new"])
    print(f"\nWhat will happen: {new_count} new shift(s), {len(diff['already'])} already on your calendar"
          + (f", {len(diff['removed'])} dropped from the schedule ({'removed' if remove_stale else 'kept'})"
             if diff["removed"] else ""))
    for day, conflict in analysis["conflicts"]:
        print(f"  ! {describe_conflict(day, conflict)}")

    if args.dry_run:
        if new_count:
            example = event_title_for_shift(config, diff["new"][0].hours)
            print(f"\n[dry run] Would create {new_count} calendar event(s) titled like \"{example}\"")
        if not args.no_email:
            print("[dry run] Would build and send the full weekly report email.")
        return

    if new_count:
        prompt = f"\nImport this schedule ({new_count} shift(s)) to Google Calendar and email you the full week's report?"
    elif diff["removed"] and remove_stale:
        prompt = "\nRemove the dropped shift(s) and email this week's report?"
    else:
        print("\nAll shifts in this email are already on your calendar.")
        prompt = "No new shifts to add. Still generate and send this week's full schedule report?"

    if not args.yes and not confirm(prompt):
        print("Cancelled.")
        return

    result = perform_import(gmail, calendar, config, state, parsed, force=args.force,
                             send_report=not args.no_email, log=print, remove_stale=remove_stale)
    extra = f", {result['removed']} removed" if result["removed"] else ""
    print(f"\nDone. {len(result['created_events'])} event(s) added{extra} for the week of {fmt_date(parsed.week_start)}.")
    if result.get("email_error"):
        print(f"Warning: the report email failed: {result['email_error']}")


def cmd_search(args):
    config = load_config()
    _, calendar = build_services()
    found = search_events(calendar, config, " ".join(args.text))
    if not found:
        print("No matches.")
        return
    for e in found:
        when = "all day" if e["all_day"] else fmt_time(e["start"])
        print(f"  {DAY_ABBR[e['day'].weekday()]} {fmt_date(e['day']):<11} {when:<9} {e['summary']}  [{e['category']}]")
    print(f"\n{len(found)} match(es).")


def _delete_week_events(calendar, state: StateStore, week_key: str, log) -> int:
    """Delete a tracked week's events. Anything that could not be deleted stays tracked (never orphaned)."""
    entry = state.week_entry(week_key)
    deleted = 0
    for i in reversed(range(len(entry["event_ids"]))):
        if delete_calendar_event(calendar, entry["calendar_id"], entry["event_ids"][i]):
            _forget_shift(state, entry, i)
            deleted += 1
            log("Deleted an event")
    if entry["event_ids"]:
        state.save()
        raise RuntimeError(f"Deleted {deleted}, but {len(entry['event_ids'])} event(s) could not be deleted. "
                           "They are still tracked - try again.")
    state.data["weeks"].pop(week_key, None)
    return deleted


def perform_delete_week(calendar, state: StateStore, week_key: str, log=print) -> Optional[int]:
    if not state.week_entry(week_key):
        return None
    ids = list(state.week_entry(week_key)["event_ids"])
    deleted = _delete_week_events(calendar, state, week_key, log)
    state.log("delete", week=week_key, event_ids=ids)
    state.save()
    return deleted


def perform_undo(calendar, state: StateStore, log=print) -> Optional[tuple]:
    last = state.last_import()
    if not last:
        return None
    week_key = last["week"]
    if not state.week_entry(week_key):
        return None
    ids = list(state.week_entry(week_key)["event_ids"])
    deleted = _delete_week_events(calendar, state, week_key, log)
    last["undone"] = True
    state.log("undo", week=week_key, event_ids=ids)
    state.save()
    return week_key, deleted


def perform_reset(calendar, state: StateStore, log=print) -> tuple:
    weeks = list(state.data["weeks"])
    total_events = 0
    for week_key in weeks:
        total_events += _delete_week_events(calendar, state, week_key, log)
        log(f"Cleared week of {fmt_date(date.fromisoformat(week_key))}")
    state.data = {"imported_shift_keys": [], "weeks": {}, "history": []}
    state.log("reset")
    state.save()
    return len(weeks), total_events


def cmd_delete(args):
    state = StateStore(STATE_PATH)

    week_key = parse_mmddyyyy(args.week).isoformat()
    entry = state.week_entry(week_key)
    if not entry:
        sys.exit(f"No tracked import found for the week of {args.week}. (see `history` or `find`)")

    print(f"Week of {args.week}: {len(entry['event_ids'])} calendar event(s) will be deleted.")
    if not args.yes and not confirm("Proceed?"):
        print("Cancelled.")
        return

    _, calendar = build_services()
    count = perform_delete_week(calendar, state, week_key, log=print)
    print(f"Deleted {count} event(s) for the week of {args.week}.")


def cmd_undo(args):
    state = StateStore(STATE_PATH)
    last = state.last_import()
    if not last:
        print("Nothing to undo.")
        return

    entry = state.week_entry(last["week"])
    if not entry:
        print("The last import has already been undone or removed.")
        return

    print(f"This will undo the import for the week of {fmt_date(date.fromisoformat(last['week']))}: "
          f"{len(entry['event_ids'])} calendar event(s) will be deleted.")
    if not args.yes and not confirm("Proceed?"):
        print("Cancelled.")
        return

    _, calendar = build_services()
    perform_undo(calendar, state, log=print)
    print("Undo complete.")


def cmd_reset(args):
    state = StateStore(STATE_PATH)
    weeks = list(state.data["weeks"].items())
    if not weeks:
        print("Nothing tracked - there is nothing to reset.")
        return

    total_events = sum(len(e["event_ids"]) for _, e in weeks)
    print(f"This will delete ALL {total_events} calendar event(s) this tool has ever created, "
          f"across {len(weeks)} week(s), and wipe its local history.")
    if not args.yes:
        typed = input("Type RESET to confirm: ").strip()
        if typed != "RESET":
            print("Cancelled.")
            return

    _, calendar = build_services()
    perform_reset(calendar, state, log=print)
    print("\nEverything has been undone. state.json is back to a clean slate.")


def cmd_history(args):
    state = StateStore(STATE_PATH)
    if not state.data["history"]:
        print("No history yet.")
        return
    for entry in state.data["history"]:
        ts = entry["timestamp"]
        action = entry["action"]
        if action in ("import", "delete", "undo"):
            week = entry.get("week", "?")
            n = len(entry.get("event_ids", []))
            undone = " (undone)" if entry.get("undone") else ""
            print(f"  {ts}  {action:<8} week={week}  events={n}{undone}")
        else:
            print(f"  {ts}  {action}")


def cmd_report(args):
    config = load_config()

    if args.start and args.end:
        week_start, week_end = parse_mmddyyyy(args.start), parse_mmddyyyy(args.end)
    elif args.week:
        week_start = parse_mmddyyyy(args.week)
        week_end = week_start + timedelta(days=6)
    elif args.this_week:
        week_start, week_end = this_week_bounds()
    else:
        week_start, week_end = next_week_bounds()

    gmail, calendar = build_services()
    report = generate_weekly_report(calendar, config, week_start, week_end)
    print(report["body"])

    if args.dry_run:
        return

    to = config.get("summary_email_to")
    if not to:
        print("\n(summary_email_to not set in config.json - skipping email)")
        return

    if not args.yes and not confirm("\nEmail this report to yourself?"):
        print("Cancelled.")
        return

    send_email(gmail, to, report["subject"], report["body"], email_html(config, report))
    print(f"Report emailed to {to}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import Domino's work schedule emails into Google Calendar.")
    sub = parser.add_subparsers(dest="command")

    p_import = sub.add_parser("import", help="Import a schedule email into Google Calendar (default command)")
    p_import.add_argument("--index", type=int, help="Pick the Nth email from `find` (0 = newest)")
    p_import.add_argument("--week", help="Pick the email for a specific week, e.g. 9/21/2026")
    p_import.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    p_import.add_argument("--dry-run", action="store_true", help="Show what would happen without doing it")
    p_import.add_argument("--no-email", action="store_true", help="Don't send the summary email")
    p_import.add_argument("--force", action="store_true", help="Re-create events even if already imported")
    p_import.add_argument("--keep-dropped", action="store_true",
                          help="If an updated schedule dropped shifts, leave them on the calendar")
    p_import.add_argument("--limit", type=int, help="How many recent emails to search (default 15)")
    p_import.set_defaults(func=cmd_import)

    p_find = sub.add_parser("find", help="List Domino's schedule emails found in Gmail")
    p_find.add_argument("--limit", type=int, help="How many recent emails to search (default 15)")
    p_find.set_defaults(func=cmd_find)

    p_delete = sub.add_parser("delete", help="Delete a previously imported week from your calendar")
    p_delete.add_argument("--week", required=True, help="Week to delete, e.g. 9/21/2026")
    p_delete.add_argument("--yes", "-y", action="store_true")
    p_delete.set_defaults(func=cmd_delete)

    p_undo = sub.add_parser("undo", help="Undo the most recent import")
    p_undo.add_argument("--yes", "-y", action="store_true")
    p_undo.set_defaults(func=cmd_undo)

    p_reset = sub.add_parser("reset", help="Undo EVERYTHING this tool has ever done (for testing)")
    p_reset.add_argument("--yes", "-y", action="store_true")
    p_reset.set_defaults(func=cmd_reset)

    p_history = sub.add_parser("history", help="Show a log of imports/deletes/undos")
    p_history.set_defaults(func=cmd_history)

    p_sync = sub.add_parser("sync", help="Sync settings and import history with your other computers")
    p_sync.set_defaults(func=cmd_sync)

    p_check = sub.add_parser("check", help="Verify config + Google auth are working")
    p_check.set_defaults(func=cmd_check)

    p_search = sub.add_parser("search", help="Find events on your calendar (4 months back to a year ahead)")
    p_search.add_argument("text", nargs="+", help="Words to search for")
    p_search.set_defaults(func=cmd_search)

    p_report = sub.add_parser("report", help="Build/send the full weekly report (all calendars) without importing")
    p_report.add_argument("--week", help="Week to report on, e.g. 9/21/2026 (defaults to next week)")
    p_report.add_argument("--start", help="Custom range start, e.g. 9/21/2026 (use with --end)")
    p_report.add_argument("--end", help="Custom range end, e.g. 9/27/2026 (use with --start)")
    p_report.add_argument("--this-week", action="store_true", help="Report on the current week instead of next week")
    p_report.add_argument("--dry-run", action="store_true", help="Print the report without emailing it")
    p_report.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    p_report.set_defaults(func=cmd_report)

    return parser


def _sync_quietly():
    import sync
    sync.sync_quietly()


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        # default: `import` with defaults, matching the old script's "just run it" behavior
        args.func = cmd_import
        for attr, default in [
            ("index", None), ("week", None), ("yes", False), ("dry_run", False),
            ("no_email", False), ("force", False), ("limit", None), ("keep_dropped", False),
        ]:
            if not hasattr(args, attr):
                setattr(args, attr, default)

    changes_state = args.func in (cmd_import, cmd_delete, cmd_undo, cmd_reset)
    try:
        if changes_state:
            _sync_quietly()
        args.func(args)
        if changes_state:
            _sync_quietly()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
    except DataFileError as e:
        sys.exit(f"Error: {e}")
    except Exception as e:
        if is_network_error(e):
            sys.exit("Can't reach Google. Check your internet connection and try again.")
        if type(e).__name__ == "RefreshError":
            sys.exit("Your Google login expired. Delete token.json and run again to sign in.")
        raise


if __name__ == "__main__":
    main()
