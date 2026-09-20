"""In-memory stand-ins for the Google Calendar / Gmail clients, so tests never touch the network."""
import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httplib2
from googleapiclient.errors import HttpError

TZ = ZoneInfo("America/Los_Angeles")


def make_config(**over):
    cfg = {
        "gmail_query": "from:dominos.com", "calendar_id": "primary", "event_title": "Dominos",
        "timezone": "America/Los_Angeles", "reminder_minutes_before": [60, 30], "store_name": "Domino's",
        "summary_email_to": "me@example.com", "summary_email_subject_prefix": "Weekly Schedule",
        "report_calendars": ["primary"], "event_color_id": {},
        "job_match": {"Dominos": "dominos", "Staples": "staples, work"},
        "job_wages": {"Dominos": 17.58, "Staples": 17.2},
        "school_course_code_regex": r"^[A-Za-z]{2,4}\s?-?\d{2,4}[A-Za-z]?\b",
        "school_extra_titles": [], "school_exceptions": [],
        "conflict_thresholds_minutes": {"very_close": 30, "close": 60}, "break_ok_categories": ["Dominos"],
        "display_name": "Tester", "sync_enabled": False, "tax_rate_percent": 0, "min_rest_hours": 8, "weekly_hours_goal": 0, "email_html": True, "pay_schedule": {"type": "off", "start": "", "delay_days": 0},
    }
    cfg.update(over)
    return cfg


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


def http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"{}")


