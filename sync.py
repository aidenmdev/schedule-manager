"""Keeps settings and import history the same on every computer signed in to your Google account.

The shared copy lives in a private calendar called "Schedule Manager sync data" (created on first use,
hidden from your calendar list). Each computer merges its own changes with the shared copy, so nothing
one computer did is lost when another one syncs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import dominos_schedule as core

CALENDAR_NAME = "Schedule Manager sync data"
FORMAT = 1
CHUNK_CHARS = 6000          # Google limits an event description to 8192 characters
ANCHOR_DAY = "2000-01-01"
STALE_CHUNK_MINUTES = 10    # leftovers from older versions are removed once they are this old
LOCAL_ONLY_CONFIG = frozenset({"tablet_port", "sync_enabled"})
SYNCED_PREFS = ("plan_goal", "plan_longest", "plan_title", "auto_check_email")
SECTIONS = ("config", "state", "prefs")
EMPTY_STATE = {"imported_shift_keys": [], "weeks": {}, "history": []}
MISSING = object()


class SyncError(Exception):
    """Something other than being offline stopped a sync; the message is meant to be shown."""


class Conflict(Exception):
    """Another computer saved first."""


def enabled(config: Optional[dict]) -> bool:
    return bool(config) and config.get("sync_enabled", True) is not False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def merge_dict(base: dict, local: dict, remote: dict, local_newer: bool, deep: bool = True) -> dict:
    """Three-way merge. A change made on only one side wins; if both sides changed the same key, the newer side wins."""
    keys = list(local) + [k for k in remote if k not in local] + [k for k in base if k not in local and k not in remote]
    out = {}
    for k in keys:
        b, l, r = base.get(k, MISSING), local.get(k, MISSING), remote.get(k, MISSING)
        if l == r:
            pick = l
        elif l == b:
            pick = r
        elif r == b:
            pick = l
        elif deep and isinstance(l, dict) and isinstance(r, dict):
            pick = merge_dict(b if isinstance(b, dict) else {}, l, r, local_newer)
        else:
            pick = l if local_newer else r
        if pick is not MISSING:
            out[k] = pick
    return out


def _merge_list(base: list, local: list, remote: list, ident, combine=None) -> list:
    """Merge lists of items: anything added on either side is kept, anything removed on either side is dropped."""
    b = {ident(x): x for x in base}
    l = {ident(x): x for x in local}
    r = {ident(x): x for x in remote}
    out = []
    for key in list(l) + [k for k in r if k not in l]:
        if key in l and key in r:
            item = combine(l[key], r[key]) if combine else l[key]
        elif key in l:
            if key in b:  # the other side deleted it
                continue
            item = l[key]
        else:
            if key in b:
                continue
            item = r[key]
        out.append(item)
    return out


def _history_ident(entry) -> str:
    return json.dumps({k: v for k, v in entry.items() if k != "undone"}, sort_keys=True, default=str) \
        if isinstance(entry, dict) else json.dumps(entry)


def _combine_history(a: dict, b: dict) -> dict:
    merged = dict(a)
    if a.get("undone") or b.get("undone"):
        merged["undone"] = True
    return merged


def normalize_state(state: Optional[dict]) -> dict:
    out = dict(state or {})
    for key, default in EMPTY_STATE.items():
        out.setdefault(key, type(default)())
    return out


def merge_state(base: dict, local: dict, remote: dict, local_newer: bool) -> dict:
    base, local, remote = normalize_state(base), normalize_state(local), normalize_state(remote)
    merged = merge_dict(base, local, remote, local_newer, deep=False)
    merged["weeks"] = merge_dict(base["weeks"], local["weeks"], remote["weeks"], local_newer, deep=False)
    merged["history"] = sorted(
        _merge_list(base["history"], local["history"], remote["history"], _history_ident, _combine_history),
        key=lambda e: (e.get("timestamp", ""), _history_ident(e)) if isinstance(e, dict) else ("", ""))
    merged["imported_shift_keys"] = sorted(_merge_list(  # sorted, so two computers always write the same thing
        base["imported_shift_keys"], local["imported_shift_keys"], remote["imported_shift_keys"], lambda k: k))
    return merged


def synced_config(config: dict) -> dict:
    return {k: v for k, v in config.items() if k not in LOCAL_ONLY_CONFIG}


def synced_prefs(prefs: dict) -> dict:
    return {k: prefs[k] for k in SYNCED_PREFS if k in prefs}


def _blank(section: str, value: Optional[dict]) -> bool:
    if section == "config":
        return not synced_config(value or {})
    if section == "state":
        return not any((value or {}).get(k) for k in EMPTY_STATE)
    return False


def merge_sections(base: Optional[dict], local: dict, remote: Optional[dict], local_newer: dict) -> dict:
    """{'config', 'state', 'prefs'} that both sides should end up with."""
    if remote is None:
        return {"config": synced_config(local["config"]), "state": normalize_state(local["state"]),
                "prefs": synced_prefs(local["prefs"])}
    joining = base is None
    base = base or {}
    return {
        "config": merge_dict(base.get("config", {}), synced_config(local["config"]), remote.get("config", {}),
                             local_newer["config"] and not joining),  # a computer that is just joining adopts the shared settings
        "state": merge_state(base.get("state", {}), local["state"], remote.get("state", {}), local_newer["state"]),
        "prefs": merge_dict(base.get("prefs", {}), synced_prefs(local["prefs"]), remote.get("prefs", {}),
                            local_newer["prefs"]),
    }


def encode_doc(doc: dict) -> str:
    raw = json.dumps(doc, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode_doc(text: str) -> dict:
    return json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))


def head_text(rev: int, tag: str, chunks: int) -> str:
    return f"sync v{FORMAT} rev={rev} tag={tag} chunks={chunks}"  # plain words: nothing for Google to reformat


def parse_head(text: Optional[str]) -> dict:
    fields = dict(part.split("=", 1) for part in (text or "").split() if "=" in part)
    return {"rev": int(fields["rev"]), "tag": fields["tag"], "chunks": int(fields["chunks"])}


def _http_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    try:
        return int(resp.status) if resp is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class CloudStore:
    """Reads and writes the shared copy: one 'head' event that points at the newest set of 'chunk' events."""

    def __init__(self, service, calendar_id: Optional[str] = None, tzname: str = "America/Los_Angeles"):
        self.svc = service
        self.calendar_id = calendar_id
        self.tzname = tzname

    def _ensure_calendar(self) -> str:
        if self.calendar_id:
            try:
                self.svc.calendars().get(calendarId=self.calendar_id).execute(num_retries=core.RETRIES)
                return self.calendar_id
            except Exception as e:
                if _http_status(e) not in (404, 410):
                    raise
                self.calendar_id = None
        found, token = [], None
        while True:
            resp = self.svc.calendarList().list(showHidden=True, pageToken=token).execute(num_retries=core.RETRIES)
            found += [c for c in resp.get("items", [])
                      if c.get("summary") == CALENDAR_NAME and c.get("accessRole", "owner") == "owner"]
            token = resp.get("nextPageToken")
            if not token:
                break
        if found:
            self.calendar_id = sorted(c["id"] for c in found)[0]  # if two computers both made one, everyone uses the same
            return self.calendar_id
        created = self.svc.calendars().insert(body={
            "summary": CALENDAR_NAME, "timeZone": self.tzname,
            "description": "Used by Schedule Manager to keep your computers in sync. Safe to hide; please don't delete.",
        }).execute(num_retries=core.RETRIES)
        self.calendar_id = created["id"]
        try:
            self.svc.calendarList().patch(calendarId=self.calendar_id, body={"hidden": True, "selected": False}) \
                .execute(num_retries=core.RETRIES)
        except Exception:
            pass  # only tidiness
        return self.calendar_id

    def _list(self, props: list) -> list:
        items, token = [], None
        while True:
            resp = self.svc.events().list(calendarId=self.calendar_id, privateExtendedProperty=props, maxResults=250,
                                          pageToken=token).execute(num_retries=core.RETRIES)
            items += resp.get("items", [])
            token = resp.get("nextPageToken")
            if not token:
                return items

    @staticmethod
    def _event_body(summary: str, description: str, private: dict) -> dict:
        return {"summary": summary, "description": description,
                "start": {"date": ANCHOR_DAY}, "end": {"date": "2000-01-02"},
                "transparency": "transparent", "reminders": {"useDefault": False},
                "extendedProperties": {"private": private}}

    def read(self):
        """(document or None, head event or None)."""
        self._ensure_calendar()
        for _attempt in range(3):
            heads = sorted(self._list(["sm_kind=head"]), key=lambda e: (e.get("created", ""), e["id"]))
            if not heads:
                return None, None
            head = heads[0]
            try:
                info = parse_head(head.get("description"))
                tag, count = info["tag"], info["chunks"]
            except (ValueError, KeyError, TypeError):
                raise SyncError("The shared settings copy in your Google account is unreadable. "
                                "Delete the 'Schedule Manager sync data' calendar to start it fresh.")
            chunks = {}
            for c in self._list(["sm_kind=chunk", f"sm_tag={tag}"]):
                chunks[int(c["extendedProperties"]["private"]["sm_idx"])] = c.get("description", "")
            if sorted(chunks) == list(range(count)):
                try:
                    return decode_doc("".join(chunks[i] for i in range(count))), head
                except (ValueError, zlib.error) as e:
                    raise SyncError(f"The shared copy in your Google account is damaged ({e}).")
            # another computer was replacing the chunks while we read; look again
        raise SyncError("The shared copy in Google is incomplete right now. Try again in a minute.")

    def write(self, doc: dict, head: Optional[dict]) -> None:
        """Save `doc`. Raises Conflict if another computer saved since `head` was read."""
        self._ensure_calendar()
        tag = uuid.uuid4().hex
        rev = (parse_head(head.get("description"))["rev"] + 1) if head else 1
        text = encode_doc(doc)
        pieces = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or [""]
        try:
            for i, piece in enumerate(pieces):
                self.svc.events().insert(calendarId=self.calendar_id, body=self._event_body(
                    f"sync data {rev} part {i + 1}", piece,
                    {"sm_kind": "chunk", "sm_tag": tag, "sm_idx": str(i), "sm_rev": str(rev)})).execute(num_retries=core.RETRIES)
            body = self._event_body("Schedule Manager sync (do not edit)",
                                    head_text(rev, tag, len(pieces)),
                                    {"sm_kind": "head"})
            if head is None:
                mine = self.svc.events().insert(calendarId=self.calendar_id, body=body).execute(num_retries=core.RETRIES)
                heads = sorted(self._list(["sm_kind=head"]), key=lambda e: (e.get("created", ""), e["id"]))
                if heads and heads[0]["id"] != mine["id"]:
                    self.svc.events().delete(calendarId=self.calendar_id, eventId=mine["id"]).execute()
                    raise Conflict()
            else:
                request = self.svc.events().update(calendarId=self.calendar_id, eventId=head["id"], body=body)
                request.headers["If-Match"] = head["etag"]
                try:
                    request.execute()
                except Exception as e:
                    if _http_status(e) == 412:
                        raise Conflict() from e
                    raise
        except BaseException:
            self._delete_chunks(lambda private: private.get("sm_tag") == tag)
            raise
        cutoff = utc_now() - timedelta(minutes=STALE_CHUNK_MINUTES)
        self._delete_chunks(lambda private: private.get("sm_tag") != tag, older_than=cutoff)

    def _delete_chunks(self, wanted, older_than: Optional[datetime] = None) -> None:
        try:
            for c in self._list(["sm_kind=chunk"]):
                private = c.get("extendedProperties", {}).get("private", {})
                if not wanted(private):
                    continue
                if older_than is not None and _parse_time(c.get("created", "1970-01-01T00:00:00Z")) > older_than:
                    continue
                self.svc.events().delete(calendarId=self.calendar_id, eventId=c["id"]).execute()
        except Exception:
            pass  # leftovers are harmless and get cleaned up next time


class LocalFiles:
    def __init__(self, config=None, state=None, prefs=None, meta=None, backups=None):
        self.config = Path(config or core.CONFIG_PATH)
        self.state = Path(state or core.STATE_PATH)
        self.prefs = Path(prefs or core.PREFS_PATH)
        self.meta = Path(meta or self.state.parent / "sync_state.json")
        self.backups = Path(backups or core.BACKUP_DIR)

    def _read(self, path: Path, default):
        if not path.exists():
            return default, b""
        raw = path.read_bytes()
        try:
            return core.read_json_safe(path), raw
        except core.DataFileError as e:
            raise SyncError(f"Not syncing because {e}")

    def read(self) -> dict:
        config, config_raw = self._read(self.config, {})
        state, state_raw = self._read(self.state, dict(EMPTY_STATE))
        prefs, prefs_raw = self._read(self.prefs, {})
        return {"config": config, "state": normalize_state(state), "prefs": prefs,
                "raw": {"config": config_raw, "state": state_raw, "prefs": prefs_raw}}

    def mtimes(self) -> dict:
        def mtime(path: Path) -> datetime:
            try:
                return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                return datetime.fromtimestamp(0, timezone.utc)
        return {"config": mtime(self.config), "state": mtime(self.state), "prefs": mtime(self.prefs)}

    def load_meta(self) -> dict:
        try:
            meta = core.read_json_safe(self.meta)
        except (OSError, core.DataFileError):
            meta = {}
        meta.setdefault("machine_id", uuid.uuid4().hex[:8])
        return meta

    def forget_base(self) -> None:
        """Next sync treats this computer as new: the shared settings win over whatever is on disk now."""
        meta = self.load_meta()
        if meta.pop("base", None) is not None:
            self.save_meta(meta)

    def save_meta(self, meta: dict) -> None:
        try:
            core.write_json_atomic(self.meta, meta)
        except OSError:
            pass

    def _backup(self) -> None:
        try:
            self.backups.mkdir(parents=True, exist_ok=True)
            path = self.backups / f"pre-sync-{datetime.now():%Y%m%d-%H%M%S}.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for p in (self.config, self.state, self.prefs):
                    if p.exists():
                        z.write(p, p.name)
            for old in sorted(self.backups.glob("pre-sync-*.zip"), reverse=True)[10:]:
                old.unlink()
        except OSError:
            pass

    def write(self, merged: dict, local: dict) -> tuple:
        """Bring the files up to date. Returns (sections changed, True if a file changed while we worked)."""
        wanted = {
            "config": {**{k: v for k, v in local["config"].items() if k in LOCAL_ONLY_CONFIG}, **merged["config"]},
            "state": merged["state"],
            "prefs": {**{k: v for k, v in local["prefs"].items() if k not in SYNCED_PREFS}, **merged["prefs"]},
        }
        paths = {"config": self.config, "state": self.state, "prefs": self.prefs}
        current = {"config": local["config"], "state": local["state"], "prefs": local["prefs"]}
        changed, interrupted, backed_up = [], False, False
        for section in SECTIONS:
            if wanted[section] == current[section]:
                continue
            path = paths[section]
            now_raw = path.read_bytes() if path.exists() else b""
            if now_raw != local["raw"][section]:
                interrupted = True  # the app saved this file while we were syncing; the next sync will pick it up
                continue
            if not backed_up and section != "prefs":
                self._backup()
                backed_up = True
            core.write_json_atomic(path, wanted[section])
            changed.append(section)
        return tuple(changed), interrupted


@dataclass
class SyncResult:
    pushed: bool = False
    pulled: tuple = ()
    first_upload: bool = False
    joined: bool = False
    interrupted: bool = False
    calendar_id: str = ""
    at: str = ""
    notes: list = field(default_factory=list)


def _digest(sections: dict) -> str:
    return hashlib.sha256(json.dumps(sections, sort_keys=True, default=str).encode()).hexdigest()


def sync_once(calendar, files: Optional[LocalFiles] = None, tzname: str = "America/Los_Angeles",
              now: Optional[datetime] = None, attempts: int = 4) -> SyncResult:
    files = files or LocalFiles()
    meta = files.load_meta()
    store = CloudStore(calendar, meta.get("calendar_id"), tzname)
    now = now or utc_now()
    for _ in range(attempts):
        local = files.read()
        times = files.mtimes()
        remote_doc, head = store.read()
        if remote_doc and remote_doc.get("format", FORMAT) > FORMAT:
            raise SyncError("Another computer is running a newer Schedule Manager. Update this one to keep syncing.")
        base = meta.get("base")
        effective = dict(local)
        for section in ("config", "state"):  # an emptied or re-created file is damage, not a decision to delete everything
            if base and _blank(section, local[section]) and not _blank(section, base.get(section)):
                effective[section] = base[section]
        remote_time = _parse_time(remote_doc["updated"]) if remote_doc else None
        local_newer = {s: bool(remote_time is None or times[s] > remote_time) for s in SECTIONS}
        remote_sections = {s: remote_doc.get(s, {}) for s in SECTIONS} if remote_doc else None
        merged = merge_sections(base, effective, remote_sections, local_newer)

        result = SyncResult(first_upload=remote_doc is None, joined=remote_doc is not None and base is None,
                            at=now.isoformat(timespec="seconds"))
        if remote_sections is None or _digest(merged) != _digest({s: remote_sections[s] for s in SECTIONS}):
            doc = {"format": FORMAT, "updated": now.isoformat(timespec="seconds"),
                   "writer": {"id": meta["machine_id"], "name": socket.gethostname()}, **merged}
            try:
                store.write(doc, head)
            except Conflict:
                continue
            result.pushed = True
        result.pulled, result.interrupted = files.write(merged, local)
        meta.update({"calendar_id": store.calendar_id, "last_sync": result.at, "machine_name": socket.gethostname()})
        if not result.interrupted:
            meta["base"] = merged
        files.save_meta(meta)
        result.calendar_id = store.calendar_id
        return result
    raise SyncError("Another computer was saving at the same moment. It will sync on the next try.")


def sync_quietly(calendar=None, config: Optional[dict] = None) -> Optional[SyncResult]:
    """For the command line: sync if turned on, and never let a sync problem stop the real work."""
    try:
        config = config or core.load_config()
        if not enabled(config):
            return None
        if calendar is None:
            calendar = core.build_services()[1]
        return sync_once(calendar, tzname=config.get("timezone", "America/Los_Angeles"))
    except (Exception, SystemExit) as e:
        text = "offline" if isinstance(e, Exception) and core.is_network_error(e) else str(e) or type(e).__name__
        print(f"(Sync skipped: {text})")
        return None
