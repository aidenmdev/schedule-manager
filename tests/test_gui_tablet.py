"""The Tablet tab in Settings, with the server calls replaced. Run:  .venv\\Scripts\\python.exe -m unittest tests.test_gui_tablet -v"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
import tablet_server as ts
from tests.gui_env import Env, pump


class GuiTabletTests(unittest.TestCase):
    def setUp(self):
        self.env = Env().install()
        self.state = {"running": False, "ip": "192.168.0.50", "start_ok": True}
        self.calls = []
        self.saved = {n: getattr(ts, n) for n in ("tablet_status", "start_background", "stop_processes")}
        ts.tablet_status = lambda port: (self.calls.append(("status", port)) or
                                         {"running": self.state["running"], "ip": self.state["ip"], "port": port,
                                          "url": f"http://{self.state['ip']}:{port}"})

        def start(port):
            self.calls.append(("start", port))
            self.state["running"] = self.state["start_ok"]
            return self.state["start_ok"]

        def stop(port=None):
            self.calls.append(("stop", port))
            was, self.state["running"] = self.state["running"], False
            return was
        ts.start_background, ts.stop_processes = start, stop
        self.app = self.env.g.App()
        pump(self.app, 1.5)
        self.app.show_page("settings")
        pump(self.app, 0.5)
        self.sp = self.app.pages["settings"]

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(ts, name, fn)
        try:
            self.app.destroy()
        except Exception:
            pass
        self.env.uninstall()

    def open_tab(self):
        self.sp.tab_bar.set("Tablet")
        self.sp._show_tab("Tablet")
        pump(self.app, 0.8)

    def wait(self, predicate, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            pump(self.app, 0.15)
            if predicate():
                return True
        return False

    def test_shows_the_address_to_type_on_the_tablet(self):
        self.open_tab()
        self.assertEqual(self.sp.tablet_state.cget("text"), "Stopped")
        self.assertEqual(self.sp.tablet_url.cget("text"), "http://192.168.0.50:8765")
        self.assertEqual(str(self.sp.tablet_start_btn.cget("state")), "normal")
        self.assertEqual(str(self.sp.tablet_stop_btn.cget("state")), "disabled")
        self.assertEqual(self.env.errors, "")

    def test_looks_at_the_server_when_the_tab_opens(self):
        before = len(self.calls)
        self.open_tab()
        self.assertGreater(len(self.calls), before)
        self.assertEqual(self.calls[-1], ("status", 8765))

    def test_start_button_starts_it_and_the_screen_follows(self):
        self.open_tab()
        self.sp.tablet_start_btn.invoke()
        self.assertTrue(self.wait(lambda: self.sp.tablet_state.cget("text") == "Running"))
        self.assertIn(("start", 8765), self.calls)
        self.assertEqual(str(self.sp.tablet_start_btn.cget("state")), "disabled")
        self.assertEqual(str(self.sp.tablet_stop_btn.cget("state")), "normal")
        self.assertIn("same Wi-Fi", self.sp.tablet_hint.cget("text"))

    def test_stop_button_stops_it(self):
        self.state["running"] = True
        self.open_tab()
        self.assertEqual(self.sp.tablet_state.cget("text"), "Running")
        self.sp.tablet_stop_btn.invoke()
        self.assertTrue(self.wait(lambda: self.sp.tablet_state.cget("text") == "Stopped"))
        self.assertIn(("stop", 8765), self.calls)
        self.assertEqual(str(self.sp.tablet_start_btn.cget("state")), "normal")

    def test_a_display_that_will_not_start_is_reported(self):
        self.state["start_ok"] = False
        toasts = []
        original = self.app.toast
        self.app.toast = lambda text, *a, **k: (toasts.append(text), original(text, *a, **k))[1]
        self.open_tab()
        self.sp.tablet_start_btn.invoke()
        self.assertTrue(self.wait(lambda: any("didn't start" in t for t in toasts)))
        self.assertTrue(self.wait(lambda: self.sp.tablet_state.cget("text") == "Stopped"))

    def test_copy_puts_the_address_on_the_clipboard(self):
        self.open_tab()
        self.sp.tablet_copy_btn.invoke()
        self.assertEqual(self.sp.clipboard_get(), "http://192.168.0.50:8765")

    def test_no_network_is_explained(self):
        self.state["ip"] = "127.0.0.1"
        self.open_tab()
        self.assertEqual(self.sp.tablet_url.cget("text"), "No network found")
        self.assertIn("Wi-Fi", self.sp.tablet_hint.cget("text"))
        self.assertEqual(str(self.sp.tablet_copy_btn.cget("state")), "disabled")

    def test_the_port_is_saved_and_used(self):
        self.open_tab()
        self.sp.vars["tablet_port"].delete(0, "end")
        self.sp.vars["tablet_port"].insert(0, "9321")
        self.sp.save()
        self.assertEqual(json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))["tablet_port"], 9321)
        self.sp.tablet_start_btn.invoke()
        self.assertTrue(self.wait(lambda: ("start", 9321) in self.calls))

    def test_a_bad_port_is_refused_and_nothing_is_saved(self):
        self.open_tab()
        before = core.CONFIG_PATH.read_text(encoding="utf-8")
        for bad in ("80", "70000", "abc", "-5"):
            self.sp.vars["tablet_port"].delete(0, "end")
            self.sp.vars["tablet_port"].insert(0, bad)
            self.sp.save()
            self.assertEqual(core.CONFIG_PATH.read_text(encoding="utf-8"), before, bad)
        self.assertEqual(self.sp._tablet_port(), 8765)     # an unusable value never reaches the server calls

    def test_the_port_does_not_travel_between_computers(self):
        import sync
        self.assertNotIn("tablet_port", sync.synced_config({"tablet_port": 9000, "a": 1}))

    def test_stopping_and_starting_keep_the_rest_of_settings_intact(self):
        self.open_tab()
        self.sp.tablet_start_btn.invoke()
        self.assertTrue(self.wait(lambda: self.sp.tablet_state.cget("text") == "Running"))
        self.sp.save()
        self.assertEqual(json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))["job_wages"], self.env.cfg["job_wages"])
        self.assertEqual(self.env.errors, "")


if __name__ == "__main__":
    unittest.main()