class FakeCalendar:
    """Just enough of events().insert/list/delete/patch/get."""

    def __init__(self):
        self.events_by_id = {}
        self._n = 0
        self.fail_insert_after = None  # raise on the (n+1)th insert
        self.fail_delete_ids = set()
        self.inserts = 0
        self.offline = False

    def events(self):
        return self

    def _add(self, body):
        self._n += 1
        eid = f"ev{self._n}"
        ev = dict(body)
        ev["id"] = eid
        for k in ("start", "end"):  # store like Google does: an offset-aware timestamp
            naive = datetime.fromisoformat(body[k]["dateTime"])
            ev[k] = {"dateTime": naive.replace(tzinfo=TZ).isoformat()}
        ev.setdefault("htmlLink", f"https://calendar.example/{eid}")
        if "reminders" in body:
            ev["reminders"] = {"useDefault": False, "overrides": body["reminders"]["overrides"]}
        self.events_by_id[eid] = ev
        return ev

    def add_raw(self, title, start, end):
        return self._add({"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}})

    def insert(self, calendarId, body):
        def go():
            if self.fail_insert_after is not None and self.inserts >= self.fail_insert_after:
                raise http_error(500)
            self.inserts += 1
            return self._add(body)
        return Req(go)

    def list(self, calendarId, timeMin, timeMax, singleEvents=True, orderBy=None, pageToken=None, q=None, **kw):
        lo, hi = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)

        def go():
            if self.offline:
                raise OSError("[Errno 11001] getaddrinfo failed")
            items = [e for e in self.events_by_id.values()
                     if lo <= datetime.fromisoformat(e["start"]["dateTime"]) < hi
                     and (not q or q.lower() in (e.get("summary", "") + " " + e.get("description", "")).lower())]
            return {"items": sorted(items, key=lambda e: e["start"]["dateTime"])}
        return Req(go)

    def delete(self, calendarId, eventId):
        def go():
            if eventId in self.fail_delete_ids:
                raise http_error(500)
            if eventId not in self.events_by_id:
                raise http_error(404)
            del self.events_by_id[eventId]
        return Req(go)

    def patch(self, calendarId, eventId, body):
        def go():
            if eventId not in self.events_by_id:
                raise http_error(404)
            if "reminders" in body:
                self.events_by_id[eventId]["reminders"] = body["reminders"]
            for k in ("start", "end"):
                if k in body:
                    naive = datetime.fromisoformat(body[k]["dateTime"])
                    self.events_by_id[eventId][k] = {"dateTime": naive.replace(tzinfo=TZ).isoformat()}
            return self.events_by_id[eventId]
        return Req(go)

    def get(self, calendarId, eventId):
        def go():
            if eventId not in self.events_by_id:
                raise http_error(404)
            return self.events_by_id[eventId]
        return Req(go)

    def calendarList(self):
        class _CL:
            def get(self, calendarId):
                return Req(lambda: {"summary": "Test Calendar", "id": calendarId})
        return _CL()


def make_message(mid, body, internal_ms):
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {"id": mid, "internalDate": str(internal_ms),
            "payload": {"mimeType": "text/plain", "body": {"data": data}}}


class FakeGmail:
    def __init__(self, messages=None):
        self.messages_list = messages or []
        self.get_calls = 0
        self.sent = []
        self.fail_send = False
        self.offline = False

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        def go():
            if self.offline:
                raise OSError("[Errno 11001] getaddrinfo failed")
            return {"messages": [{"id": m["id"]} for m in self.messages_list]}
        return Req(go)

    def get(self, userId, id, format):
        def go():
            self.get_calls += 1
            return next(m for m in self.messages_list if m["id"] == id)
        return Req(go)

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": "test@example.com"})

    def send(self, userId, body):
        def go():
            if self.fail_send:
                raise RuntimeError("boom")
            raw = base64.urlsafe_b64decode(body["raw"]).decode("utf-8", errors="replace")
            subject = next((l[9:] for l in raw.splitlines() if l.lower().startswith("subject: ")), "")
            self.sent.append({"raw": raw, "subject": subject})
            return {"id": f"sent-{len(self.sent)}"}
        return Req(go)


class HeaderReq(Req):
    """A request whose headers can be set before execute(), like the real client's."""

    def __init__(self, fn):
        super().__init__(fn)
        self.headers = {}


class FakeGoogleAccount:
    """A whole Google account for the sync code: calendars, a calendar list, and events with etags,
    private extended properties and If-Match. Every 'computer' in a test shares one instance."""

    def __init__(self):
        self.calendars_by_id = {}
        self.events_by_cal = {}
        self.hidden = set()
        self._n = 0
        self.offline = False
        self.before_update = None   # called with (calendarId, eventId) just before an update is checked
        self.before_insert = None   # called with (calendarId, body) just before an insert
        self.calls = []

    def _tick(self):
        self._n += 1
        return self._n

    def _check_online(self):
        if self.offline:
            raise OSError("[Errno 11001] getaddrinfo failed")

    def add_calendar(self, summary, cal_id=None):
        cal_id = cal_id or f"cal{self._tick()}"
        self.calendars_by_id[cal_id] = {"id": cal_id, "summary": summary}
        self.events_by_cal[cal_id] = {}
        return cal_id

    def events_in(self, cal_id, kind=None):
        items = list(self.events_by_cal[cal_id].values())
        if kind:
            items = [e for e in items if e.get("extendedProperties", {}).get("private", {}).get("sm_kind") == kind]
        return items

    # ---- services ----
    def calendars(self):
        acct = self

        class _Calendars:
            def get(self, calendarId):
                def go():
                    acct._check_online()
                    if calendarId not in acct.calendars_by_id:
                        raise http_error(404)
                    return acct.calendars_by_id[calendarId]
                return Req(go)

            def insert(self, body):
                def go():
                    acct._check_online()
                    acct.calls.append("calendars.insert")
                    cid = acct.add_calendar(body["summary"])
                    return acct.calendars_by_id[cid]
                return Req(go)
        return _Calendars()

    def calendarList(self):
        acct = self

        class _CalendarList:
            def list(self, showHidden=False, pageToken=None, **kw):
                def go():
                    acct._check_online()
                    return {"items": [dict(c, accessRole="owner") for c in acct.calendars_by_id.values()
                                      if showHidden or c["id"] not in acct.hidden]}
                return Req(go)

            def patch(self, calendarId, body):
                def go():
                    if body.get("hidden"):
                        acct.hidden.add(calendarId)
                    return {"id": calendarId}
                return Req(go)
        return _CalendarList()

    def events(self):
        acct = self

        class _Events:
            def list(self, calendarId, privateExtendedProperty=None, maxResults=250, pageToken=None, **kw):
                def go():
                    acct._check_online()
                    needs = [p.split("=", 1) for p in (privateExtendedProperty or [])]
                    items = [dict(e) for e in acct.events_by_cal[calendarId].values()
                             if all(e.get("extendedProperties", {}).get("private", {}).get(k) == v for k, v in needs)]
                    return {"items": items}
                return Req(go)

            def insert(self, calendarId, body):
                def go():
                    acct._check_online()
                    if acct.before_insert:
                        acct.before_insert(calendarId, body)
                    n = acct._tick()
                    ev = dict(body, id=f"e{n}", etag=f'"{n}"', created=(datetime.now(timezone.utc) + timedelta(microseconds=n)).isoformat().replace("+00:00", "Z"))
                    acct.events_by_cal[calendarId][ev["id"]] = ev
                    return dict(ev)
                return Req(go)

            def update(self, calendarId, eventId, body):
                def go():
                    acct._check_online()
                    if acct.before_update:
                        acct.before_update(calendarId, eventId)
                    ev = acct.events_by_cal[calendarId].get(eventId)
                    if ev is None:
                        raise http_error(404)
                    if req.headers.get("If-Match") not in (None, ev["etag"]):
                        raise http_error(412)
                    n = acct._tick()
                    updated = dict(body, id=eventId, etag=f'"{n}"', created=ev["created"])
                    acct.events_by_cal[calendarId][eventId] = updated
                    return dict(updated)
                req = HeaderReq(go)
                return req

            def delete(self, calendarId, eventId):
                def go():
                    acct._check_online()
                    if eventId not in acct.events_by_cal[calendarId]:
                        raise http_error(404)
                    del acct.events_by_cal[calendarId][eventId]
                return Req(go)
        return _Events()


class FakeMailbox:
    """A Gmail account that understands sending, listing, reading messages with attachments, and downloading them."""

    def __init__(self, address="me@example.com"):
        self.address = address
        self.stored = {}
        self.attachment_data = {}
        self.offline = False
        self.sent_sizes = []
        self.media_sends = 0

    def users(self):
        return self

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": self.address})

    def messages(self):
        return self

    def attachments(self):
        return self.attachments_api()

    def attachments_api(self):
        mailbox = self

        class _Attachments:
            def get(self, userId, messageId, id):
                def go():
                    mailbox._check()
                    return {"data": base64.urlsafe_b64encode(mailbox.attachment_data[(messageId, id)]).decode("ascii"),
                            "size": len(mailbox.attachment_data[(messageId, id)])}
                return Req(go)
        return _Attachments()

    def _check(self):
        if self.offline:
            raise OSError("[Errno 11001] getaddrinfo failed")

    def send(self, userId, body=None, media_body=None):
        import email
        from email import policy

        def go():
            self._check()
            if media_body is not None:
                raw = media_body.getbytes(0, media_body.size())
                self.media_sends += 1
            else:
                raw = base64.urlsafe_b64decode(body["raw"])
            self.sent_sizes.append(len(raw))
            msg = email.message_from_bytes(raw, policy=policy.default)
            mid = f"m{len(self.stored) + 1}"
            parts = []
            for i, part in enumerate(msg.walk()):
                if part.is_multipart():
                    continue
                payload = part.get_payload(decode=True)
                name = part.get_filename()
                if name:
                    att_id = f"att-{mid}-{i}"
                    self.attachment_data[(mid, att_id)] = payload
                    parts.append({"mimeType": part.get_content_type(), "filename": name,
                                  "body": {"attachmentId": att_id, "size": len(payload)}})
                else:
                    parts.append({"mimeType": part.get_content_type(), "filename": "",
                                  "body": {"data": base64.urlsafe_b64encode(payload).decode("ascii")}})
            self.stored[mid] = {"id": mid, "from": self.address, "subject": msg["Subject"],
                                "payload": {"mimeType": "multipart/mixed", "parts": parts}}
            return {"id": mid}
        return Req(go)

    def add_plain(self, subject, text, sender=None):
        mid = f"m{len(self.stored) + 1}"
        self.stored[mid] = {"id": mid, "from": sender or self.address, "subject": subject, "payload": {
            "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode("ascii")}}}
        return mid

    def list(self, userId, q="", maxResults=50, pageToken=None, **kw):
        def go():
            self._check()
            import re as _re
            subject = _re.search(r'subject:"([^"]+)"', q or "")
            hits = [m for m in self.stored.values()
                    if (not subject or subject.group(1) in m["subject"])
                    and ("from:me" not in (q or "") or m["from"] == self.address)
                    and ("has:attachment" not in (q or "") or any(p.get("filename") for p in m["payload"].get("parts", [])))]
            return {"messages": [{"id": m["id"]} for m in reversed(hits)][:maxResults]}
        return Req(go)

    def get(self, userId, id, format="full"):
        def go():
            self._check()
            m = self.stored[id]
            return {"id": id, "payload": m["payload"]}
        return Req(go)
