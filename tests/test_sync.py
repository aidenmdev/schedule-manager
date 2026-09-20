"""Syncing settings and import history between computers, using a fake Google account.
Run:  .venv\\Scripts\\python.exe -m unittest tests.test_sync -v"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
import sync
from tests.fakes import FakeGoogleAccount, make_config

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def minutes(n):
    return T0 + timedelta(minutes=n)


def week_entry(week, ids):
    return {"email_id": "m-" + week, "week_start": week, "week_end": week, "imported_at": "2026-09-19T10:00:00",
            "calendar_id": "primary", "event_ids": ids, "shift_keys": [f"{week}|{i}" for i in ids]}


def history_entry(action, ts, week, ids):
    return {"action": action, "timestamp": ts, "week": week, "event_ids": ids}


class Computer:
    """One computer's files in its own folder."""

    def __init__(self, name, config=None, state=None, prefs=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.name = name
        self.files = sync.LocalFiles(config=self.dir / "config.json", state=self.dir / "state.json",
                                     prefs=self.dir / "gui_prefs.json", meta=self.dir / "sync_state.json",
                                     backups=self.dir / "backups")
        if config is not None:
            self.write("config", config)
        if state is not None:
            self.write("state", state)
        if prefs is not None:
            self.write("prefs", prefs)

    def path(self, section):
        return {"config": self.files.config, "state": self.files.state, "prefs": self.files.prefs}[section]

    def write(self, section, data, when=None):
        p = self.path(section)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if when is not None:
            os.utime(p, (when.timestamp(), when.timestamp()))

    def read(self, section):
        return json.loads(self.path(section).read_text(encoding="utf-8"))

    def sync(self, account, at=0):
        return sync.sync_once(account, self.files, now=minutes(at))

    def close(self):
        self.tmp.cleanup()


class MergeTests(unittest.TestCase):
    def test_change_on_one_side_wins(self):
        base = {"a": 1, "b": 2}
        self.assertEqual(sync.merge_dict(base, {"a": 5, "b": 2}, {"a": 1, "b": 9}, True), {"a": 5, "b": 9})

    def test_both_changed_newer_side_wins(self):
        base = {"a": 1}
        self.assertEqual(sync.merge_dict(base, {"a": 2}, {"a": 3}, True), {"a": 2})
        self.assertEqual(sync.merge_dict(base, {"a": 2}, {"a": 3}, False), {"a": 3})

    def test_deletion_propagates(self):
        self.assertEqual(sync.merge_dict({"a": 1, "b": 2}, {"a": 1}, {"a": 1, "b": 2}, True), {"a": 1})
        self.assertEqual(sync.merge_dict({"a": 1, "b": 2}, {"a": 1, "b": 2}, {"b": 2}, True), {"b": 2})

    def test_new_key_on_either_side_is_kept(self):
        self.assertEqual(sync.merge_dict({}, {"a": 1}, {"b": 2}, True), {"a": 1, "b": 2})

    def test_nested_dicts_merge_per_key(self):
        base = {"job_wages": {"Dominos": 16, "Staples": 17}}
        local = {"job_wages": {"Dominos": 18, "Staples": 17}}
        remote = {"job_wages": {"Dominos": 16, "Staples": 19}}
        self.assertEqual(sync.merge_dict(base, local, remote, True), {"job_wages": {"Dominos": 18, "Staples": 19}})

    def test_lists_are_replaced_not_merged(self):
        merged = sync.merge_dict({"r": [60, 30]}, {"r": [60]}, {"r": [120, 30]}, False)
        self.assertEqual(merged["r"], [120, 30])

    def test_state_history_is_a_union(self):
        a = history_entry("import", "2026-09-19T10:00:00", "2026-09-21", ["x"])
        b = history_entry("import", "2026-09-19T11:00:00", "2026-09-28", ["y"])
        merged = sync.merge_state({"history": []}, {"history": [a]}, {"history": [b]}, True)
        self.assertEqual([e["week"] for e in merged["history"]], ["2026-09-21", "2026-09-28"])

    def test_history_deleted_on_one_side_is_gone(self):
        a = history_entry("import", "2026-09-19T10:00:00", "2026-09-21", ["x"])
        merged = sync.merge_state({"history": [a]}, {"history": []}, {"history": [a]}, True)
        self.assertEqual(merged["history"], [])

    def test_undone_flag_survives_from_either_side(self):
        a = history_entry("import", "2026-09-19T10:00:00", "2026-09-21", ["x"])
        undone = dict(a, undone=True)
        for local, remote in (([undone], [a]), ([a], [undone])):
            merged = sync.merge_state({"history": [a]}, {"history": local}, {"history": remote}, True)
            self.assertEqual(len(merged["history"]), 1)
            self.assertTrue(merged["history"][0]["undone"])

    def test_weeks_added_and_removed_on_different_computers(self):
        w1, w2 = week_entry("2026-09-21", ["a"]), week_entry("2026-09-28", ["b"])
        base = {"weeks": {"2026-09-21": w1}}
        merged = sync.merge_state(base, {"weeks": {"2026-09-21": w1, "2026-09-28": w2}}, {"weeks": {}}, True)
        self.assertEqual(list(merged["weeks"]), ["2026-09-28"])

    def test_shift_keys_are_sorted_and_deduplicated(self):
        merged = sync.merge_state({}, {"imported_shift_keys": ["b", "a"]}, {"imported_shift_keys": ["c", "a"]}, True)
        self.assertEqual(merged["imported_shift_keys"], ["a", "b", "c"])

    def test_missing_state_keys_are_filled_in(self):
        self.assertEqual(sync.normalize_state({}), {"imported_shift_keys": [], "weeks": {}, "history": []})

    def test_local_only_settings_never_travel(self):
        cfg = {"a": 1, "tablet_port": 9000, "sync_enabled": False}
        self.assertEqual(sync.synced_config(cfg), {"a": 1})

    def test_only_chosen_prefs_travel(self):
        self.assertEqual(sync.synced_prefs({"accent": "Rose", "size": "800x600", "plan_goal": 5}),
                         {"accent": "Rose", "plan_goal": 5})

    def test_encode_round_trip(self):
        doc = {"format": 1, "x": ["é", 1, {"y": None}]}
        self.assertEqual(sync.decode_doc(sync.encode_doc(doc)), doc)


class SyncFlowTests(unittest.TestCase):
    def setUp(self):
        self.acct = FakeGoogleAccount()
        self.cfg = make_config(sync_enabled=True)
        self.computers = []
        self._stale = sync.STALE_CHUNK_MINUTES

    def tearDown(self):
        sync.STALE_CHUNK_MINUTES = self._stale
        for c in self.computers:
            c.close()

    def computer(self, name, **kw):
        c = Computer(name, **kw)
        self.computers.append(c)
        return c

    def test_first_computer_uploads_and_creates_a_hidden_calendar(self):
        a = self.computer("A", config=self.cfg, state={"weeks": {"2026-09-21": week_entry("2026-09-21", ["e1"])}})
        r = a.sync(self.acct)
        self.assertTrue(r.first_upload and r.pushed)
        cals = [c for c in self.acct.calendars_by_id.values() if c["summary"] == sync.CALENDAR_NAME]
        self.assertEqual(len(cals), 1)
        self.assertIn(cals[0]["id"], self.acct.hidden)
        self.assertEqual(len(self.acct.events_in(cals[0]["id"], "head")), 1)

    def test_local_files_are_untouched_by_the_first_upload(self):
        a = self.computer("A", config=self.cfg, state={"weeks": {}})
        before = {s: a.path(s).read_bytes() for s in ("config", "state")}
        r = a.sync(self.acct)
        self.assertEqual(r.pulled, ())
        self.assertEqual({s: a.path(s).read_bytes() for s in before}, before)

    def test_second_computer_adopts_settings_and_history(self):
        a = self.computer("A", config=dict(self.cfg, job_wages={"Dominos": 17.58}),
                          state={"history": [history_entry("import", "2026-09-19T10:00:00", "2026-09-21", ["x"])],
                                 "weeks": {"2026-09-21": week_entry("2026-09-21", ["x"])}},
                          prefs={"accent": "Rose", "size": "1000x700"})
        a.sync(self.acct)
        b = self.computer("B", config=make_config(sync_enabled=True, job_wages={"Dominos": 0}, tablet_port=9999),
                          prefs={"size": "1500x900"})
        r = b.sync(self.acct, at=5)
        self.assertTrue(r.joined)
        self.assertEqual(set(r.pulled), {"config", "state", "prefs"})
        self.assertEqual(b.read("config")["job_wages"], {"Dominos": 17.58})
        self.assertEqual(b.read("config")["tablet_port"], 9999)           # stays this computer's own
        self.assertEqual(b.read("state")["weeks"]["2026-09-21"]["event_ids"], ["x"])
        self.assertEqual(len(b.read("state")["history"]), 1)
        self.assertEqual(b.read("prefs"), {"size": "1500x900", "accent": "Rose"})

    def test_joining_keeps_a_backup_of_what_it_replaced(self):
        a = self.computer("A", config=dict(self.cfg, display_name="Original"))
        a.sync(self.acct)
        b = self.computer("B", config=make_config(sync_enabled=True, display_name="Different"))
        b.sync(self.acct, at=1)
        self.assertEqual(len(list((b.dir / "backups").glob("pre-sync-*.zip"))), 1)

    def test_import_on_one_computer_shows_up_on_the_other(self):
        a = self.computer("A", config=self.cfg, state={})
        b = self.computer("B", config=self.cfg, state={})
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        a.write("state", {"weeks": {"2026-09-21": week_entry("2026-09-21", ["e1", "e2"])},
                          "imported_shift_keys": ["2026-09-21|e1", "2026-09-21|e2"],
                          "history": [history_entry("import", "2026-09-19T13:00:00", "2026-09-21", ["e1", "e2"])]},
                when=minutes(10))
        self.assertTrue(a.sync(self.acct, at=10).pushed)
        r = b.sync(self.acct, at=11)
        self.assertEqual(r.pulled, ("state",))
        self.assertEqual(list(b.read("state")["weeks"]), ["2026-09-21"])
        self.assertEqual(b.read("state")["history"][0]["action"], "import")

    def test_delete_week_on_one_computer_removes_it_on_the_other(self):
        state = {"weeks": {"2026-09-21": week_entry("2026-09-21", ["e1"])}, "imported_shift_keys": ["k1"],
                 "history": [history_entry("import", "2026-09-19T10:00:00", "2026-09-21", ["e1"])]}
        a = self.computer("A", config=self.cfg, state=state)
        b = self.computer("B", config=self.cfg, state=state)
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        b.write("state", {"weeks": {}, "imported_shift_keys": [],
                          "history": state["history"] + [history_entry("delete", "2026-09-19T14:00:00", "2026-09-21", ["e1"])]},
                when=minutes(20))
        b.sync(self.acct, at=20)
        a.sync(self.acct, at=21)
        merged = a.read("state")
        self.assertEqual(merged["weeks"], {})
        self.assertEqual(merged["imported_shift_keys"], [])
        self.assertEqual([e["action"] for e in merged["history"]], ["import", "delete"])

    def test_different_settings_changed_on_each_computer_both_survive(self):
        a = self.computer("A", config=self.cfg)
        b = self.computer("B", config=self.cfg)
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        a.write("config", dict(self.cfg, tax_rate_percent=12), when=minutes(10))
        b.write("config", dict(self.cfg, weekly_hours_goal=25), when=minutes(11))
        a.sync(self.acct, at=12)
        b.sync(self.acct, at=13)
        a.sync(self.acct, at=14)
        for c in (a, b):
            self.assertEqual(c.read("config")["tax_rate_percent"], 12)
            self.assertEqual(c.read("config")["weekly_hours_goal"], 25)

    def test_same_setting_changed_on_both_newest_edit_wins(self):
        a = self.computer("A", config=self.cfg)
        b = self.computer("B", config=self.cfg)
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        a.write("config", dict(self.cfg, display_name="From A"), when=minutes(30))
        b.write("config", dict(self.cfg, display_name="From B"), when=minutes(40))
        b.sync(self.acct, at=41)        # B's edit is newer and reaches Google first
        r = a.sync(self.acct, at=42)    # A's edit is older, so B's value wins on A
        self.assertIn("config", r.pulled)
        self.assertEqual(a.read("config")["display_name"], "From B")

    def test_older_edit_does_not_beat_newer_shared_value(self):
        a = self.computer("A", config=self.cfg)
        b = self.computer("B", config=self.cfg)
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        b.write("config", dict(self.cfg, display_name="From B"), when=minutes(50))
        b.sync(self.acct, at=50)
        a.write("config", dict(self.cfg, display_name="From A"), when=minutes(20))  # made earlier, saved later
        a.sync(self.acct, at=51)
        self.assertEqual(a.read("config")["display_name"], "From B")

    def test_sync_with_nothing_new_changes_nothing(self):
        a = self.computer("A", config=self.cfg, state={"weeks": {}})
        a.sync(self.acct)
        heads = {e["etag"] for e in self.acct.events_in(a.files.load_meta()["calendar_id"], "head")}
        r = a.sync(self.acct, at=5)
        self.assertFalse(r.pushed)
        self.assertEqual(r.pulled, ())
        self.assertEqual({e["etag"] for e in self.acct.events_in(a.files.load_meta()["calendar_id"], "head")}, heads)

    def test_two_computers_do_not_ping_pong(self):
        state = {"imported_shift_keys": ["b", "a"], "weeks": {}, "history": []}
        a = self.computer("A", config=self.cfg, state=state)
        b = self.computer("B", config=self.cfg, state={"imported_shift_keys": ["c", "a"], "weeks": {}, "history": []})
        a.sync(self.acct, at=0)
        b.sync(self.acct, at=1)
        a.sync(self.acct, at=2)
        for i in range(3, 7):
            self.assertFalse(a.sync(self.acct, at=i).pushed)
            self.assertFalse(b.sync(self.acct, at=i).pushed)

    def test_emptied_settings_file_is_repaired_not_propagated(self):
        a = self.computer("A", config=dict(self.cfg, display_name="Keep me"))
        b = self.computer("B", config=self.cfg)
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        a.write("config", {}, when=minutes(5))
        r = a.sync(self.acct, at=6)
        self.assertEqual(r.pulled, ("config",))
        self.assertEqual(a.read("config")["display_name"], "Keep me")
        b.sync(self.acct, at=7)
        self.assertEqual(b.read("config")["display_name"], "Keep me")

    def test_deleted_state_file_is_repaired_not_propagated(self):
        state = {"weeks": {"2026-09-21": week_entry("2026-09-21", ["e1"])}, "imported_shift_keys": ["k"],
                 "history": [history_entry("import", "2026-09-19T10:00:00", "2026-09-21", ["e1"])]}
        a = self.computer("A", config=self.cfg, state=state)
        a.sync(self.acct)
        a.path("state").unlink()
        r = a.sync(self.acct, at=6)
        self.assertEqual(r.pulled, ("state",))
        self.assertEqual(list(a.read("state")["weeks"]), ["2026-09-21"])

    def test_config_recreated_from_a_template_adopts_shared_settings(self):
        a = self.computer("A", config=dict(self.cfg, display_name="Real name", job_wages={"Dominos": 17.58}))
        a.sync(self.acct)
        a.write("config", make_config(sync_enabled=True, job_wages={"Dominos": 0}, display_name=""), when=minutes(30))
        a.files.forget_base()      # what the app does when it has to rebuild config.json from the template
        r = a.sync(self.acct, at=31)
        self.assertEqual(a.read("config")["job_wages"], {"Dominos": 17.58})
        self.assertEqual(a.read("config")["display_name"], "Real name")
        self.assertIn("config", r.pulled)

    def test_local_only_settings_are_not_uploaded(self):
        a = self.computer("A", config=dict(self.cfg, tablet_port=1234, sync_enabled=True))
        a.sync(self.acct)
        cal_id = a.files.load_meta()["calendar_id"]
        head = self.acct.events_in(cal_id, "head")[0]
        chunks = sorted(self.acct.events_in(cal_id, "chunk"), key=lambda e: int(e["extendedProperties"]["private"]["sm_idx"]))
        doc = sync.decode_doc("".join(c["description"] for c in chunks))
        self.assertNotIn("tablet_port", doc["config"])
        self.assertNotIn("sync_enabled", doc["config"])
        self.assertEqual(sync.parse_head(head["description"])["chunks"], len(chunks))

    def test_big_history_is_split_into_chunks_and_read_back(self):
        history = [history_entry("import", f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}", "2026-01-05",
                                 [os.urandom(13).hex() for _ in range(6)]) for i in range(200)]
        a = self.computer("A", config=self.cfg, state={"history": history})
        a.sync(self.acct)
        cal_id = a.files.load_meta()["calendar_id"]
        self.assertGreater(len(self.acct.events_in(cal_id, "chunk")), 1)
        for e in self.acct.events_in(cal_id, "chunk"):
            self.assertLessEqual(len(e["description"]), sync.CHUNK_CHARS)
        b = self.computer("B", config=self.cfg)
        b.sync(self.acct, at=1)
        self.assertEqual(len(b.read("state")["history"]), 200)

    def test_old_chunks_are_cleaned_up_after_a_newer_save(self):
        sync.STALE_CHUNK_MINUTES = -1  # treat everything as old
        a = self.computer("A", config=self.cfg, state={})
        a.sync(self.acct)
        for i in range(1, 4):
            a.write("state", {"history": [history_entry("import", f"2026-09-19T1{i}:00:00", "w", [str(i)])]}, when=minutes(i * 10))
            a.sync(self.acct, at=i * 10)
        cal_id = a.files.load_meta()["calendar_id"]
        self.assertEqual(len(self.acct.events_in(cal_id, "chunk")), 1)
        self.assertEqual(len(self.acct.events_in(cal_id, "head")), 1)

    def test_recent_chunks_are_left_alone(self):
        a = self.computer("A", config=self.cfg, state={})
        a.sync(self.acct)
        a.write("state", {"history": [history_entry("import", "2026-09-19T11:00:00", "w", ["1"])]}, when=minutes(10))
        a.sync(self.acct, at=10)
        cal_id = a.files.load_meta()["calendar_id"]
        self.assertEqual(len(self.acct.events_in(cal_id, "chunk")), 2)   # the previous copy may still be being read

    def test_losing_a_race_merges_and_retries(self):
        a = self.computer("A", config=self.cfg, state={})
        b = self.computer("B", config=self.cfg, state={})
        a.sync(self.acct)
        b.sync(self.acct, at=1)
        a.write("state", {"history": [history_entry("import", "2026-09-19T15:00:00", "2026-09-21", ["a1"])]}, when=minutes(9))
        b.write("state", {"history": [history_entry("import", "2026-09-19T15:30:00", "2026-09-28", ["b1"])]}, when=minutes(9))
        fired = []

        def b_saves_first(calendar_id, event_id):
            if not fired:
                fired.append(1)
                self.acct.before_update = None
                b.sync(self.acct, at=10)      # B gets its update in just before A's is checked

        self.acct.before_update = b_saves_first
        r = a.sync(self.acct, at=11)
        self.assertTrue(fired)
        self.assertTrue(r.pushed)
        self.assertEqual(sorted(e["week"] for e in a.read("state")["history"]), ["2026-09-21", "2026-09-28"])
        b.sync(self.acct, at=12)
        self.assertEqual(sorted(e["week"] for e in b.read("state")["history"]), ["2026-09-21", "2026-09-28"])
        cal_id = a.files.load_meta()["calendar_id"]
        self.assertEqual(len(self.acct.events_in(cal_id, "head")), 1)

    def test_two_computers_creating_the_first_copy_at_once(self):
        a = self.computer("A", config=dict(self.cfg, display_name="A"), state={
            "history": [history_entry("import", "2026-09-19T15:00:00", "2026-09-21", ["a1"])]})
        b = self.computer("B", config=dict(self.cfg, display_name="B"), state={
            "history": [history_entry("import", "2026-09-19T15:30:00", "2026-09-28", ["b1"])]})
        fired = []

        def b_creates_first(calendar_id, body):
            kind = body["extendedProperties"]["private"]["sm_kind"]
            if kind == "head" and not fired:
                fired.append(1)
                self.acct.before_insert = None
                b.sync(self.acct, at=1)

        self.acct.before_insert = b_creates_first
        a.sync(self.acct, at=2)
        b.sync(self.acct, at=3)
        a.sync(self.acct, at=4)
        cal_id = a.files.load_meta()["calendar_id"]
        self.assertEqual(len(self.acct.events_in(cal_id, "head")), 1)
        for c in (a, b):
            self.assertEqual(sorted(e["week"] for e in c.read("state")["history"]), ["2026-09-21", "2026-09-28"])

    def test_gives_up_cleanly_if_it_keeps_losing(self):
        a = self.computer("A", config=self.cfg, state={})
        a.sync(self.acct)
        a.write("state", {"history": [history_entry("import", "2026-09-19T15:00:00", "w", ["1"])]}, when=minutes(9))

        def always_beaten(calendar_id, event_id):
            ev = self.acct.events_by_cal[calendar_id][event_id]
            ev["etag"] = ev["etag"] + "x"

        self.acct.before_update = always_beaten
        with self.assertRaises(sync.SyncError):
            a.sync(self.acct, at=10)
        self.assertEqual(len(self.acct.events_in(a.files.load_meta()["calendar_id"], "chunk")), 1)  # no litter left

    def test_offline_raises_and_leaves_files_alone(self):
        a = self.computer("A", config=self.cfg, state={"weeks": {}})
        a.sync(self.acct)
        before = {s: a.path(s).read_bytes() for s in ("config", "state")}
        self.acct.offline = True
        with self.assertRaises(OSError) as ctx:
            a.sync(self.acct, at=5)
        self.assertTrue(core.is_network_error(ctx.exception))
        self.assertEqual({s: a.path(s).read_bytes() for s in before}, before)

    def test_damaged_local_file_is_not_uploaded(self):
        a = self.computer("A", config=self.cfg)
        a.path("state").write_text("{ broken", encoding="utf-8")
        with self.assertRaises(sync.SyncError):
            a.sync(self.acct)
        self.assertEqual(self.acct.calendars_by_id, {})
        self.assertEqual(a.path("state").read_text(encoding="utf-8"), "{ broken")

    def test_file_saved_by_the_app_mid_sync_is_not_overwritten(self):
        a = self.computer("A", config=self.cfg, state={})
        b = self.computer("B", config=self.cfg, state={})
        b.write("state", {"history": [history_entry("import", "2026-09-19T15:00:00", "2026-09-21", ["b1"])]}, when=minutes(1))
        b.sync(self.acct, at=1)
        a.sync(self.acct, at=2)
        mine = {"history": [history_entry("import", "2026-09-19T15:00:00", "2026-09-21", ["b1"]),
                            history_entry("quick_add", "2026-09-19T16:00:00", "x", ["mine"])]}
        real_read = a.files.read

        def read_then_app_saves():
            data = real_read()
            a.write("state", mine)          # the app writes state.json right after we read it
            return data

        a.files.read = read_then_app_saves
        b.write("state", {"history": [history_entry("import", "2026-09-19T15:00:00", "2026-09-21", ["b1"]),
                                      history_entry("import", "2026-09-19T15:10:00", "2026-09-28", ["b2"])]}, when=minutes(20))
        b.sync(self.acct, at=20)
        r = a.sync(self.acct, at=21)
        self.assertTrue(r.interrupted)
        self.assertEqual(a.read("state"), mine)   # untouched
        a.files.read = real_read
        a.sync(self.acct, at=22)                   # next time it merges properly
        self.assertEqual(sorted(e["action"] + e["week"] for e in a.read("state")["history"]),
                         ["import2026-09-21", "import2026-09-28", "quick_addx"])

    def test_newer_format_asks_for_an_update(self):
        a = self.computer("A", config=self.cfg)
        a.sync(self.acct)
        cal_id = a.files.load_meta()["calendar_id"]
        for chunk in self.acct.events_in(cal_id, "chunk"):
            doc = sync.decode_doc(chunk["description"])
            doc["format"] = 99
            chunk["description"] = sync.encode_doc(doc)
        with self.assertRaises(sync.SyncError) as ctx:
            a.sync(self.acct, at=3)
        self.assertIn("newer", str(ctx.exception))

    def test_missing_chunk_is_reported_not_overwritten(self):
        a = self.computer("A", config=self.cfg)
        a.sync(self.acct)
        cal_id = a.files.load_meta()["calendar_id"]
        for chunk in self.acct.events_in(cal_id, "chunk"):
            del self.acct.events_by_cal[cal_id][chunk["id"]]
        with self.assertRaises(sync.SyncError):
            a.sync(self.acct, at=3)
        self.assertEqual(len(self.acct.events_in(cal_id, "head")), 1)

    def test_garbled_head_is_reported(self):
        a = self.computer("A", config=self.cfg)
        a.sync(self.acct)
        cal_id = a.files.load_meta()["calendar_id"]
        self.acct.events_in(cal_id, "head")[0]["description"] = "not json"
        with self.assertRaises(sync.SyncError):
            a.sync(self.acct, at=3)

    def test_if_two_calendars_exist_the_lowest_id_is_used(self):
        self.acct.add_calendar(sync.CALENDAR_NAME, "zzz")
        self.acct.add_calendar(sync.CALENDAR_NAME, "aaa")
        a = self.computer("A", config=self.cfg)
        a.sync(self.acct)
        self.assertEqual(a.files.load_meta()["calendar_id"], "aaa")

    def test_deleted_calendar_is_recreated_from_local_copy(self):
        a = self.computer("A", config=dict(self.cfg, display_name="Still here"), state={})
        a.sync(self.acct)
        old = a.files.load_meta()["calendar_id"]
        del self.acct.calendars_by_id[old]
        del self.acct.events_by_cal[old]
        r = a.sync(self.acct, at=5)
        self.assertTrue(r.first_upload)
        self.assertNotEqual(a.files.load_meta()["calendar_id"], old)
        b = self.computer("B", config=make_config(sync_enabled=True))
        b.sync(self.acct, at=6)
        self.assertEqual(b.read("config")["display_name"], "Still here")

    def test_other_calendars_are_never_touched(self):
        primary = self.acct.add_calendar("Personal", "primary")
        self.acct.events_by_cal[primary]["mine"] = {"id": "mine", "summary": "Dentist", "etag": '"0"', "created": "2026-01-01T00:00:00Z"}
        a = self.computer("A", config=self.cfg, state={})
        a.sync(self.acct)
        a.write("state", {"history": [history_entry("import", "2026-09-19T15:00:00", "w", ["1"])]}, when=minutes(5))
        a.sync(self.acct, at=5)
        self.assertEqual(list(self.acct.events_by_cal[primary]), ["mine"])

    def test_pre_sync_backup_is_kept_small(self):
        a = self.computer("A", config=self.cfg, state={})
        a.sync(self.acct)
        for i in range(1, 15):
            b = self.computer(f"B{i}", config=make_config(sync_enabled=True, display_name=f"n{i}"))
            b.sync(self.acct, at=i)
        for c in self.computers[1:]:
            self.assertLessEqual(len(list((c.dir / "backups").glob("pre-sync-*.zip"))), 10)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.names = ("CONFIG_PATH", "STATE_PATH", "PREFS_PATH", "BACKUP_DIR", "build_services")
        self.saved = {n: getattr(core, n) for n in self.names}
        core.CONFIG_PATH, core.STATE_PATH, core.PREFS_PATH, core.BACKUP_DIR = d / "config.json", d / "state.json", d / "p.json", d / "b"
        self.acct = FakeGoogleAccount()
        core.build_services = lambda: (None, self.acct)
        core.CONFIG_PATH.write_text(json.dumps(make_config(sync_enabled=True)), encoding="utf-8")

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(core, n, v)
        self.tmp.cleanup()

    def run_sync(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            core.cmd_sync(None)
        return out.getvalue()

    def test_sync_command_uploads_then_reports_in_sync(self):
        self.assertIn("Uploaded", self.run_sync())
        self.assertIn("Already in sync", self.run_sync())

    def test_sync_command_when_turned_off(self):
        core.CONFIG_PATH.write_text(json.dumps(make_config(sync_enabled=False)), encoding="utf-8")
        self.assertIn("turned off", self.run_sync())
        self.assertEqual(self.acct.calendars_by_id, {})

    def test_import_style_commands_sync_before_and_after(self):
        import io
        from contextlib import redirect_stdout
        seen = []
        real = sync.sync_quietly
        sync.sync_quietly = lambda *a, **k: seen.append("sync")
        try:
            argv = sys.argv
            sys.argv = ["dominos_schedule.py", "history"]
            with redirect_stdout(io.StringIO()):
                core.main()
            self.assertEqual(seen, [])                     # read-only commands don't sync
            sys.argv = ["dominos_schedule.py", "undo"]
            with redirect_stdout(io.StringIO()):
                try:
                    core.main()
                except SystemExit:
                    pass
            self.assertGreaterEqual(len(seen), 1)         # commands that change history do
        finally:
            sync.sync_quietly = real
            sys.argv = argv


class EnabledTests(unittest.TestCase):
    def test_on_unless_switched_off(self):
        self.assertTrue(sync.enabled({}) is False)          # no config at all: nothing to sync
        self.assertTrue(sync.enabled({"a": 1}))
        self.assertTrue(sync.enabled({"sync_enabled": True}))
        self.assertFalse(sync.enabled({"sync_enabled": False}))

    def test_sync_quietly_skips_when_off_and_reports_problems(self):
        import io
        from contextlib import redirect_stdout
        self.assertIsNone(sync.sync_quietly(object(), {"sync_enabled": False}))
        out = io.StringIO()
        acct = FakeGoogleAccount()
        acct.offline = True
        tmp = tempfile.TemporaryDirectory()
        saved = core.STATE_PATH, core.CONFIG_PATH, core.PREFS_PATH, core.BACKUP_DIR
        core.STATE_PATH, core.CONFIG_PATH = Path(tmp.name) / "state.json", Path(tmp.name) / "config.json"
        core.PREFS_PATH, core.BACKUP_DIR = Path(tmp.name) / "p.json", Path(tmp.name) / "b"
        try:
            with redirect_stdout(out):
                self.assertIsNone(sync.sync_quietly(acct, make_config(sync_enabled=True)))
        finally:
            core.STATE_PATH, core.CONFIG_PATH, core.PREFS_PATH, core.BACKUP_DIR = saved
            tmp.cleanup()
        self.assertIn("Sync skipped: offline", out.getvalue())


if __name__ == "__main__":
    unittest.main()
