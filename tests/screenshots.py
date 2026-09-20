"""Save a PNG of each page (fake data, isolated config) without showing a window.
Usage: python -m tests.screenshots OUT_DIR [page ...] [scale=1.0]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core
from tests.gui_env import Env, pump
from tests.snap import snap


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
    out.mkdir(parents=True, exist_ok=True)
    opts = {a.split("=")[0]: a.split("=")[1] for a in sys.argv[2:] if "=" in a}
    pages = [a for a in sys.argv[2:] if "=" not in a]
    import customtkinter as ctk
    if "scale" in opts:  # emulate a different display scaling, e.g. scale=1.0 for a 100% screen
        ctk.set_window_scaling(float(opts["scale"]) / 1.5)
        ctk.set_widget_scaling(float(opts["scale"]) / 1.5)
    env = Env().install()
    try:
        results = core.find_schedule_emails(env.gmail, env.cfg, 10)
        old = next(p for p in results if p.email_id == "old-week")
        core.perform_import(env.gmail, env.cal, env.cfg, core.StateStore(core.STATE_PATH), old,
                            send_report=False, log=lambda *_: None)
        app = env.g.App()
        pump(app, 3)
        for key, _t in env.g.App.NAV:
            if pages and key not in pages:
                continue
            app.show_page(key)
            pump(app, 1.5)
            snap(app, out / f"{key}.png")
            print("saved", key)
        app.destroy()
    finally:
        env.uninstall()


if __name__ == "__main__":
    main()
