#!/usr/bin/env python3
"""
Schedule Manager - desktop app.

Double-click "Schedule Manager.vbs" (no console window), or run:  python schedule_gui.py
"""
from __future__ import annotations

import ctypes
import functools
import gc
import hashlib
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, ttk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

import dominos_schedule as core
import sync
import updater
from month_view import MonthGrid, month_grid_bounds
from timeline_view import DayTimeline
from week_view import WeekGrid

BASE = core.BASE_DIR
PREFS_PATH = core.PREFS_PATH
LOG_PATH = BASE / "app.log"
ICON_ICO = core.RESOURCE_DIR / "app.ico"
ICON_PNG = core.RESOURCE_DIR / "app_icon.png"

C = {
    "bg": "#090c13", "sidebar": "#0c1018", "card": "#111725", "card2": "#19212f", "border": "#222b3d",
    "text": "#eaeef7", "muted": "#8b96ab", "dim": "#5b667c",
    "accent": "#6b8afd", "accent_hover": "#8aa1ff", "accent_soft": "#1b2645", "select": "#232f52", "today_bg": "#131b33",
    "success": "#3fdc98", "warning": "#ffb454", "danger": "#ff6b74", "danger_bg": "#c02f3b",
}
cat_color = core.category_color

NAV_ICONS = {"dashboard": "\ue80f", "week": "\ue8c0", "month": "\ue787", "plan": "\ue7be", "earnings": "\ue8c7",
             "import": "\ue896", "manage": "\ue71d", "report": "\ue8a5", "settings": "\ue713", "help": "\ue897"}
ICON_FONT_FILES = (r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf")
WHEEL = {"px": 96}


@functools.lru_cache(maxsize=None)
def icon_image(glyph: str, color: str, size: int = 20):
    """A crisp icon from the Windows icon font as a CTkImage, or None if this computer doesn't have the font."""
    for path in ICON_FONT_FILES:
        if os.path.exists(path):
            big = size * 4
            img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((big / 2, big / 2), glyph, font=ImageFont.truetype(path, int(big * 0.78)),
                                     fill=color, anchor="mm")
            return ctk.CTkImage(img, size=(size, size))
    return None


class Scroller:
    """Wheel and trackpad input pile up as a pending distance that is spent a little each frame, so scrolling stays
    smooth and never falls behind, however many events arrive or however heavy the page is to redraw."""
    FRAME_MS = 9

    def __init__(self, root):
        self.root = root
        self.pending: dict = {}
        self.running = False
        self._job = None

    def add(self, canvas, pixels: float):
        try:
            top, bottom = canvas.yview()
        except tk.TclError:
            return
        if bottom - top >= 0.995:  # everything already fits
            return
        self.pending[canvas] = self.pending.get(canvas, 0.0) + pixels
        if not self.running:
            self.running = True
            self._job = self.root.after(1, self._step)

    def cancel(self):
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
        self.pending.clear()
        self.running = False
        self._job = None

    def _step(self):
        for canvas, left in list(self.pending.items()):
            step = int(round(left * 0.3))
            if step == 0:
                step = 1 if left > 0.5 else -1 if left < -0.5 else 0
            try:
                before = canvas.yview()
                if step:
                    canvas.yview_scroll(step, "units")  # 1 unit = 1 pixel (yscrollincrement is 1)
                after = canvas.yview()
            except tk.TclError:
                del self.pending[canvas]
                continue
            left -= step
            if abs(left) < 0.5 or after == before:
                del self.pending[canvas]
            else:
                self.pending[canvas] = left
        if self.pending:
            self._job = self.root.after(self.FRAME_MS, self._step)
        else:
            self.running = False
            self._job = None


SCROLLER: Scroller | None = None


def _smooth_wheel(self, event):
    """CustomTkinter scrolls ~20px per notch, which feels stuck; this moves about three lines, smoothly."""
    if SCROLLER is None or not self._check_if_valid_scroll(event.widget):
        return
    SCROLLER.add(self._parent_canvas, -event.delta / 120 * WHEEL["px"])


ctk.CTkScrollableFrame._mouse_wheel_all = _smooth_wheel


class ScrollFrame(ctk.CTkScrollableFrame):
    """A scrolling panel with pixel-exact scrolling and a slim scrollbar."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("scrollbar_button_color", C["card2"])
        kw.setdefault("scrollbar_button_hover_color", C["border"])
        super().__init__(master, **kw)
        self._parent_canvas.configure(yscrollincrement=1)
        try:
            self._scrollbar.configure(width=10)
        except (tk.TclError, ValueError):
            pass


def scroll_frame(parent, **kw):
    return ScrollFrame(parent, **kw)


def load_prefs() -> dict:
    prefs = {"auto_check_email": True, "size": "1240x760"}
    try:
        prefs.update(json.loads(PREFS_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return prefs


def save_prefs(prefs: dict):
    try:
        PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except OSError:
        pass


def log_error(text: str):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except OSError:
        pass


def friendly_error(e: BaseException) -> str:
    text = str(e) or type(e).__name__
    low = text.lower()
    if "invalid_grant" in low or type(e).__name__ == "RefreshError":
        return "Google login expired. Go to Settings > Reset Google login, then try again."
    if "getaddrinfo" in low or "connection aborted" in low or "timed out" in low:
        return "Can't reach Google. Check your internet connection."
    return text if len(text) < 300 else text[:297] + "..."


def hidden_mode() -> bool:
    """Automated tests set this so their windows are invisible instead of flashing on screen."""
    return bool(os.environ.get("SCHEDULE_MANAGER_HIDDEN"))


MUTEX_NAME = "ScheduleManager.v2.SingleInstance"
if os.environ.get("SCHEDULE_MANAGER_DATA"):  # a copy with its own data folder (for testing) is its own instance
    MUTEX_NAME += "." + hashlib.md5(os.path.normcase(os.environ["SCHEDULE_MANAGER_DATA"]).encode()).hexdigest()[:8]
_mutex_handle = None  # kept alive for the whole process so the mutex exists while the app runs


def acquire_single_instance(name: str | None = None):
    """(is_first_instance, handle). Keep the handle alive for the life of the process."""
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name or MUTEX_NAME)
        already = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True, None
    return (not already), handle


def focus_existing_window(title: str) -> bool:
    """Bring an already-running copy of the app to the front."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except (AttributeError, OSError):
        return False


@functools.lru_cache(maxsize=None)
def font(size=13, weight="normal", family="Segoe UI"):
    # cached: Tk font objects must never be garbage collected from a worker thread
    return ctk.CTkFont(family=family, size=size, weight=weight)


def label(parent, text="", size=13, weight="normal", color=None, **kw):
    return ctk.CTkLabel(parent, text=text, font=font(size, weight), text_color=color or C["text"], **kw)


def card(parent, **kw):
    kw.setdefault("fg_color", C["card"])
    kw.setdefault("corner_radius", 16)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", C["border"])
    return ctk.CTkFrame(parent, **kw)


def button(parent, text, command, kind="normal", width=None, **kw):
    styles = {  # fill, hover, text, border
        "primary": (C["accent"], C["accent_hover"], "#ffffff", 0),
        "normal": (C["card2"], C["border"], C["text"], 0),
        "danger": (C["danger_bg"], C["danger"], "#ffffff", 0),
        "ghost": ("transparent", C["card2"], C["muted"], 0),
    }
    fg, hover, tc, border = styles[kind]
    if width:
        kw["width"] = width
    return ctk.CTkButton(parent, text=text, command=command, fg_color=fg, hover_color=hover, text_color=tc,
                         text_color_disabled=C["dim"], corner_radius=10, height=36, border_width=border,
                         font=font(13, "bold"), **kw)


def entry(parent, width=200, placeholder="", textvariable=None):
    return ctk.CTkEntry(parent, width=width, height=36, fg_color=C["card2"], border_color=C["border"],
                        text_color=C["text"], placeholder_text=placeholder, textvariable=textvariable,
                        corner_radius=10, font=font(13))


def segmented(parent, values=(), command=None, width=None, size=12, **_ignored):
    box = ctk.CTkSegmentedButton(parent, values=values, command=command, selected_color=C["accent"],
                                 selected_hover_color=C["accent_hover"], unselected_color=C["card2"],
                                 unselected_hover_color=C["border"], fg_color=C["card2"], text_color=C["text"],
                                 corner_radius=10, font=font(size, "bold"), height=34)
    if width:
        box.configure(width=width)
    return box


def option_menu(parent, values=(), command=None, width=140, **_ignored):
    return ctk.CTkOptionMenu(parent, values=values, command=command, width=width, height=36, fg_color=C["card2"],
                             button_color=C["card2"], button_hover_color=C["border"], dropdown_fg_color=C["card"],
                             dropdown_hover_color=C["card2"], dropdown_text_color=C["text"], text_color=C["text"],
                             corner_radius=10, font=font(13, "bold"), dropdown_font=font(13))


def switch(parent, text="", command=None, size=13, **_ignored):
    return ctk.CTkSwitch(parent, text=text, command=command, progress_color=C["accent"], button_color="#ffffff",
                         button_hover_color="#dfe6ff", fg_color=C["border"], font=font(size), text_color=C["text"])


def textbox(parent, mono=True, size=13, **kw):
    return ctk.CTkTextbox(parent, fg_color=C["card2"], text_color=C["text"], corner_radius=10,
                          font=font(size, family="Consolas" if mono else "Segoe UI"), border_width=0, **kw)


def set_text(box, text: str, line_colors=None):
    """Replace a read-only text box's contents. line_colors: optional list (one per line) of hex colors/None."""
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    if line_colors:
        for i, color in enumerate(line_colors, start=1):
            if color:
                tag = "c_" + color.lstrip("#")
                box._textbox.tag_config(tag, foreground=color)
                box._textbox.tag_add(tag, f"{i}.0", f"{i}.end")
    box.configure(state="disabled")


def make_tree(parent, columns, height=10, selectmode="browse"):
    """columns: list of (id, heading, width, anchor). Returns (frame, tree)."""
    frame = ctk.CTkFrame(parent, fg_color=C["card"], corner_radius=16, border_width=1, border_color=C["border"])
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", style="App.Treeview",
                        height=height, selectmode=selectmode)
    for cid, head, width, anchor in columns:
        tree.heading(cid, text=head, anchor="w")
        tree.column(cid, width=width, anchor=anchor, stretch=True)
    sb = ctk.CTkScrollbar(frame, command=tree.yview, width=10, button_color=C["card2"], button_hover_color=C["border"])
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", padx=(0, 4), pady=8)
    tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
    frame.empty = label(frame, "", 13, color=C["muted"], justify="center")
    return frame, tree


def show_empty(frame, text):
    """Show a centered hint over a table when it has no rows (text=None hides it)."""
    if text:
        frame.empty.configure(text=text)
        frame.empty.place(relx=0.5, rely=0.42, anchor="center")
        frame.empty.lift()
    else:
        frame.empty.place_forget()


def make_sortable(tree: ttk.Treeview, numeric: set = frozenset()):
    state = {}

    def sort_by(col):
        rows = [(tree.set(k, col), k) for k in tree.get_children("")]
        reverse = state.get(col, False)

        def key(item):
            if col in numeric:
                try:
                    return float(item[0].replace("$", "").replace("h", "").replace(",", "").strip() or 0)
                except ValueError:
                    return 0.0
            return item[0].lower()

        rows.sort(key=key, reverse=reverse)
        for i, (_, k) in enumerate(rows):
            tree.move(k, "", i)
        state[col] = not reverse

    for col in tree["columns"]:
        tree.heading(col, command=lambda c=col: sort_by(c))


def fmt_stamp(iso: str) -> str:
    """'2026-09-18T14:07:03' -> 'Sep 18, 2:07 PM' (falls back to the raw text if it can't be read)."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{dt:%b} {dt.day}, {core.fmt_time(dt)}"


def humanize_delta(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "now"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


def when_text(ev: dict, with_day=False) -> str:
    t = "All day" if ev["all_day"] else f"{core.fmt_time(ev['start'])} - {core.fmt_time(ev['end'])}"
    if with_day:
        return f"{core.DAY_ABBR[ev['day'].weekday()]} {core.fmt_date_short(ev['day'])}  ·  {t}"
    return t


def event_row(parent, ev, with_day=False, muted=False):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkFrame(row, width=4, height=36, fg_color=cat_color(ev["category"]), corner_radius=2).pack(
        side="left", fill="y", padx=(0, 10), pady=2)
    box = ctk.CTkFrame(row, fg_color="transparent")
    box.pack(side="left", fill="x", expand=True)
    label(box, ev["summary"], 13, "bold", C["muted"] if muted else C["text"], anchor="w").pack(anchor="w")
    label(box, when_text(ev, with_day), 12, color=C["muted"], anchor="w").pack(anchor="w")
    return row


class Modal(ctk.CTkToplevel):
    def __init__(self, app, title, width=460):
        super().__init__(app, fg_color=C["card"])
        if hidden_mode():
            self.attributes("-alpha", 0.0)
        self.app = app
        self.result = None
        self._dlg_w = width
        self.title(title)
        self.resizable(False, False)
        self.withdraw()
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=24, pady=22)
        self.bind("<Escape>", lambda _e: self.close(None))
        if ICON_ICO.exists():
            self.after(250, lambda: self._set_icon())

    def show(self):
        self.update_idletasks()
        w, h = int(self._dlg_w * self.app.scale), self.winfo_reqheight()
        x = self.app.winfo_rootx() + (self.app.winfo_width() - w) // 2
        y = self.app.winfo_rooty() + max((self.app.winfo_height() - h) // 3, 20)
        tk.Wm.geometry(self, f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        self._shown = True
        self.transient(self.app)
        self.deiconify()
        self.after(60, self._grab)
        self.app.wait_window(self)

    def fit(self):
        """Resize to the content (used when rows are added after the dialog is already open)."""
        if getattr(self, "_shown", False):
            self.update_idletasks()
            tk.Wm.geometry(self, f"{int(self._dlg_w * self.app.scale)}x{self.winfo_reqheight()}")

    def _set_icon(self):
        try:
            self.iconbitmap(str(ICON_ICO))
        except tk.TclError:
            pass

    def _grab(self):
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass

    def close(self, result=None):
        self.result = result
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class LazyPages(dict):
    """app.pages[key] builds a page the first time it is asked for; .get(key) only returns pages that already exist."""

    def __init__(self, app):
        super().__init__()
        self.app = app

    def __missing__(self, key):
        if key not in self.app.PAGES:
            raise KeyError(key)
        page = globals()[self.app.PAGES[key]](self.app.content, self.app)
        self[key] = page
        return page


class App(ctk.CTk):
    NAV = [("dashboard", "Dashboard"), ("week", "Week"), ("month", "Month"), ("plan", "Study planner"), ("earnings", "Earnings"),
           ("import", "Import"), ("manage", "Manage weeks"), ("report", "Report"), ("settings", "Settings"),
           ("help", "About & help")]
    NAV_SECTIONS = {"dashboard": "", "import": "SCHEDULE", "settings": "APP"}  # a small heading above these items

    def __init__(self):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScheduleManager.v2")
        except (AttributeError, OSError):
            pass
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__(fg_color=C["bg"])
        icon_image.cache_clear()  # images belong to one Tk root; a fresh window needs fresh ones
        if hidden_mode():
            self.attributes("-alpha", 0.0)
        self.title(f"Schedule Manager v{core.APP_VERSION}")
        self.prefs = load_prefs()
        self.configure(fg_color=C["bg"])
        self.minsize(980, 620)
        if hidden_mode():
            self.geometry("1440x880+-20000+0")  # tests: a fixed size, off screen
        else:
            self._open_maximized()
        if ICON_ICO.exists():
            try:
                self.iconbitmap(str(ICON_ICO))
                self.after(300, lambda: self.iconbitmap(str(ICON_ICO)))
            except tk.TclError:
                pass

        try:
            self.scale = max(1.0, float(ctk.ScalingTracker.get_window_scaling(self)))
        except Exception:
            self.scale = 1.0
        WHEEL["px"] = 96 * self.scale
        global SCROLLER
        SCROLLER = Scroller(self)

        # Tk objects must only be freed on the main thread: run the cyclic GC from _poll instead of
        # letting it fire inside background workers.
        gc.disable()
        self._polls = 0
        self.write_lock = threading.Lock()  # held by jobs that change state.json, so they never interleave
        self.queue = queue.Queue()
        self.config_data = None
        self.store = None
        self.report_cache = {}
        self._toast = None
        self._sync_running = False
        self._sync_again = False
        self._sync_timer = None
        self._sync_last_ok = 0.0
        self._sync_last_error = ""
        self.sync_text = ""
        self.update_check = None
        self.update_text = ""
        self._update_running = False
        self.config_stamp = 0
        self._load_config()
        self._build_ui()
        self.show_page("dashboard")
        self.after(1500, self._prebuild_pages)
        self.bind("<F5>", lambda _e: self.pages[self.current].refresh())
        self.bind("<F11>", lambda _e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))
        self.bind("<Escape>", lambda _e: self.attributes("-fullscreen", False), add="+")
        self.bind("<Control-n>", lambda _e: self.quick_add())
        for combo in ("<Control-f>", "<Control-F>"):
            self.bind(combo, lambda _e: self.search())
        for combo in ("<Control-k>", "<Control-K>"):
            self.bind(combo, lambda _e: self.palette())
        for i, (key, _t) in enumerate(self.NAV, start=1):
            self.bind(f"<Control-Key-{i % 10}>", lambda _e, k=key: self.show_page(k))

        # Trackpads (Windows precision touchpads) send <TouchpadScroll>; Tk 9 handles it for tables and text
        # boxes but not for canvases, which is what scrollable panels and the week calendar are.
        self.bind_all("<TouchpadScroll>", self._on_touchpad, add="+")
        self.after(150, self._poll)
        self.after(400, self._startup)
        self.after(self.SYNC_EVERY_MS, self._periodic_sync)
        self.bind("<FocusIn>", self._on_focus, add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    SYNC_EVERY_MS = 6 * 60 * 1000

    def _open_maximized(self):
        """The window opens filling the screen (F11 goes fully fullscreen)."""
        self.wm_state("zoomed")

    PAGES = {"dashboard": "DashboardPage", "week": "WeekPage", "month": "MonthPage", "plan": "PlanPage",
             "earnings": "EarningsPage", "import": "ImportPage", "manage": "ManagePage", "report": "ReportPage",
             "settings": "SettingsPage", "help": "HelpPage"}

    def _build_ui(self):
        self._style_ttk()
        self.pages, self.nav_buttons, self.current = LazyPages(self), {}, None
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(side="left", fill="both", expand=True, padx=(4, 30), pady=(26, 20))
        self.pages["dashboard"]

    def _prebuild_pages(self):
        """Build the pages that haven't been opened yet, one at a time while the app is idle, so the first visit is instant."""
        for key in self.PAGES:
            if key not in self.pages:
                self.pages[key]
                self.after(120, self._prebuild_pages)
                return

    def _style_ttk(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        rh = int(34 * self.scale)
        s.configure("App.Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["text"],
                    borderwidth=0, rowheight=rh, font=("Segoe UI", round(10 * self.scale)),
                    bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"])
        s.map("App.Treeview", background=[("selected", C["select"])], foreground=[("selected", "#ffffff")])
        s.configure("App.Treeview.Heading", background=C["card"], foreground=C["muted"], relief="flat",
                    font=("Segoe UI", round(9 * self.scale), "bold"), padding=(8, 8), borderwidth=0)
        s.map("App.Treeview.Heading", background=[("active", C["card2"])])

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=240, fg_color=C["sidebar"], corner_radius=0)
        side.pack(side="left", fill="y", padx=(0, 26))
        side.pack_propagate(False)

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=22, pady=(28, 22))
        if ICON_PNG.exists():
            img = ctk.CTkImage(Image.open(ICON_PNG), size=(38, 38))
            ctk.CTkLabel(brand, image=img, text="").pack(side="left")
        names = ctk.CTkFrame(brand, fg_color="transparent")
        names.pack(side="left", padx=12)
        label(names, "Schedule", 17, "bold").pack(anchor="w")
        label(names, "Manager", 12, color=C["muted"]).pack(anchor="w")

        for key, text in self.NAV:
            heading = self.NAV_SECTIONS.get(key)
            if heading:
                label(side, heading, 10, "bold", C["dim"]).pack(anchor="w", padx=28, pady=(16, 4))
            b = ctk.CTkButton(side, text=f"   {text}", anchor="w", height=42, corner_radius=12, border_spacing=10,
                              fg_color="transparent", hover_color=C["card2"], text_color=C["muted"],
                              image=icon_image(NAV_ICONS[key], C["muted"]), compound="left",
                              font=font(14, "bold"), command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=14, pady=1)
            self.nav_buttons[key] = b

        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=22, pady=20)
        self.offline_lbl = label(foot, "", 12, "bold", C["warning"], anchor="w", wraplength=190, justify="left")
        self.account_lbl = label(foot, "Not connected yet", 12, color=C["muted"], anchor="w", wraplength=190)
        self.account_lbl.pack(anchor="w", pady=(0, 6))
        row = ctk.CTkFrame(foot, fg_color="transparent")
        row.pack(fill="x")
        self.dot = label(row, "\u25cf", 12, color=C["success"])
        self.dot.pack(side="left")
        self.status_lbl = label(row, "Ready", 12, color=C["muted"], anchor="w", wraplength=170, justify="left")
        self.status_lbl.pack(side="left", padx=6)

    def _load_config(self):
        self.config_stamp += 1
        if not core.CONFIG_PATH.exists() and core.CONFIG_EXAMPLE_PATH.exists():
            import shutil
            shutil.copy(core.CONFIG_EXAMPLE_PATH, core.CONFIG_PATH)
            try:
                sync.LocalFiles().forget_base()  # a rebuilt config is a fresh start: shared settings win over the template
            except OSError:
                pass
            self.after(800, lambda: self.toast("Created config.json from the template. Fill it in under Settings.", "warn"))
        try:
            self.config_data = core.load_config()
        except SystemExit as e:
            self.config_data = None
            message = str(e)  # `e` is deleted when this block ends, so capture the text for the delayed toast
            self.after(800, lambda: self.toast(message, "error", 9000))
        self.reload_store()

    def reload_store(self):
        """(Re)read state.json into the SAME store object (jobs hold references to it). A damaged file is reported, never silently replaced. Skipped while a state-changing job is running."""
        current = getattr(self, "store", None)
        if current is not None and self.write_lock.locked():
            return
        try:
            fresh = core.StateStore(core.STATE_PATH)
        except core.DataFileError as e:
            core.CACHE_DIR.mkdir(exist_ok=True)
            stand_in = core.StateStore(core.CACHE_DIR / "state-unreadable.json")  # real file stays untouched
            if current is None:
                self.store = stand_in
            else:
                current.data, current.path = stand_in.data, stand_in.path
            message = str(e)
            self.after(800, lambda: self.toast(message, "error", 9000))
            return
        if current is None:
            self.store = fresh
        else:
            current.data, current.path = fresh.data, fresh.path
        self.store.on_save = self._state_saved

    def _state_saved(self):
        """Called from whichever thread saved state.json; syncing is scheduled on the main thread."""
        self.queue.put(("ok", lambda _result: self.request_sync(), None))

    def request_sync(self, delay_ms: int = 2500):
        """Sync soon, and just once if several changes arrive close together."""
        if not sync.enabled(self.config_data):
            return
        if self._sync_timer is not None:
            self.after_cancel(self._sync_timer)

        def fire():
            self._sync_timer = None
            self.sync_now()
        self._sync_timer = self.after(delay_ms, fire)

    def _sync_service(self):
        return core.build_services()[1]

    def sync_now(self, manual: bool = False):
        """Merge this computer with the shared copy in Google. Quiet unless something changed or `manual`."""
        if not sync.enabled(self.config_data):
            if manual:
                self.toast("Sync is turned off on this computer.", "warn")
            return
        if self._sync_running:
            self._sync_again = True
            return
        self._sync_running = True
        tzname = self.config_data.get("timezone", "America/Los_Angeles")

        def work():
            return sync.sync_once(self._sync_service(), tzname=tzname)

        def done(result):
            self._sync_running = False
            self._sync_finished(result, manual)
            if self._sync_again:
                self._sync_again = False
                self.request_sync(1000)

        def failed(err):
            self._sync_running = False
            self._sync_again = False
            offline = core.is_network_error(err)
            self.sync_text = "Offline. Will sync when the internet is back." if offline else f"Sync problem: {friendly_error(err)}"
            self._update_sync_label()
            if not offline and not isinstance(err, sync.SyncError):
                log_error(f"sync failed: {err!r}")
            message = friendly_error(err)
            if manual or (not offline and message != self._sync_last_error):
                self.toast("Sync: " + message, "warn" if offline else "error", 7000)
            self._sync_last_error = "" if offline else message

        self.run_async(work, done, failed, exclusive=True)

    def _sync_finished(self, result, manual: bool):
        self._sync_last_ok = time.time()
        self._sync_last_error = ""
        self.sync_text = f"Last synced {core.fmt_time(datetime.now())}."
        if result.pulled:
            self._apply_pulled(result.pulled)
        if result.interrupted:
            self.request_sync(1500)
        if result.first_upload:
            self.toast("Sync is on. Your settings and history are now saved in a private calendar called "
                       "\"Schedule Manager sync data\" in your Google account.", "success", 9000)
        elif result.pulled:
            what = "Settings and history loaded from your Google account." if result.joined else \
                "Updated from your other computers."
            self.toast(what, "success")
        elif manual:
            self.toast("Already in sync.", "success")
        self._update_sync_label()

    def _apply_pulled(self, pulled, tries: int = 0):
        if self.write_lock.locked() and tries < 20:
            self.after(500, lambda: self._apply_pulled(pulled, tries + 1))
            return
        if "config" in pulled:
            self.reload_config()
        if "state" in pulled:
            self.reload_store()
        if "prefs" in pulled:
            fresh = load_prefs()
            self.prefs.update({k: fresh[k] for k in sync.SYNCED_PREFS if k in fresh})
        self.invalidate()
        page = self.pages.get(self.current)
        if page is not None and self.current != "settings":
            page.refresh()
        elif page is not None:
            page.on_show()

    def _update_sync_label(self):
        page = self.pages.get("settings")
        if page is not None:
            page.show_sync_status()

    def _periodic_sync(self):
        self.sync_now()
        self.maybe_check_updates()
        self.after(self.SYNC_EVERY_MS, self._periodic_sync)

    def _install_dir(self) -> Path:
        return Path(sys.executable).parent

    def _tablet_running(self) -> bool:
        try:
            import tablet_server
            return tablet_server.already_running(int((self.config_data or {}).get("tablet_port") or tablet_server.DEFAULT_PORT))
        except Exception:
            return False

    def maybe_check_updates(self):
        """At most twice a day, and never if the person turned it off."""
        if not updater.is_installed() or not self.prefs.get("auto_check_updates", True):
            return
        if time.time() - float(self.prefs.get("last_update_check", 0)) < 12 * 3600:
            return
        self.check_updates()

    def check_updates(self, manual: bool = False):
        info = updater.read_build_info()
        if not updater.is_installed() or info is None:
            if manual:
                self.toast("Updates are for the installed app. This copy runs straight from the source folder.", "warn")
            return
        if self._update_running:
            return
        self._update_running = True
        self.update_text = "Checking..."
        self._update_label()

        def work():
            return updater.find_updates(info)

        def done(check):
            self._update_running = False
            self.update_check = check
            self.prefs["last_update_check"] = time.time()
            save_prefs(self.prefs)
            if check.available:
                u = check.available
                self.update_text = "Update available: " + updater.describe_build(updater.BuildInfo(u.version, u.build, u.base)) + "."
                self.toast("A Schedule Manager update is available.", "info", 10000, action=("See it", self.offer_update))
            elif check.needs_new_installer:
                self.update_text = "A newer version exists, but it needs the latest Schedule Manager Setup file."
                if manual:
                    self.toast(self.update_text, "warn", 8000)
            else:
                self.update_text = "You're up to date."
                if manual:
                    self.toast("You're up to date.", "success")
            self._update_label()

        def failed(err):
            self._update_running = False
            offline = core.is_network_error(err)
            self.update_text = "Offline, so it couldn't check for updates." if offline else f"Couldn't check for updates: {friendly_error(err)}"
            self._update_label()
            if manual:
                self.toast(self.update_text, "warn" if offline else "error", 7000)

        self.run_async(work, done, failed)

    def offer_update(self):
        check = self.update_check
        update = check.available if check else None
        if update is None:
            self.check_updates(manual=True)
            return
        m = Modal(self, "Update Schedule Manager", width=540)
        label(m.body, f"Version {update.version} is ready", 18, "bold").pack(anchor="w")
        label(m.body, update.notes or "No notes were included.", 13, color=C["muted"], wraplength=480, justify="left").pack(anchor="w", pady=(8, 6))
        label(m.body, f"About {max(update.size / 1e6, 0.1):.1f} MB to download. Schedule Manager will close, update, and open again by itself.",
              12, color=C["muted"], wraplength=480, justify="left").pack(anchor="w", pady=(0, 14))
        row = ctk.CTkFrame(m.body, fg_color="transparent")
        row.pack(fill="x")
        button(row, "Update now", lambda: m.close(True), "primary", width=130).pack(side="right")
        button(row, "Later", lambda: m.close(False), "normal", width=100).pack(side="right", padx=8)
        m.show()
        if m.result:
            self.install_update(update)

    def install_update(self, update):
        if self.write_lock.locked():
            self.toast("Wait for the current task to finish, then try again.", "warn")
            return
        info = updater.read_build_info()
        install_dir = self._install_dir()
        work_dir = core.BASE_DIR / "update"

        def work():
            key = updater.load_public_key(core.RESOURCE_DIR / updater.KEY_NAME)
            package = updater.download(update)
            shutil.rmtree(work_dir, ignore_errors=True)
            return updater.stage(package, update, info, key, install_dir, work_dir / "stage")

        def done(staged):
            script = updater.apply_script(staged, install_dir, work_dir, os.getpid(), restart=True,
                                          restart_tablet=self._tablet_running(), failure_note=core.BASE_DIR / "update_failed.txt")
            self.toast("Updating. Schedule Manager will reopen in a moment.", "success", 6000)
            self.after(800, lambda: self._relaunch(script))

        self.run_job([], "Downloading the update...", work, done, exclusive=True)

    def _relaunch(self, script):
        if self.write_lock.locked():
            self.after(1000, lambda: self._relaunch(script))
            return
        updater.launch(script)
        self._on_close()

    def _update_label(self):
        page = self.pages.get("help")
        if page is not None:
            page.show_update_status()

    def _after_update_notice(self):
        failed = core.BASE_DIR / "update_failed.txt"
        if failed.exists():
            try:
                failed.unlink()
            except OSError:
                pass
            self.toast("The last update couldn't be installed, so the previous version was restored.", "warn", 9000)
        info = updater.read_build_info()
        if info is not None:
            seen = self.prefs.get("seen_build")
            if seen is not None and seen != info.build:
                self.toast(f"Schedule Manager was updated to version {info.version}.", "success", 7000)
            if seen != info.build:
                self.prefs["seen_build"] = info.build
                save_prefs(self.prefs)

    def _on_focus(self, event):
        if event.widget is self and time.time() - self._sync_last_ok > 120:
            self.sync_now()

    def load_raw_config(self):
        """config.json as a dict for editing, or None (with an error shown) if it is damaged."""
        try:
            return core.read_json_safe(core.CONFIG_PATH)
        except core.DataFileError as e:
            self.toast(str(e), "error", 9000)
            return None
        except OSError:
            return {}

    def reload_config(self):
        self._load_config()
        self.report_cache.clear()

    def show_page(self, key):
        if self.current:
            self.pages[self.current].pack_forget()
            old = self.nav_buttons[self.current]
            old.configure(fg_color="transparent", text_color=C["muted"], image=icon_image(NAV_ICONS[self.current], C["muted"]))
        self.current = key
        self.pages[key].pack(fill="both", expand=True)
        self.nav_buttons[key].configure(fg_color=C["accent_soft"], text_color=C["text"],
                                        image=icon_image(NAV_ICONS[key], C["accent_hover"]))
        self.pages[key].on_show()

    def set_status(self, text, level="ok"):
        color = {"ok": C["success"], "busy": C["warning"], "error": C["danger"]}[level]
        self.dot.configure(text_color=color)
        self.status_lbl.configure(text=text)

    def set_offline(self, stale_at):
        """Show/hide the 'offline, showing saved data' notice in the sidebar."""
        if stale_at:
            self.offline_lbl.configure(text=f"Offline. Showing data saved {core.fmt_time(stale_at)}.")
            self.offline_lbl.pack(anchor="w", pady=(0, 6), before=self.account_lbl)
        else:
            self.offline_lbl.pack_forget()

    def toast(self, text, kind="info", ms=4200, action=None):
        color = {"info": C["accent"], "success": C["success"], "error": C["danger"], "warn": C["warning"]}[kind]
        if self._toast is not None:
            try:
                self._toast.destroy()
            except tk.TclError:
                pass
        t = ctk.CTkFrame(self, fg_color=C["card2"], corner_radius=14, border_width=1, border_color=color)
        label(t, text, 13, wraplength=380, justify="left").pack(side="left", padx=16, pady=12)
        if action:
            def run_action():
                t.destroy()
                action[1]()
            button(t, action[0], run_action, "primary", width=80).pack(side="left", padx=(0, 12), pady=10)
        t.place(relx=1.0, rely=1.0, x=-24, y=-24, anchor="se")
        t.lift()
        self._toast = t
        self.after(ms, lambda: t.destroy() if t.winfo_exists() else None)

    def confirm(self, title, message, ok_text="Confirm", danger=False, type_to_confirm=None) -> bool:
        m = Modal(self, title)
        label(m.body, title, 18, "bold").pack(anchor="w")
        label(m.body, message, 13, color=C["muted"], wraplength=410, justify="left").pack(anchor="w", pady=(8, 14))
        box = None
        if type_to_confirm:
            box = entry(m.body, width=410, placeholder=f"Type {type_to_confirm} to confirm")
            box.pack(fill="x", pady=(0, 14))
        row = ctk.CTkFrame(m.body, fg_color="transparent")
        row.pack(fill="x")

        def ok():
            if box is not None and box.get().strip() != type_to_confirm:
                box.configure(border_color=C["danger"])
                return
            m.close(True)

        button(row, ok_text, ok, "danger" if danger else "primary", width=120).pack(side="right")
        button(row, "Cancel", lambda: m.close(False), "normal", width=100).pack(side="right", padx=8)
        m.show()
        return bool(m.result)

    def run_async(self, work, on_success, on_error=None, exclusive=False):
        def worker():
            try:
                if exclusive:
                    with self.write_lock:
                        result = work()
                else:
                    result = work()
                self.queue.put(("ok", on_success, result))
            except SystemExit as e:
                self.queue.put(("error", on_error, RuntimeError(str(e.code) if e.code else "Stopped")))
            except Exception as e:
                if not core.is_network_error(e) and not isinstance(e, sync.SyncError):  # expected problems aren't bugs
                    log_error(traceback.format_exc())
                self.queue.put(("error", on_error, e))

        threading.Thread(target=worker, daemon=True).start()

    def run_job(self, widgets, status, work, on_success, on_error=None, exclusive=False, silent_errors=False):
        for w in widgets:
            w.configure(state="disabled")
        self.set_status(status, "busy")

        def restore():
            for w in widgets:
                try:
                    w.configure(state="normal")
                except tk.TclError:
                    pass

        def ok(result):
            restore()
            self.set_status("Ready")
            on_success(result)

        def fail(err):
            restore()
            if on_error:
                on_error(err)
            if silent_errors:  # background refresh: stay quiet
                self.set_status("Ready")
                return
            self.set_status("Something went wrong", "error")
            self.toast(friendly_error(err), "error", 7000)

        self.run_async(work, ok, fail, exclusive)

    def _poll(self):
        try:
            while True:
                kind, cb, payload = self.queue.get_nowait()
                if cb:
                    try:
                        cb(payload)
                    except tk.TclError:
                        pass  # the widget was replaced while the job ran (e.g. accent color change)
                    except Exception:
                        log_error(traceback.format_exc())
                        self.toast("Unexpected error - details saved to app.log", "error")
                elif kind == "error":
                    self.toast(friendly_error(payload), "error", 7000)
        except queue.Empty:
            pass
        self._polls += 1
        if self._polls % 60 == 0:
            gc.collect()
        self.after(150, self._poll)

    @staticmethod
    def touch_target(widget):
        """The canvas a trackpad scroll over `widget` should move, or None if Tk already handles it."""
        w = widget
        while isinstance(w, tk.Misc):
            if isinstance(w, (ttk.Treeview, tk.Text, tk.Listbox)):
                return None
            if isinstance(w, ctk.CTkScrollableFrame):
                return w._parent_canvas
            if isinstance(w, WeekGrid):
                return w.canvas
            w = w.master
        return None

    def _on_touchpad(self, event):
        canvas = self.touch_target(event.widget)
        if canvas is None:
            return
        try:
            _dx, dy = (int(v) for v in self.tk.splitlist(self.tk.call("tk::PreciseScrollDeltas", event.delta)))
        except (tk.TclError, ValueError, TypeError):
            return
        if dy:
            SCROLLER.add(canvas, -dy * 1.33 * self.scale)

    def report_callback_exception(self, exc, val, tb):
        log_error("".join(traceback.format_exception(exc, val, tb)))
        self.toast(f"Unexpected error: {val}", "error")

    def invalidate(self):
        self.report_cache.clear()

    def quick_add(self, day: date | None = None, start: datetime | None = None, end: datetime | None = None):
        if not self.config_data:
            self.toast("Fix config.json first (Settings).", "error")
            return
        QuickAddDialog(self, day, start, end).run()

    def search(self):
        if not self.config_data:
            self.toast("Fix config.json first (Settings).", "error")
            return
        SearchDialog(self).run()

    def palette(self):
        CommandPalette(self).run()

    def open_week(self, d: date):
        """Show the Week page for the week containing `d`."""
        self.pages["week"].week_start = d - timedelta(days=d.weekday())
        self.show_page("week")

    def edit_jobs(self):
        if not self.config_data:
            self.toast("Fix config.json first (Settings).", "error")
            return
        JobsDialog(self, self._jobs_saved).run()

    def _jobs_saved(self):
        self.invalidate()
        for key in {self.current, "dashboard"}:
            self.pages[key].refresh()

    def _startup(self):
        try:
            core.auto_backup()
        except OSError as e:
            log_error(f"auto backup failed: {e}")
        if self.config_data:
            self.pages["dashboard"].refresh(check_email=bool(self.prefs.get("auto_check_email", True)))
            self.sync_now()
        self._after_update_notice()
        self.after(8000, self.maybe_check_updates)

    def _on_close(self):
        self.destroy()


class QuickAddDialog:
    PRESETS = ["Staples", "CIS 111", "CIS 110", "Appointment", "Study", "Task"]

    def __init__(self, app: App, day: date | None, start: datetime | None = None, end: datetime | None = None):
        self.app = app
        self.day = day or date.today()
        self.start_default = core.fmt_time(start) if start else "4:00 PM"
        self.end_default = core.fmt_time(end) if end else "8:00 PM"

    def run(self):
        app = self.app
        m = Modal(app, "Add event", width=500)
        label(m.body, "Add to calendar", 18, "bold").pack(anchor="w")
        label(m.body, "Creates the event on your Google Calendar right away.", 12, color=C["muted"]).pack(
            anchor="w", pady=(2, 12))

        title = entry(m.body, width=450, placeholder="Title (e.g. Staples)")
        title.pack(fill="x")
        presets = ctk.CTkFrame(m.body, fg_color="transparent")
        presets.pack(fill="x", pady=(8, 12))
        for p in self.PRESETS:
            b = button(presets, p, lambda t=p: (title.delete(0, "end"), title.insert(0, t)), "normal", width=10)
            b.configure(height=28, font=font(12))
            b.pack(side="left", padx=(0, 6))

        grid = ctk.CTkFrame(m.body, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure((0, 1, 2), weight=1, uniform="q")
        fields = {}
        for i, (key, text, default) in enumerate([("date", "Date", core.fmt_date(self.day)),
                                                   ("start", "Start", self.start_default), ("end", "End", self.end_default)]):
            label(grid, text, 12, color=C["muted"]).grid(row=0, column=i, sticky="w", padx=(0, 8))
            e = entry(grid, width=140)
            e.insert(0, default)
            e.grid(row=1, column=i, sticky="ew", padx=(0, 8), pady=(2, 0))
            fields[key] = e

        note = label(m.body, "", 12, color=C["warning"], wraplength=450, justify="left", anchor="w")
        note.pack(anchor="w", pady=(10, 0))
        reminders = core.reminder_list(app.config_data)
        remind = switch(m.body, text=f"Remind me {core.describe_reminders(reminders)} before",
                               progress_color=C["accent"], font=font(13))
        remind.select()
        remind.pack(anchor="w", pady=(10, 14))

        def parse():
            d = core.parse_date_flexible(fields["date"].get())
            st = datetime.combine(d, core.parse_clock(fields["start"].get()))
            en = datetime.combine(d, core.parse_clock(fields["end"].get()))
            if en <= st:
                en += timedelta(days=1)
            return st, en

        def check_overlap(*_):
            try:
                st, en = parse()
            except ValueError:
                note.configure(text="")
                return
            rep = app.report_cache.get(st.date() - timedelta(days=st.date().weekday()))
            hits = []
            if rep:
                for ev in rep["days"].get(st.date(), []):
                    if not ev["all_day"] and ev["start"] < en and ev["end"] > st:
                        hits.append(f"{ev['summary']} {core.fmt_time(ev['start'])}-{core.fmt_time(ev['end'])}")
            note.configure(text=("Overlaps: " + ", ".join(hits)) if hits else "")

        for e in fields.values():
            e.bind("<KeyRelease>", check_overlap)
        check_overlap()

        row = ctk.CTkFrame(m.body, fg_color="transparent")
        row.pack(fill="x")

        def submit():
            name = title.get().strip()
            if not name:
                title.configure(border_color=C["danger"])
                return
            try:
                st, en = parse()
            except ValueError as err:
                note.configure(text=str(err), text_color=C["danger"])
                return
            reminder = reminders if remind.get() else None
            m.close(True)

            def work():
                _, cal = core.build_services()
                ev = core.create_manual_event(cal, app.config_data, name, st, en, "Added from Schedule Manager",
                                              reminder)
                app.store.log("quick_add", title=name, event_ids=[ev["id"]])
                app.store.save()
                return ev

            def done(_ev):
                app.invalidate()
                app.toast(f"Added {name} on {core.fmt_date(st.date())}", "success")
                for key in ("week", "dashboard"):
                    if app.current == key:
                        app.pages[key].refresh()

            app.run_job([], "Adding event...", work, done, exclusive=True)

        button(row, "Add event", submit, "primary", width=130).pack(side="right")
        button(row, "Cancel", lambda: m.close(False), "normal", width=100).pack(side="right", padx=8)
        m.show()


class SearchDialog:
    """Ctrl+F: search every event on the calendar, then jump to its week."""

    def __init__(self, app: App):
        self.app = app
        self.results = []
        self._job = None
        self._query = ""

    def run(self):
        app = self.app
        m = self.m = Modal(app, "Find events", width=780)
        m.dialog = self
        label(m.body, "Find events", 18, "bold").pack(anchor="w")
        label(m.body, "Searches titles, notes and locations from 4 months ago to a year ahead.", 12,
              color=C["muted"]).pack(anchor="w", pady=(2, 10))
        self.entry = entry(m.body, width=700, placeholder="Search your calendar...")
        self.entry.pack(fill="x")
        self.frame, self.tree = make_tree(m.body, [("when", "When", 190, "w"), ("title", "Event", 330, "w"),
                                                   ("cat", "Category", 110, "w")], height=9)
        self.frame.pack(fill="both", expand=True, pady=(10, 4))
        self.status = label(m.body, "Type to search.", 12, color=C["muted"], anchor="w")
        self.status.pack(anchor="w")
        row = ctk.CTkFrame(m.body, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        button(row, "Open week", self.open_selected, "primary", width=130).pack(side="right")
        button(row, "Close", lambda: m.close(None), "normal", width=100).pack(side="right", padx=8)
        self.entry.bind("<KeyRelease>", self._typed)
        self.entry.bind("<Return>", lambda _e: self.search_now())
        self.entry.bind("<Down>", lambda _e: self._move(1))
        self.entry.bind("<Up>", lambda _e: self._move(-1))
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())
        self.tree.bind("<Return>", lambda _e: self.open_selected())
        m.after(120, self.entry.focus_set)
        m.show()
        if m.result:
            app.open_week(m.result["day"])

    def _typed(self, event=None):
        if event is not None and event.keysym in ("Return", "Up", "Down", "Escape"):
            return
        if self._job:
            self.m.after_cancel(self._job)
        self._job = self.m.after(450, self.search_now)

    def _move(self, step):
        rows = self.tree.get_children()
        if not rows:
            return
        cur = self.tree.selection()
        i = (rows.index(cur[0]) + step) if cur else 0
        self.tree.selection_set(rows[max(0, min(i, len(rows) - 1))])
        self.tree.see(self.tree.selection()[0])

    def search_now(self):
        text = self.entry.get().strip()
        self._query = text
        if not text:
            self._fill([])
            self.status.configure(text="Type to search.")
            return
        self.status.configure(text="Searching...")
        app, cfg = self.app, self.app.config_data

        def work():
            _, cal = core.build_services()
            return core.search_events(cal, cfg, text)

        def done(results):
            if self.m.winfo_exists() and text == self._query:
                self._fill(results)
                self.status.configure(text=f"{len(results)} result(s)" if results else "No matches.")

        def failed(err):
            if self.m.winfo_exists():
                self.status.configure(text=friendly_error(err))

        app.run_job([], "Searching...", work, done, failed)

    def _fill(self, results):
        self.results = results
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(results):
            when = f"{core.DAY_ABBR[e['day'].weekday()]} {core.fmt_date(e['day'])}  " + (
                "all day" if e["all_day"] else core.fmt_time(e["start"]))
            self.tree.insert("", "end", iid=str(i), values=(when, e["summary"], e["category"]))
        if results:
            today = date.today()
            upcoming = next((i for i, e in enumerate(results) if e["day"] >= today), 0)
            self.tree.selection_set(str(upcoming))
            self.tree.see(str(upcoming))

    def open_selected(self):
        sel = self.tree.selection()
        if sel:
            self.m.close(self.results[int(sel[0])])


class CommandPalette:
    """Ctrl+K: type part of what you want to do, press Enter."""

    def __init__(self, app: App):
        self.app = app
        app_ = app
        self.commands = [(f"Go to {title}", lambda k=key: app_.show_page(k)) for key, title in app.NAV]
        self.commands += [
            ("Add event", app.quick_add),
            ("Find events", app.search),
            ("Edit jobs & pay", app.edit_jobs),
            ("Check for a new schedule", lambda: (app_.show_page("dashboard"),
                                                  app_.pages["dashboard"].refresh(check_email=True))),
            ("Refresh this page", lambda: app_.pages[app_.current].refresh()),
            ("Back up my data now", lambda: app_.pages["settings"].backup()),
            ("Open app folder", lambda: os.startfile(BASE)),
        ]
        self.shown = list(self.commands)

    def filtered(self, query: str):
        words = query.lower().split()
        return [c for c in self.commands if all(w in c[0].lower() for w in words)]

    def run(self):
        m = self.m = Modal(self.app, "Command palette", width=560)
        m.dialog = self
        self.entry = entry(m.body, width=500, placeholder="What do you want to do?")
        self.entry.pack(fill="x")
        self.frame, self.tree = make_tree(m.body, [("name", "", 500, "w")], height=9)
        self.tree.configure(show="tree")
        self.tree.column("#0", width=0, stretch=False)
        self.frame.pack(fill="both", expand=True, pady=(10, 0))
        self._fill("")
        self.entry.bind("<KeyRelease>", lambda e: self._fill(self.entry.get()) if e.keysym not in
                        ("Up", "Down", "Return", "Escape") else None)
        self.entry.bind("<Down>", lambda _e: self._move(1))
        self.entry.bind("<Up>", lambda _e: self._move(-1))
        self.entry.bind("<Return>", lambda _e: self.run_selected())
        self.tree.bind("<Double-1>", lambda _e: self.run_selected())
        m.after(120, self.entry.focus_set)
        m.show()
        if m.result:
            m.result[1]()

    def _fill(self, query):
        self.shown = self.filtered(query)
        self.tree.delete(*self.tree.get_children())
        for i, (name, _fn) in enumerate(self.shown):
            self.tree.insert("", "end", iid=str(i), values=(name,))
        if self.shown:
            self.tree.selection_set("0")

    def _move(self, step):
        rows = self.tree.get_children()
        if rows:
            cur = self.tree.selection()
            i = (rows.index(cur[0]) + step) if cur else 0
            self.tree.selection_set(rows[max(0, min(i, len(rows) - 1))])

    def run_selected(self):
        sel = self.tree.selection()
        if sel:
            self.m.close(self.shown[int(sel[0])])


class JobsDialog:
    """Quick editor for each job's hourly pay and the words that identify its calendar events."""

    def __init__(self, app: App, on_saved=None):
        self.app = app
        self.on_saved = on_saved

    def run(self):
        app, cfg = self.app, self.app.config_data
        m = Modal(app, "Jobs & pay", width=620)
        label(m.body, "Jobs & pay", 18, "bold").pack(anchor="w")
        label(m.body, "Set what each job pays per hour. A calendar event counts as a job when its title contains "
                      "one of the words below (separate several with commas, capitals don't matter).",
              12, color=C["muted"], wraplength=570, justify="left").pack(anchor="w", pady=(4, 14))
        grid = ctk.CTkFrame(m.body, fg_color="transparent")
        grid.pack(fill="x")
        for col, t in enumerate(["Job", "$ per hour", "Title contains"]):
            label(grid, t, 12, "bold", C["muted"]).grid(row=0, column=col, sticky="w", padx=(0, 10))
        rows = []

        def add_row(name="", wage="", match=""):
            r = len(rows) + 1
            n = entry(grid, width=130, placeholder="Job name")
            n.insert(0, name)
            if name:
                n.configure(state="disabled")
            w = entry(grid, width=100, placeholder="0.00")
            w.insert(0, wage)
            mt = entry(grid, width=250, placeholder="words in the title")
            mt.insert(0, match)
            for col, wdg in enumerate((n, w, mt)):
                wdg.grid(row=r, column=col, sticky="w", padx=(0, 10), pady=4)
            rows.append((n, w, mt))
            m.fit()

        wages = cfg.get("job_wages") or {}
        for name, spec in (cfg.get("job_match") or {}).items():
            add_row(name, f"{wages.get(name, 0):g}", spec)

        tax_row = ctk.CTkFrame(m.body, fg_color="transparent")
        tax_row.pack(fill="x", pady=(10, 0))
        label(tax_row, "Estimated tax withheld (%)", 13, color=C["muted"]).pack(side="left")
        tax = entry(tax_row, width=90, placeholder="0")
        tax.insert(0, f"{float(cfg.get('tax_rate_percent') or 0):g}")
        tax.pack(side="left", padx=10)
        label(tax_row, "Shows a take-home estimate. 0 hides it.", 11, color=C["muted"]).pack(side="left")
        note = label(m.body, "", 12, color=C["danger"], anchor="w")
        note.pack(anchor="w", pady=(6, 0))
        foot = ctk.CTkFrame(m.body, fg_color="transparent")
        foot.pack(fill="x", pady=(8, 0))

        def save():
            job_match, job_wages = {}, {}
            try:
                for n, w, mt in rows:
                    name = n.get().strip()
                    if not name:
                        continue
                    job_wages[name] = float(w.get().strip() or 0)
                    job_match[name] = mt.get().strip() or name.lower()
                tax_value = SettingsPage._tax(tax.get().strip())
            except ValueError:
                note.configure(text="Pay must be a number like 17.50, and tax a number from 0 to 90.")
                return
            raw = app.load_raw_config()
            if raw is None:
                return
            raw["job_match"], raw["job_wages"], raw["tax_rate_percent"] = job_match, job_wages, tax_value
            core.save_config(raw)
            app.reload_config()
            m.close(True)
            app.toast("Jobs & pay saved.", "success")
            if self.on_saved:
                self.on_saved()

        button(foot, "+ Add job", lambda: add_row(), "ghost", width=100).pack(side="left")
        button(foot, "Save", save, "primary", width=110).pack(side="right")
        button(foot, "Cancel", lambda: m.close(False), "normal", width=100).pack(side="right", padx=8)
        m.show()


class Page(ctk.CTkFrame):
    def __init__(self, master, app: App):
        super().__init__(master, fg_color="transparent")
        self.app = app

    def build_header(self, title, subtitle=""):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", pady=(0, 20))
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        self.title_lbl = label(left, title, 28, "bold")
        self.title_lbl.pack(anchor="w")
        self.sub_lbl = label(left, subtitle, 13, color=C["muted"])
        self.sub_lbl.pack(anchor="w")
        actions = ctk.CTkFrame(head, fg_color="transparent", height=0)  # an empty CTkFrame is 200px tall by default
        actions.pack(side="right")
        return actions

    def on_show(self):
        pass

    def refresh(self):
        pass

    def need_config(self) -> bool:
        if not self.app.config_data:
            self.app.toast("Fix config.json first (Settings).", "error")
            return False
        return True


def stat_card(parent, title):
    c = card(parent)
    c.title_lbl = label(c, title.upper(), 10, "bold", C["dim"])
    c.title_lbl.pack(anchor="w", padx=18, pady=(16, 0))
    value = label(c, "-", 24, "bold")
    value.pack(anchor="w", padx=18, pady=(2, 16))
    return c, value


class DashboardPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        actions = self.build_header("Dashboard", "")
        button(actions, "Refresh", self.refresh, "normal", width=90).pack(side="left", padx=4)
        button(actions, "Find", app.search, "normal", width=70).pack(side="left", padx=4)
        button(actions, "Jobs & pay", app.edit_jobs, "normal", width=110).pack(side="left", padx=4)
        button(actions, "+ Add event", app.quick_add, "primary", width=120).pack(side="left", padx=4)

        self.banner_slot = ctk.CTkFrame(self, fg_color="transparent", height=0)
        self.banner_slot.pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure((0, 1, 2), weight=1, uniform="d")
        body.grid_rowconfigure(2, weight=1)

        hero = card(body)
        hero.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=(0, 10), pady=(0, 10))
        hero.grid_columnconfigure(0, weight=3, uniform="h")
        hero.grid_columnconfigure(1, weight=2, uniform="h")
        hero.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(hero, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(26, 10), pady=22)
        label(left, "NEXT UP", 11, "bold", C["accent_hover"]).pack(anchor="w")
        self.hero_title = label(left, "Loading...", 34, "bold")
        self.hero_title.pack(anchor="w", pady=(6, 0))
        self.hero_time = label(left, "", 15, color=C["muted"])
        self.hero_time.pack(anchor="w", pady=(2, 0))
        self.hero_count = label(left, "", 16, "bold", C["accent"])
        self.hero_count.pack(anchor="w", pady=(14, 0))
        right = ctk.CTkFrame(hero, fg_color=C["card2"], corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=18)
        label(right, "AFTER THAT", 10, "bold", C["dim"]).pack(anchor="w", padx=18, pady=(16, 6))
        self.hero_then = label(right, "", 13, color=C["muted"], justify="left", anchor="w")
        self.hero_then.pack(anchor="w", padx=18, pady=(0, 16))

        stats = ctk.CTkFrame(body, fg_color="transparent")
        stats.grid(row=0, column=2, sticky="nsew", padx=(10, 0), pady=(0, 10))
        stats.grid_columnconfigure((0, 1), weight=1, uniform="s")
        self.range = "This week"
        self.data = None
        self.seg = segmented(stats, values=["This week", "Next week"], command=self._set_range,
                                          selected_color=C["accent"], selected_hover_color=C["accent_hover"],
                                          unselected_color=C["card2"], fg_color=C["card2"], font=font(12, "bold"))
        self.seg.set("This week")
        self.seg.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        self.stat = {}
        c, self.stat["hours"] = stat_card(stats, "Hours")
        c.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.hours_note = label(c, "", 11, color=C["muted"], anchor="w", wraplength=130, justify="left")
        self.hours_note.pack(anchor="w", padx=16, pady=(0, 10))
        c, self.stat["pay"] = stat_card(stats, "Est. pay")
        c.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self.pay_break = label(c, "", 11, color=C["muted"], justify="left", anchor="w", wraplength=150)
        self.pay_break.pack(anchor="w", padx=16, pady=(0, 10))
        self.pay_fix = button(c, "", app.edit_jobs, "ghost")
        self.pay_fix.configure(height=26, font=font(11), text_color=C["warning"], anchor="w")
        c, self.stat["conflicts"] = stat_card(stats, "Conflicts")
        c.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        c, self.stat["off"] = stat_card(stats, "Days off")
        c.grid(row=2, column=1, sticky="nsew", padx=4, pady=4)

        self.timeline = DayTimeline(body, dict(C), cat_color, app.scale)
        self.timeline.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        self.today_box = self._panel(body, "Today", 0)
        self.heads_box = self._panel(body, "Heads up (next 2 weeks)", 1)
        self.upcoming_box = self._panel(body, "Coming up", 2)

        self.future = None
        self.last_refresh = 0.0
        self.last_email_check = 0.0
        self._loading = False
        self._timer = self.after(1000, self._tick)  # the timer chain starts here and in _tick only
        self._update_header()

    def _panel(self, parent, title, col):
        c = card(parent)
        c.grid(row=2, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0 if col == 2 else 10), pady=(0, 0))
        label(c, title, 15, "bold").pack(anchor="w", padx=20, pady=(18, 6))
        box = scroll_frame(c, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        return box

    def _update_header(self):
        now = datetime.now()
        greet = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
        name = (self.app.config_data or {}).get("display_name", "")
        self.title_lbl.configure(text=f"{greet}{', ' + name if name else ''}")
        self.sub_lbl.configure(text=f"{now:%A, %B} {now.day}")

    def on_show(self):
        self._update_header()
        if self.app.config_data and time.time() - self.last_refresh > 120 and not self._loading:
            self.refresh(quiet=True)  # back on the dashboard after a while

    def _maybe_auto_refresh(self):
        """Keeps the dashboard current when the app is left open: data every 10 min, new schedule email every 30."""
        app = self.app
        if not app.config_data or self._loading or app.write_lock.locked():
            return
        now = time.time()
        if now - self.last_refresh > 600:
            check = bool(app.prefs.get("auto_check_email", True)) and now - self.last_email_check > 1800
            self.refresh(check_email=check, quiet=True)

    def refresh(self, check_email=False, quiet=False):
        if not self.need_config():
            return
        app = self.app
        cfg = app.config_data
        self._loading = True

        def work():
            gmail, cal = core.build_services()
            ws, we = core.this_week_bounds()
            nws, nwe = core.next_week_bounds()
            this = core.generate_weekly_report(cal, cfg, ws, we)
            nxt = core.generate_weekly_report(cal, cfg, nws, nwe)
            found = None
            if check_email:
                try:
                    found = core.find_schedule_emails(gmail, cfg, 8)
                except Exception as e:
                    log_error(f"email check failed: {e}")
            account = None
            try:
                account = gmail.users().getProfile(userId="me").execute().get("emailAddress")
            except Exception:
                pass
            return {"ws": ws, "nws": nws, "this": this, "next": nxt, "emails": found,
                    "account": account if isinstance(account, str) else None}

        def done(data):
            self._loading = False
            self.last_refresh = time.time()
            if check_email:
                self.last_email_check = self.last_refresh
            app.report_cache[data["ws"]] = data["this"]
            app.report_cache[data["nws"]] = data["next"]
            self._render(data)

        def failed(_err):
            self._loading = False
            self.last_refresh = time.time() - 480  # retry in about 2 minutes

        app.run_job([], "Loading dashboard...", work, done, failed, silent_errors=quiet)

    def _render(self, data):
        app = self.app
        this, nxt = data["this"], data["next"]
        now = datetime.now()
        self._update_header()

        self.data = data
        app.set_offline(this.get("stale_at") or nxt.get("stale_at"))
        self._render_stats()
        if data.get("account"):
            app.account_lbl.configure(text=data["account"])

        events = this["events"] + nxt["events"]
        self.future = sorted([e for e in events if not e["all_day"] and e["end"] >= now], key=lambda e: e["start"])
        self.timeline.set_events(events, now.date())
        self._update_hero()
        future = self.future

        for box in (self.today_box, self.heads_box, self.upcoming_box):
            for w in box.winfo_children():
                w.destroy()

        today_events = sorted(this["days"].get(now.date(), []), key=lambda e: (not e["all_day"], e["start"] or now))
        if today_events:
            for e in today_events:
                event_row(self.today_box, e, muted=(not e["all_day"] and e["end"] < now)).pack(fill="x", pady=4, padx=6)
        else:
            label(self.today_box, "Nothing scheduled today.", 13, color=C["muted"]).pack(anchor="w", padx=8, pady=8)

        lines = []
        for rep in (this, nxt):
            for d in sorted(rep["conflicts"]):
                if d >= now.date():
                    lines += [core.describe_conflict(d, c) for c in rep["conflicts"][d]]
        if lines:
            for text in lines[:12]:
                color = C["danger"] if text.startswith("OVERLAP") else C["warning"]
                label(self.heads_box, text, 12, color=color, wraplength=200, justify="left", anchor="w").pack(
                    anchor="w", padx=8, pady=5)
        else:
            label(self.heads_box, "All clear. No conflicts or tight turnarounds.", 13, color=C["success"],
                  wraplength=200, justify="left").pack(anchor="w", padx=8, pady=8)

        for e in future[:14]:
            event_row(self.upcoming_box, e, with_day=True).pack(fill="x", pady=4, padx=6)
        if not future:
            label(self.upcoming_box, "Nothing coming up.", 13, color=C["muted"]).pack(anchor="w", padx=8, pady=8)

        self._banner(data.get("emails"))

    def _set_range(self, value):
        self.range = value
        self._render_stats()

    def _render_stats(self):
        if not self.data:
            return
        cfg = self.app.config_data
        rep = self.data["next"] if self.range == "Next week" else self.data["this"]
        hours, pay = core.paid_totals(rep["category_hours"], cfg)
        wages, jobs = cfg.get("job_wages") or {}, list(cfg.get("job_match", {}).keys())
        self.stat["hours"].configure(text=f"{hours:.1f}h")
        goal = float(cfg.get("weekly_hours_goal") or 0)
        if goal > 0:
            over = hours - goal
            self.hours_note.configure(text=f"{over:.1f}h over your {goal:g}h goal" if over > 0 else f"Goal {goal:g}h",
                                      text_color=C["warning"] if over > 0 else C["muted"])
        else:
            self.hours_note.configure(text="")
        self.stat["pay"].configure(text=f"${pay:,.2f}")
        lines = [f"{j}  {rep['category_hours'][j]:.1f}h  ${rep['category_hours'][j] * wages.get(j, 0):,.2f}"
                 for j in jobs if rep["category_hours"].get(j)]
        if pay > 0 and float(cfg.get("tax_rate_percent") or 0) > 0:
            lines.append(f"Take-home ~ ${core.take_home(pay, cfg):,.2f}")
        upcoming = core.next_payday(cfg.get("pay_schedule") or {})
        if upcoming:
            lines.append(f"Payday {core.DAY_ABBR[upcoming[0].weekday()]} {core.fmt_date_short(upcoming[0])}")
        self.pay_break.configure(text="\n".join(lines) if lines else "No paid shifts")

        # long events that matched no job (e.g. a shift titled "Work") are probably missing from pay
        missing = {}
        for e in rep["events"]:
            if e["category"] == "Other" and not e["all_day"]:
                h = (e["end"] - e["start"]).total_seconds() / 3600
                if h >= 3:
                    missing[e["summary"]] = missing.get(e["summary"], 0) + h
        if missing:
            name, h = max(missing.items(), key=lambda kv: kv[1])
            self.pay_fix.configure(text=f"Not counted: {name} {h:.0f}h (fix)")
            self.pay_fix.pack(anchor="w", padx=8, pady=(0, 8))
        else:
            self.pay_fix.pack_forget()

        n_conf = sum(len(v) for v in rep["conflicts"].values())
        self.stat["conflicts"].configure(text=str(n_conf), text_color=C["warning"] if n_conf else C["success"])
        off = sum(1 for d, evs in rep["days"].items() if not any(e["category"] in jobs for e in evs))
        self.stat["off"].configure(text=str(off))

    def _update_hero(self):
        """Refresh the 'Next up' card from the saved event list; moves on to the next event when one ends."""
        if self.future is None:
            return
        now = datetime.now()
        upcoming = [e for e in self.future if e["end"] >= now]
        ev = upcoming[0] if upcoming else None
        if ev:
            self.hero_title.configure(text=ev["summary"])
            self.hero_time.configure(text=f"{core.DAY_ABBR[ev['day'].weekday()]} {core.fmt_date_short(ev['day'])}"
                                          f"  ·  {core.fmt_time(ev['start'])} - {core.fmt_time(ev['end'])}")
            if ev["start"] <= now <= ev["end"]:
                self.hero_count.configure(text="Happening now", text_color=C["success"])
            else:
                self.hero_count.configure(text=humanize_delta(ev["start"] - now).replace("in ", "Starts in "),
                                          text_color=C["accent"])
            self.hero_then.configure(text="\n".join(
                f"{e['summary']}   {core.DAY_ABBR[e['day'].weekday()]} {core.fmt_time(e['start'])}"
                for e in upcoming[1:5]) or "Nothing else scheduled")
        else:
            self.hero_title.configure(text="Nothing coming up")
            self.hero_time.configure(text="")
            self.hero_count.configure(text="")
            self.hero_then.configure(text="")

    def _tick(self):
        if self.data:
            self.timeline.redraw()  # moves the "now" marker
        self._update_hero()
        self._update_header()
        self._maybe_auto_refresh()
        self._timer = self.after(30000, self._tick)

    def destroy(self):
        timer = getattr(self, "_timer", None)
        if timer:
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass
        super().destroy()

    def _banner(self, emails):
        for w in self.banner_slot.winfo_children():
            w.destroy()
        if not emails:
            return
        todo = [(p, core.schedule_status(self.app.store, p)) for p in emails]
        todo = [(p, st) for p, st in todo if st in ("new", "changed")]
        if not todo:
            return
        p, status = todo[0]
        b = card(self.banner_slot, border_width=1, border_color=C["accent"] if status == "new" else C["warning"])
        b.pack(fill="x", pady=(0, 14))
        kind = "New schedule email" if status == "new" else "Updated schedule email"
        label(b, f"{kind}: week of {core.fmt_date(p.week_start)} - {core.fmt_date(p.week_end)}"
                 f"  ({p.total_hours:.2f} hrs, {len(p.shifts)} shifts)", 14, "bold").pack(side="left", padx=18, pady=14)

        def go():
            self.app.pages["import"].set_results(emails, select_new=True)
            self.app.show_page("import")

        button(b, "Review & import", go, "primary", width=150).pack(side="right", padx=14, pady=10)


class WeekPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.week_start = core.this_week_bounds()[0]
        self.report = None
        self.selected = None

        actions = self.build_header("Week", "")
        self.btn_prev = button(actions, "<", lambda: self.shift(-1), "normal", width=40)
        self.btn_prev.pack(side="left", padx=2)
        button(actions, "Today", self.go_today, "normal", width=80).pack(side="left", padx=2)
        self.btn_next = button(actions, ">", lambda: self.shift(1), "normal", width=40)
        self.btn_next.pack(side="left", padx=(2, 12))
        button(actions, "Find", app.search, "normal", width=70).pack(side="left", padx=4)
        self.export_menu = option_menu(actions, ["CSV file", "Calendar file (.ics)"], self._export_choice, width=110)
        self.export_menu.set("Export")
        self.export_menu.pack(side="left", padx=4)
        button(actions, "+ Add event", lambda: app.quick_add(self.week_start), "primary", width=120).pack(
            side="left", padx=4)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.grid_view = WeekGrid(body, dict(C), cat_color, self.on_select, app.scale,
                                  on_move=self.on_move, on_create=self.on_create)
        self.grid_view.scroller = SCROLLER
        self.grid_view.pack(side="left", fill="both", expand=True, padx=(0, 12))

        side = scroll_frame(body, width=290, fg_color="transparent")
        side.pack(side="right", fill="y")
        self.detail = card(side)
        self.detail.pack(fill="x", pady=(0, 10))
        self.totals = card(side)
        self.totals.pack(fill="x", pady=(0, 10))
        self.alerts = card(side)
        self.alerts.pack(fill="x", pady=(0, 10))
        self.free = card(side)
        self.free.pack(fill="x")
        self._render_detail()

    def on_show(self):
        self.load()

    def refresh(self):
        self.load(force=True)

    def shift(self, weeks):
        self.week_start += timedelta(weeks=weeks)
        self.load()

    def go_today(self):
        self.week_start = core.this_week_bounds()[0]
        self.load()

    def load(self, force=False):
        if not self.need_config():
            return
        ws = self.week_start
        we = ws + timedelta(days=6)
        self.sub_lbl.configure(text=f"{core.fmt_date(ws)} - {core.fmt_date(we)}")
        if not force and ws in self.app.report_cache:
            self.render(self.app.report_cache[ws])
            return
        cfg = self.app.config_data

        def work():
            _, cal = core.build_services()
            return core.generate_weekly_report(cal, cfg, ws, we)

        def done(rep):
            self.app.report_cache[ws] = rep
            if ws == self.week_start:
                self.render(rep)

        self.app.run_job([self.btn_prev, self.btn_next], "Loading week...", work, done)

    def render(self, rep):
        self.report = rep
        cfg = self.app.config_data
        self.app.set_offline(rep.get("stale_at"))
        self.selected = None
        self.grid_view.set_data(self.week_start, rep["days"], rep["conflicts"])
        self._render_detail()

        for w in self.totals.winfo_children():
            w.destroy()
        head = ctk.CTkFrame(self.totals, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 4))
        label(head, "This week", 14, "bold").pack(side="left")
        button(head, "Edit pay", self.app.edit_jobs, "ghost", width=70).pack(side="right")
        wages = cfg.get("job_wages") or {}
        for cat, hrs in rep["category_hours"].items():
            row = ctk.CTkFrame(self.totals, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=2)
            ctk.CTkFrame(row, width=10, height=10, corner_radius=5, fg_color=cat_color(cat)).pack(side="left")
            label(row, f"  {cat}", 13).pack(side="left")
            pay = hrs * wages.get(cat, 0)
            label(row, f"{hrs:.2f}h" + (f"  ${pay:,.2f}" if pay else ""), 13, color=C["muted"]).pack(side="right")
        hours, pay = core.paid_totals(rep["category_hours"], cfg)
        row = ctk.CTkFrame(self.totals, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 14))
        label(row, "Total paid", 13, "bold").pack(side="left")
        label(row, f"{hours:.2f}h  ${pay:,.2f}", 13, "bold", C["accent"]).pack(side="right")

        for w in self.alerts.winfo_children():
            w.destroy()
        label(self.alerts, "Conflicts", 14, "bold").pack(anchor="w", padx=16, pady=(14, 6))
        lines = [(d, c) for d in sorted(rep["conflicts"]) for c in rep["conflicts"][d]]
        if lines:
            for d, c in lines:
                color = C["danger"] if c["type"] == "overlap" else C["warning"]
                label(self.alerts, core.describe_conflict(d, c), 12, color=color, wraplength=250,
                      justify="left", anchor="w").pack(anchor="w", padx=16, pady=3)
            ctk.CTkFrame(self.alerts, height=8, fg_color="transparent").pack()
        else:
            label(self.alerts, "None this week.", 13, color=C["success"]).pack(anchor="w", padx=16, pady=(0, 14))

        for w in self.free.winfo_children():
            w.destroy()
        label(self.free, "Best free block each day", 14, "bold").pack(anchor="w", padx=16, pady=(14, 2))
        label(self.free, "8 AM to 10 PM, at least 1 hour", 11, color=C["muted"]).pack(anchor="w", padx=16, pady=(0, 6))
        shown = 0
        for d, evs in rep["days"].items():
            slots = core.find_free_slots(evs, d, 8, 22, 60)
            if not slots:
                continue
            best = max(slots, key=lambda s: s[1] - s[0])
            hrs = (best[1] - best[0]).total_seconds() / 3600
            row = ctk.CTkFrame(self.free, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=2)
            label(row, f"{core.DAY_ABBR[d.weekday()]} {core.fmt_date_short(d)}", 12, color=C["muted"]).pack(side="left")
            label(row, f"{core.fmt_time(best[0])} - {core.fmt_time(best[1])}  ({hrs:.1f}h)", 12).pack(side="right")
            shown += 1
        if not shown:
            label(self.free, "No free blocks.", 13, color=C["muted"]).pack(anchor="w", padx=16)
        ctk.CTkFrame(self.free, height=10, fg_color="transparent").pack()

    def on_select(self, ev):
        self.selected = ev
        self._render_detail()

    def on_create(self, day, start, end):
        """Dragged on empty space in the calendar: open the add-event dialog with that time filled in."""
        self.app.quick_add(day, start, end)

    def on_move(self, ev, start, end):
        """Dragged an event to a new time/day, or dragged its bottom edge: apply it, with Undo."""
        app = self.app
        tracked = any(ev["id"] in w.get("event_ids", []) for w in app.store.data["weeks"].values())
        if tracked and not app.confirm(
                "Move this shift?", "This shift came from your schedule email. Moving it only changes your Google "
                "Calendar; your schedule email and imported history stay as they were.", "Move"):
            self.load(force=False)  # snap the calendar back to how it was
            return
        tz = app.config_data["timezone"]
        old = (ev["start"], ev["end"])

        def work():
            _, cal = core.build_services()
            return core.move_event(cal, ev["calendar_id"], ev["id"], start, end, tz)

        def done(_r):
            app.invalidate()
            app.toast(f"Moved '{ev['summary']}' to {core.DAY_ABBR[start.weekday()]} {core.fmt_range(start, end)}.",
                      "success", 9000, action=("Undo", lambda: self._undo_move(ev, old, tz)))
            self.load(force=True)

        def failed(_e):
            self.load(force=False)

        app.run_job([], "Moving event...", work, done, failed, exclusive=True)

    def _undo_move(self, ev, old, tz):
        app = self.app

        def work():
            _, cal = core.build_services()
            return core.move_event(cal, ev["calendar_id"], ev["id"], old[0], old[1], tz)

        def done(_r):
            app.invalidate()
            app.toast("Move undone.", "success")
            self.load(force=True)

        app.run_job([], "Undoing move...", work, done, exclusive=True)

    def _render_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()
        ev = self.selected
        if not ev:
            label(self.detail, "Click an event", 14, "bold").pack(anchor="w", padx=16, pady=(14, 2))
            label(self.detail, "Click a block for details. Drag it to move it, drag its bottom edge to resize, or drag on empty space to add an event.", 12,
                  color=C["muted"], wraplength=250, justify="left").pack(anchor="w", padx=16, pady=(0, 14))
            return
        label(self.detail, ev["category"].upper(), 11, "bold", cat_color(ev["category"])).pack(
            anchor="w", padx=16, pady=(14, 0))
        label(self.detail, ev["summary"], 17, "bold", wraplength=250, justify="left", anchor="w").pack(
            anchor="w", padx=16)
        label(self.detail, f"{core.DAY_ABBR[ev['day'].weekday()]} {core.fmt_date(ev['day'])}", 13,
              color=C["muted"]).pack(anchor="w", padx=16, pady=(4, 0))
        if ev["all_day"]:
            label(self.detail, "All day", 13, color=C["muted"]).pack(anchor="w", padx=16)
        else:
            hrs = (ev["end"] - ev["start"]).total_seconds() / 3600
            label(self.detail, f"{core.fmt_time(ev['start'])} - {core.fmt_time(ev['end'])}  ({hrs:.2f}h)", 13,
                  color=C["muted"]).pack(anchor="w", padx=16)
        button(self.detail, "Delete event", self.delete_selected, "danger").pack(fill="x", padx=16, pady=14)

    def delete_selected(self):
        ev = self.selected
        if not ev:
            return
        if not self.app.confirm("Delete this event?",
                                f"'{ev['summary']}' on {core.fmt_date(ev['day'])} will be removed from your Google Calendar. "
                                "You'll have a few seconds to undo it.", "Delete", danger=True):
            return
        app = self.app

        def work():
            _, cal = core.build_services()
            snapshot = core.delete_event_with_snapshot(cal, ev["calendar_id"], ev["id"])
            tracking = core.forget_event(app.store, ev["id"])
            return snapshot, tracking

        def done(result):
            snapshot, tracking = result
            app.invalidate()
            if snapshot:
                app.toast("Event deleted.", "success", 9000,
                          action=("Undo", lambda: self.undo_delete(ev, snapshot, tracking)))
            else:
                app.toast("That event was already gone.", "info")
            self.load(force=True)

        app.run_job([], "Deleting event...", work, done, exclusive=True)

    def undo_delete(self, ev, snapshot, tracking):
        app = self.app

        def work():
            _, cal = core.build_services()
            new = core.restore_event(cal, ev["calendar_id"], snapshot)
            if tracking:
                core.restore_tracking(app.store, tracking, new["id"])
            return new

        def done(_new):
            app.invalidate()
            app.toast(f"Restored '{ev['summary']}'.", "success")
            self.load(force=True)

        app.run_job([], "Restoring event...", work, done, exclusive=True)

    def _export_choice(self, choice):
        self.export_menu.set("Export")
        (self.export if choice.startswith("CSV") else self.export_ics)()

    def export_ics(self):
        if not self.report:
            self.app.toast("Load a week first.", "warn")
            return
        path = filedialog.asksaveasfilename(defaultextension=".ics", initialfile=f"schedule-{self.week_start.isoformat()}.ics",
                                            filetypes=[("Calendar file", "*.ics")])
        if path:
            n = core.export_events_ics(path, self.report["events"], self.app.config_data["timezone"])
            self.app.toast(f"Saved {n} event(s) to {os.path.basename(path)}. Open it to import into any calendar app.", "success", 6000)

    def export(self):
        if not self.report:
            self.app.toast("Load a week first.", "warn")
            return
        default = f"schedule-{self.week_start.isoformat()}.csv"
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=default,
                                            filetypes=[("CSV file", "*.csv")])
        if path:
            core.export_events_csv(path, self.report["events"])
            self.app.toast(f"Saved {os.path.basename(path)}", "success")


class MonthPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.month = date.today().replace(day=1)
        actions = self.build_header("Month", "")
        self.btn_prev = button(actions, "<", lambda: self.shift(-1), "normal", width=40)
        self.btn_prev.pack(side="left", padx=2)
        button(actions, "Today", self.go_today, "normal", width=80).pack(side="left", padx=2)
        self.btn_next = button(actions, ">", lambda: self.shift(1), "normal", width=40)
        self.btn_next.pack(side="left", padx=(2, 12))
        button(actions, "+ Add event", app.quick_add, "primary", width=120).pack(side="left", padx=4)
        self.grid_view = MonthGrid(self, dict(C), cat_color, self.pick, app.scale)
        self.grid_view.pack(fill="both", expand=True)

    def on_show(self):
        self.load()

    def refresh(self):
        self.load()

    def shift(self, months):
        m = self.month.month - 1 + months
        self.month = date(self.month.year + m // 12, m % 12 + 1, 1)
        self.load()

    def go_today(self):
        self.month = date.today().replace(day=1)
        self.load()

    def pick(self, d):
        self.app.open_week(d)

    def load(self):
        if not self.need_config():
            return
        month, cfg = self.month, self.app.config_data
        gs, ge = month_grid_bounds(month)

        def work():
            _, cal = core.build_services()
            events, stale = core.fetch_events_or_cached(cal, cfg, gs, ge)
            days = core.group_events_by_day(events, gs, ge)
            return events, stale, days, core.find_all_conflicts(days, cfg)

        def done(res):
            events, stale, days, conflicts = res
            if month != self.month:
                return  # the user already moved on
            self.app.set_offline(stale)
            self.grid_view.set_data(month, days, conflicts, core.daily_paid_hours(events, cfg))
            in_month = [e for e in events if e["day"].month == month.month and e["day"].year == month.year]
            hours, pay = core.paid_totals(core.compute_category_hours(in_month), cfg)
            self.title_lbl.configure(text=f"{month:%B %Y}")
            self.sub_lbl.configure(text=f"{hours:.1f}h worked  \u00b7  ${pay:,.2f} estimated pay" if hours else "No paid shifts this month")

        self.app.run_job([self.btn_prev, self.btn_next], "Loading month...", work, done)


class PlanPage(Page):
    """Suggests study blocks in the free time around classes and shifts, and adds the ones you pick."""

    def __init__(self, master, app):
        super().__init__(master, app)
        self.plan = []
        actions = self.build_header("Study planner", "Finds free time in your week and suggests study blocks")
        self.seg = segmented(actions, values=["This week", "Next week"], command=lambda _v: self.suggest(),
                                          selected_color=C["accent"], selected_hover_color=C["accent_hover"],
                                          unselected_color=C["card2"], fg_color=C["card2"], font=font(13, "bold"))
        self.seg.set("Next week" if date.today().weekday() >= 5 else "This week")  # little of this week is left on weekends
        self.seg.pack(side="left", padx=4)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1, uniform="p")
        body.grid_columnconfigure(1, weight=2, uniform="p")
        body.grid_rowconfigure(0, weight=1)

        left = card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        label(left, "Plan", 14, "bold").pack(anchor="w", padx=18, pady=(16, 8))
        prefs = app.prefs
        label(left, "Study hours to fit in", 12, color=C["muted"]).pack(anchor="w", padx=18)
        self.goal = entry(left, width=120)
        self.goal.insert(0, f"{prefs.get('plan_goal', 8):g}")
        self.goal.pack(anchor="w", padx=18, pady=(2, 10))
        label(left, "Longest block", 12, color=C["muted"]).pack(anchor="w", padx=18)
        self.longest = option_menu(left, ["1 hour", "2 hours", "3 hours"], width=150)
        self.longest.set(prefs.get("plan_longest", "2 hours"))
        self.longest.pack(anchor="w", padx=18, pady=(2, 10))
        label(left, "Calendar title", 12, color=C["muted"]).pack(anchor="w", padx=18)
        self.title_entry = entry(left, width=200)
        self.title_entry.insert(0, prefs.get("plan_title", "Study"))
        self.title_entry.pack(anchor="w", padx=18, pady=(2, 14))
        self.suggest_btn = button(left, "Suggest blocks", self.suggest, "normal", width=160)
        self.suggest_btn.pack(anchor="w", padx=18)
        self.add_btn = button(left, "Add selected to calendar", self.add_selected, "primary", width=210)
        self.add_btn.pack(anchor="w", padx=18, pady=(10, 0))
        self.note = label(left, "Blocks stay 30 minutes clear of every class, shift and event, between 9 AM and 9 PM.",
                          11, color=C["muted"], wraplength=240, justify="left", anchor="w")
        self.note.pack(anchor="w", padx=18, pady=(14, 0))

        frame, self.tree = make_tree(body, [("day", "Day", 130, "w"), ("time", "Time", 170, "w"), ("len", "Length", 80, "w")],
                                     height=12, selectmode="extended")
        self.tree_frame = frame
        frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.summary = label(self, "", 13, color=C["muted"], anchor="w")
        self.summary.pack(anchor="w", pady=(10, 0))

    def on_show(self):
        if not self.plan and self.app.config_data:
            self.suggest()

    def refresh(self):
        self.suggest()

    def _week(self):
        return core.next_week_bounds() if self.seg.get() == "Next week" else core.this_week_bounds()

    def suggest(self):
        if not self.need_config():
            return
        try:
            goal = float(self.goal.get().strip())
            if not 0 < goal <= 60:
                raise ValueError
        except ValueError:
            self.app.toast("Study hours should be a number between 0 and 60.", "error")
            return
        longest = int(self.longest.get().split()[0]) * 60
        self.app.prefs.update({"plan_goal": goal, "plan_longest": self.longest.get(),
                               "plan_title": self.title_entry.get().strip() or "Study"})
        save_prefs(self.app.prefs)
        ws, we = self._week()
        cfg = self.app.config_data

        def work():
            _, cal = core.build_services()
            rep = core.generate_weekly_report(cal, cfg, ws, we)
            return core.suggest_study_blocks(rep["days"], goal, cfg, max_minutes=longest), goal

        def done(res):
            plan, wanted = res
            self.plan = plan
            self.tree.delete(*self.tree.get_children())
            for i, b in enumerate(plan):
                self.tree.insert("", "end", iid=str(i), values=(
                    f"{core.DAY_ABBR[b['day'].weekday()]} {core.fmt_date_short(b['day'])}",
                    core.fmt_range(b["start"], b["end"]), f"{b['hours']:g} h"))
            self.tree.selection_set(*self.tree.get_children())
            show_empty(self.tree_frame, None if plan else "No free time found this week.\nTry a shorter longest block.")
            total = sum(b["hours"] for b in plan)
            self.summary.configure(text=f"{total:g} of {wanted:g} hours planned"
                                        + ("" if total >= wanted - 0.01 else "  \u00b7  that's all the free time there is"))

        self.app.run_job([self.suggest_btn], "Finding free time...", work, done)

    def add_selected(self):
        picked = [self.plan[int(i)] for i in self.tree.selection()]
        if not picked:
            self.app.toast("Select at least one block.", "warn")
            return
        title = self.title_entry.get().strip() or "Study"
        app, cfg = self.app, self.app.config_data
        reminders = core.reminder_list(cfg)

        def work():
            _, cal = core.build_services()
            ids = []
            for b in picked:
                ev = core.create_manual_event(cal, cfg, title, b["start"], b["end"], "Planned in Schedule Manager", reminders)
                ids.append(ev["id"])
            app.store.log("quick_add", title=f"{title} x{len(ids)}", event_ids=ids)
            app.store.save()
            return len(ids)

        def done(n):
            app.invalidate()
            app.toast(f"Added {n} study block(s) to your calendar.", "success")
            self.suggest()

        app.run_job([self.add_btn, self.suggest_btn], "Adding blocks...", work, done, exclusive=True)


class EarningsPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.rows = []
        self.mode = ("weeks", 8)
        actions = self.build_header("Earnings", "Estimated pay from your calendar, by week")
        self.seg = segmented(actions, values=["4 weeks", "8 weeks", "12 weeks", "6 months", "Pay periods"],
                                          command=self._pick,
                                          selected_color=C["accent"], selected_hover_color=C["accent_hover"],
                                          unselected_color=C["card2"], fg_color=C["card2"], font=font(13, "bold"))
        self.seg.set("8 weeks")
        self.seg.pack(side="left", padx=4)
        button(actions, "Jobs & pay", app.edit_jobs, "normal", width=110).pack(side="left", padx=4)
        button(actions, "Refresh", self.refresh, "normal", width=90).pack(side="left", padx=4)

        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 12))
        cards.grid_columnconfigure((0, 1, 2, 3, 4), weight=1, uniform="e")
        self.vals, self.cards = {}, {}
        for i, (t, k) in enumerate([("Total", "total"), ("Average / week", "avg"), ("Best week", "best"),
                                    ("Avg hours / week", "hrs"), ("After tax", "net")]):
            c, v = stat_card(cards, t)
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 5, 0 if i == 4 else 5))
            self.vals[k], self.cards[k] = v, c

        chart_card = card(self)
        chart_card.pack(fill="both", expand=True)
        self.chart = tk.Canvas(chart_card, bg=C["card"], highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=14, pady=14)
        self.chart.bind("<Configure>", lambda _e: self.draw())
        self.loaded = False

    def _pick(self, value):
        if value == "Pay periods":
            if core.pay_period_bounds(date.today(), self.app.config_data.get("pay_schedule")) is None:
                self.app.toast("Set up your pay schedule under Settings first.", "warn")
                self.seg.set(self._seg_label())
                return
            self.mode = ("periods", 6)
        else:
            n, unit = value.split()
            self.mode = (unit, int(n))
        self.refresh()

    def _seg_label(self):
        unit, n = self.mode
        return "Pay periods" if unit == "periods" else f"{n} {unit}"

    def on_show(self):
        if not self.loaded:
            self.refresh()

    def refresh(self):
        if not self.need_config():
            return
        cfg, (unit, n) = self.app.config_data, self.mode

        def work():
            _, cal = core.build_services()
            fn = {"months": core.compute_monthly_earnings, "periods": core.compute_pay_periods}.get(unit, core.compute_weekly_earnings)
            return fn(cal, cfg, n)

        def done(rows):
            self.loaded = True
            self.rows = rows
            self.app.set_offline(rows[0].get("stale_at") if rows else None)
            self._summary()
            self.draw()

        self.app.run_job([], "Calculating earnings...", work, done)

    def _summary(self):
        rows, cfg = self.rows, self.app.config_data
        per = {"months": "month", "periods": "pay period"}.get(self.mode[0], "week")
        self.sub_lbl.configure(text=f"Estimated pay from your calendar, by {per}")
        self.cards["avg"].title_lbl.configure(text=f"AVERAGE / {per.upper()}")
        self.cards["best"].title_lbl.configure(text=f"BEST {per.upper()}")
        self.cards["hrs"].title_lbl.configure(text=f"AVG HOURS / {per.upper()}")
        total = sum(r["pay"] for r in rows)
        hours = sum(r["hours"] for r in rows)
        best = max(rows, key=lambda r: r["pay"]) if rows else None
        self.vals["total"].configure(text=f"${total:,.0f}")
        self.vals["avg"].configure(text=f"${total / max(len(rows), 1):,.0f}")
        self.vals["best"].configure(text=f"${best['pay']:,.0f}" if best else "-")
        self.vals["hrs"].configure(text=f"{hours / max(len(rows), 1):.1f}h")
        rate = float(cfg.get("tax_rate_percent") or 0)
        self.cards["net"].title_lbl.configure(text=f"AFTER {rate:g}% TAX" if rate > 0 else "AFTER TAX")
        self.vals["net"].configure(text=f"${core.take_home(total, cfg):,.0f}" if rate > 0 else "Not set")

    def draw(self):
        c = self.chart
        c.delete("all")
        rows = self.rows
        w, h = c.winfo_width(), c.winfo_height()
        if not rows or w < 100:
            return
        s = self.app.scale
        left, right, top, bottom = 58 * s, 16 * s, 34 * s, 52 * s
        top_pay = max(max(r["pay"] for r in rows), 1)
        step = 100 if top_pay <= 600 else 250 if top_pay <= 1500 else 500 if top_pay <= 3000 else 1000
        ymax = math.ceil(top_pay / step) * step
        plot_h = h - top - bottom
        f_small, f_bold = ("Segoe UI", round(9 * s)), ("Segoe UI", round(9 * s), "bold")
        for i in range(5):
            v = ymax * i / 4
            y = h - bottom - plot_h * i / 4
            c.create_line(left, y, w - right, y, fill=C["border"])
            c.create_text(left - 8, y, text=f"${v:,.0f}", anchor="e", font=f_small, fill=C["muted"])

        n = len(rows)
        slot = (w - left - right) / n
        bar_w = min(70 * s, slot * 0.58)
        wages = self.app.config_data.get("job_wages") or {}
        jobs = list(self.app.config_data.get("job_match", {}).keys())
        now_text = {"months": "This month", "periods": "This period"}.get(self.mode[0], "This week")
        for i, r in enumerate(rows):
            cx = left + slot * i + slot / 2
            y_cursor = h - bottom
            for job in jobs:
                pay = r["category_hours"].get(job, 0) * wages.get(job, 0)
                if pay <= 0:
                    continue
                seg = plot_h * pay / ymax
                c.create_rectangle(cx - bar_w / 2, y_cursor - seg, cx + bar_w / 2, y_cursor,
                                   fill=cat_color(job), outline=C["card"], width=1)
                y_cursor -= seg
            if r["pay"] > 0:
                c.create_text(cx, y_cursor - 8, text=f"${r['pay']:,.0f}", font=f_bold, fill=C["text"])
            is_now = r["current"]
            c.create_text(cx, h - bottom + 16 * s, text=now_text if is_now else r["label"],
                          font=f_bold if is_now else f_small, fill=C["accent"] if is_now else C["muted"])
            c.create_text(cx, h - bottom + 30 * s, text=f"{r['hours']:.1f}h", font=f_small, fill=C["muted"])
            if r.get("payday"):
                c.create_text(cx, h - bottom + 44 * s, text=f"paid {core.fmt_date_short(r['payday'])}", font=f_small,
                              fill=C["accent"] if r["current"] else C["muted"])

        x = left
        for job in jobs:
            c.create_rectangle(x, 10 * s, x + 10 * s, 20 * s, fill=cat_color(job), outline="")
            c.create_text(x + 16 * s, 15 * s, text=job, anchor="w", font=f_small, fill=C["text"])
            x += 90 * s


class ImportPage(Page):
    STATUS_TEXT = {"new": "New", "changed": "Changed", "imported": "Imported", "older": "Older version"}

    def __init__(self, master, app):
        super().__init__(master, app)
        self.results = []
        self.analysis = {}  # email id -> analyze_import() result, or {"error": ...}
        actions = self.build_header("Import schedule", "Read Domino's schedule emails from Gmail and add them to your calendar")
        self.find_btn = button(actions, "Find schedule emails", self.find, "primary", width=170)
        self.find_btn.pack(side="left")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=5, uniform="i")
        body.grid_columnconfigure(1, weight=6, uniform="i")
        body.grid_rowconfigure(0, weight=1)

        frame, self.tree = make_tree(body, [("week", "Week", 165, "w"), ("hours", "Hours", 55, "w"),
                                            ("status", "Status", 95, "w")])
        self.tree_frame = frame
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        show_empty(frame, "Looking for schedule emails...")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.tag_configure("new", foreground=C["success"])
        self.tree.tag_configure("changed", foreground=C["warning"])
        self.tree.tag_configure("older", foreground=C["muted"])
        make_sortable(self.tree, {"hours"})

        right = card(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        label(right, "Preview", 14, "bold").pack(anchor="w", padx=16, pady=(14, 6))
        self.preview = textbox(right, size=12)
        self.preview.pack(fill="both", expand=True, padx=14)
        self.preview.configure(state="disabled")
        opts = ctk.CTkFrame(right, fg_color="transparent")
        opts.pack(fill="x", padx=16, pady=(10, 0))
        self.remove_stale = switch(opts, text="Remove dropped shifts", progress_color=C["accent"], font=font(12))
        self.remove_stale.select()
        self.remove_stale.grid(row=0, column=0, sticky="w", padx=(0, 14), pady=2)
        self.no_email = switch(opts, text="Skip email", progress_color=C["accent"], font=font(12))
        self.no_email.grid(row=0, column=1, sticky="w", padx=(0, 14), pady=2)
        self.force = switch(opts, text="Force re-add", progress_color=C["accent"], font=font(12))
        self.force.grid(row=1, column=0, sticky="w", pady=2)
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=14)
        self.import_btn = button(row, "Import selected week", self.do_import, "primary", width=190)
        self.import_btn.pack(side="left")
        button(row, "Copy", self.copy, "normal", width=80).pack(side="left", padx=8)

    def on_show(self):
        if not self.results and self.app.config_data:
            self.find()

    def refresh(self):
        self.analysis.clear()
        self.find()

    def find(self):
        if not self.need_config():
            return
        cfg = self.app.config_data

        def work():
            gmail, _ = core.build_services()
            return core.find_schedule_emails(gmail, cfg, 15)

        def done(results):
            self.set_results(results)
            if not results:
                self.app.toast("No schedule emails matched your Gmail search.", "warn")
            elif any(p.from_cache for p in results):
                self.app.toast("Offline: showing saved schedule emails. Reconnect to check for new ones.", "warn", 6000)

        self.app.run_job([self.find_btn], "Searching Gmail...", work, done)

    def set_results(self, results, select_new=False):
        self.results = results
        self.tree.delete(*self.tree.get_children())
        show_empty(self.tree_frame, None if results else "No schedule emails found.\nClick Find schedule emails.")
        first_actionable = first_normal = None
        for i, p in enumerate(results):
            status = core.schedule_status(self.app.store, p)
            if status in ("new", "changed") and first_actionable is None:
                first_actionable = str(i)
            if status != "older" and first_normal is None:
                first_normal = str(i)
            self.tree.insert("", "end", iid=str(i), tags=(status,) if status != "imported" else (), values=(
                f"{core.fmt_date_short(p.week_start)} - {core.fmt_date(p.week_end)}", f"{p.total_hours:.2f}",
                self.STATUS_TEXT[status]))
        target = first_actionable if (select_new and first_actionable is not None) else (
            first_actionable or first_normal or ("0" if results else None))
        if target is not None:
            self.tree.selection_set(target)

    def selected(self):
        sel = self.tree.selection()
        return self.results[int(sel[0])] if sel else None

    def on_select(self, _e):
        p = self.selected()
        if not p:
            return
        self.render(p)
        prior = self.analysis.get(p.email_id)
        if (prior is None or "error" in prior) and self.app.config_data:  # failures are never remembered
            self._analyze(p)

    def _analyze(self, p):
        app = self.app
        cfg = app.config_data
        self.analysis[p.email_id] = {"loading": True}

        def work():
            _, cal = core.build_services()
            return core.analyze_import(cal, cfg, app.store, p)

        def done(a):
            self.analysis[p.email_id] = a
            cur = self.selected()
            if cur and cur.email_id == p.email_id:
                self.render(cur)

        def failed(err):
            self.analysis[p.email_id] = {"error": friendly_error(err)}
            cur = self.selected()
            if cur and cur.email_id == p.email_id:
                self.render(cur)

        app.run_job([], "Checking your calendar...", work, done, failed)

    def render(self, p):
        cfg = self.app.config_data
        a = self.analysis.get(p.email_id) or {}
        ready = "diff" in a
        tracked = set(self.app.store.data["imported_shift_keys"])
        new_keys = {s.key for s in a["diff"]["new"]} if ready else None
        who = "  \u00b7  ".join(x for x in (p.employee_name, f"Store #{p.store_number}" if p.store_number else "") if x)
        lines = [who or "Domino's schedule",
                 f"Week of {core.fmt_date(p.week_start)} to {core.fmt_date(p.week_end)}",
                 f"Email received {p.email_date:%m/%d/%Y}"]
        if p.superseded:
            lines.append("OLDER VERSION: a newer email exists for this week.")
        lines.append("")
        for s in p.shifts:
            if ready:
                tag = "NEW" if s.key in new_keys else "on calendar"
            else:
                tag = "on calendar" if s.key in tracked else ""
            lines.append(f"{s.day_name[:3]} {core.fmt_date_short(s.shift_date):<6}{s.role[:8]:<9}"
                         f"{core.fmt_range(s.start_dt, s.end_dt):<17}{s.hours:>5.2f}h  {tag}".rstrip())
        lines += ["", f"Total: {p.total_hours:.2f}h"]
        wage = (cfg.get("job_wages") or {}).get("Dominos", 0)
        if wage:
            lines.append(f"Est. pay: ${p.total_hours * wage:,.2f}")

        lines += ["", "What will happen"]
        if ready:
            d = a["diff"]
            lines.append(f"  + {len(d['new'])} new shift(s) added to your calendar")
            lines.append(f"  = {len(d['already'])} already on your calendar")
            if d["removed"]:
                lines.append(f"  - {len(d['removed'])} shift(s) no longer in this schedule"
                             + (" (will be removed)" if self.remove_stale.get() else " (kept)"))
            lines += ["", "Heads up" + ("  (offline: based on saved calendar data)" if a.get("stale_at") else "")]
            if a["conflicts"]:
                lines += [f"  ! {core.describe_conflict(day, c)}" for day, c in a["conflicts"]]
            else:
                lines.append("  No conflicts with your other events.")
        elif a.get("error"):
            lines.append(f"  Couldn't check your calendar: {a['error']}")
        else:
            lines.append("  Checking your calendar...")
        set_text(self.preview, "\n".join(lines), [self._line_color(l) for l in lines])

    @staticmethod
    def _line_color(line: str):
        t = line.strip()
        if t.endswith(" NEW") or t.startswith("+ "):
            return C["success"]
        if t.endswith("on calendar") or t.startswith("= "):
            return C["muted"]
        if t in ("What will happen",) or t.startswith("Heads up"):
            return C["accent"]
        if t.startswith("! OVERLAP") or t.startswith("Couldn't check"):
            return C["danger"]
        if t.startswith("! ") or t.startswith("- ") or t.startswith("OLDER VERSION"):
            return C["warning"]
        return None

    def copy(self):
        text = self.preview.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.toast("Copied.", "success", 1800)

    def do_import(self):
        p = self.selected()
        if not p:
            self.app.toast("Find and select a week first.", "warn")
            return
        app = self.app
        force, send = bool(self.force.get()), not self.no_email.get()
        a = self.analysis.get(p.email_id) or {}
        remove = bool(self.remove_stale.get())
        parts = []
        if "diff" in a:
            d = a["diff"]
            n_new = len(p.shifts) if force else len(d["new"])
            n_rm = len(d["removed"]) if remove else 0
            if n_new:
                parts.append(f"Add {n_new} shift(s) for the week of {core.fmt_date(p.week_start)} to Google Calendar.")
            else:
                parts.append("Everything in this week is already on your calendar.")
            if n_rm:
                parts.append(f"Remove {n_rm} shift(s) that are no longer in this schedule.")
            if a["conflicts"]:
                parts.append(f"Heads up: {len(a['conflicts'])} conflict(s) with your other events (see the preview).")
        else:
            done_keys = set(app.store.data["imported_shift_keys"])
            n_new = len([s for s in p.shifts if s.key not in done_keys or force])
            parts.append(f"Add up to {n_new} shift(s) for the week of {core.fmt_date(p.week_start)} to Google Calendar.")
        if send:
            parts.append("Your weekly report will be emailed.")
        if p.superseded:
            parts.insert(0, "A NEWER schedule email exists for this week. Import that one instead unless you want this older version.")
        if not app.confirm("Import schedule", "\n\n".join(parts), "Import"):
            return
        logs = []

        def work():
            gmail, cal = core.build_services()
            return core.perform_import(gmail, cal, app.config_data, app.store, p, force=force, send_report=send,
                                       log=logs.append, remove_stale=remove)

        def done(result):
            set_text(self.preview, "\n".join(logs))
            app.invalidate()
            self.analysis.clear()
            bits = [f"{len(result['created_events'])} added"]
            if result["removed"]:
                bits.append(f"{result['removed']} removed")
            if result["on_calendar"]:
                bits.append(f"{result['on_calendar']} were already there")
            if result["email_error"]:
                app.toast("Shifts saved, but the report email failed: " + result["email_error"], "warn", 9000)
            else:
                app.toast("Done. " + ", ".join(bits) + ".", "success")
            self.set_results(self.results)

        def failed(err):
            app.invalidate()
            self.analysis.clear()
            self.set_results(self.results)

        app.run_job([self.import_btn, self.find_btn], "Importing...", work, done, failed, exclusive=True)


class ManagePage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        actions = self.build_header("Manage weeks", "Everything this app has imported. Delete or undo it here.")
        button(actions, "Refresh", self.refresh, "normal", width=90).pack(side="left")

        frame, self.tree = make_tree(self, [("week", "Week", 160, "w"), ("hours", "Hours", 80, "w"),
                                            ("pay", "Est. pay", 90, "w"), ("events", "Events", 70, "w"),
                                            ("at", "Imported", 180, "w")], height=14)
        frame.pack(fill="both", expand=True)
        self.tree_frame = frame
        make_sortable(self.tree, {"hours", "pay", "events"})

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=14)
        self.btns = [
            button(row, "Delete selected week", self.delete, "danger", width=180),
            button(row, "Undo last import", self.undo, "normal", width=150),
            button(row, "Reset everything", self.reset, "danger", width=150),
        ]
        for b in self.btns:
            b.pack(side="left", padx=(0, 10))

    def on_show(self):
        self.refresh()

    def refresh(self):
        self.app.reload_store()
        self.tree.delete(*self.tree.get_children())
        for key, e in sorted(self.app.store.data["weeks"].items()):
            self.tree.insert("", "end", iid=key, values=(
                core.fmt_date(date.fromisoformat(key)), f"{e.get('total_hours', 0):.2f}",
                f"${e.get('estimated_pay', 0):,.2f}", len(e.get("event_ids", [])), fmt_stamp(e.get("imported_at", ""))))
        show_empty(self.tree_frame, None if self.app.store.data["weeks"] else
                   "Nothing imported yet.\nImported schedules will show up here.")

    def _run(self, status, work, done_msg):
        def done(_r):
            self.app.invalidate()
            self.app.toast(done_msg, "success")
            self.refresh()

        self.app.run_job(self.btns, status, work, done, exclusive=True)

    def delete(self):
        sel = self.tree.selection()
        if not sel:
            self.app.toast("Select a week first.", "warn")
            return
        key = sel[0]
        e = self.app.store.week_entry(key)
        if not self.app.confirm("Delete this week?", f"{len(e['event_ids'])} calendar event(s) for the week of "
                                f"{core.fmt_date(date.fromisoformat(key))} will be removed from Google Calendar.",
                                "Delete", danger=True):
            return

        def work():
            _, cal = core.build_services()
            return core.perform_delete_week(cal, self.app.store, key)

        self._run("Deleting...", work, "Week deleted.")

    def undo(self):
        last = self.app.store.last_import()
        entry = self.app.store.week_entry(last["week"]) if last else None
        if not entry:
            self.app.toast("Nothing to undo.", "warn")
            return
        if not self.app.confirm("Undo last import?", f"Removes the {len(entry['event_ids'])} event(s) from the week "
                                f"of {core.fmt_date(date.fromisoformat(last['week']))}.", "Undo", danger=True):
            return

        def work():
            _, cal = core.build_services()
            return core.perform_undo(cal, self.app.store)

        self._run("Undoing...", work, "Last import undone.")

    def reset(self):
        weeks = self.app.store.data["weeks"]
        if not weeks:
            self.app.toast("Nothing tracked yet.", "warn")
            return
        total = sum(len(e["event_ids"]) for e in weeks.values())
        if not self.app.confirm("Reset everything?", f"Deletes ALL {total} calendar event(s) this app ever created "
                                f"({len(weeks)} week(s)) and clears its history.", "Reset", danger=True,
                                type_to_confirm="RESET"):
            return

        def work():
            _, cal = core.build_services()
            return core.perform_reset(cal, self.app.store)

        self._run("Resetting...", work, "Everything cleared.")


class ReportPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.report = None
        actions = self.build_header("Weekly report", "The email you get: all jobs, classes, and conflicts")
        self.start = entry(actions, width=110)
        self.start.insert(0, core.fmt_date(core.next_week_bounds()[0]))
        self.start.pack(side="left", padx=(0, 6))
        button(actions, "This week", lambda: self._set(core.this_week_bounds()[0]), "normal", width=90).pack(side="left", padx=2)
        button(actions, "Next week", lambda: self._set(core.next_week_bounds()[0]), "normal", width=90).pack(side="left", padx=2)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(0, 12))
        self.btns = [
            button(row, "Preview", self.preview, "primary", width=110),
            button(row, "Send email", self.send, "normal", width=110),
            button(row, "Copy", self.copy, "normal", width=80),
            button(row, "Export CSV", self.export, "normal", width=110),
            button(row, "Open HTML preview", self.open_html, "normal", width=170),
        ]
        for b in self.btns:
            b.pack(side="left", padx=(0, 8))

        self.box = textbox(self, size=14)
        self.box.pack(fill="both", expand=True)
        self.box.configure(state="disabled")

    def _set(self, d):
        self.start.delete(0, "end")
        self.start.insert(0, core.fmt_date(d))
        self.preview()

    def on_show(self):
        if self.report is None:
            self.preview()

    def refresh(self):
        self.preview()

    def preview(self):
        if not self.need_config():
            return
        try:
            ws = core.parse_date_flexible(self.start.get())
        except ValueError as e:
            self.app.toast(str(e), "error")
            return
        cfg = self.app.config_data

        def work():
            _, cal = core.build_services()
            return core.generate_weekly_report(cal, cfg, ws, ws + timedelta(days=6))

        def done(rep):
            self.report = rep
            self.app.set_offline(rep.get("stale_at"))
            set_text(self.box, rep["body"])

        self.app.run_job(self.btns, "Building report...", work, done)

    def send(self):
        if not self.report:
            self.app.toast("Preview the report first.", "warn")
            return
        if self.report.get("stale_at"):
            self.app.toast("You're offline, so this report is from saved data. Reconnect and preview again before sending.",
                           "warn", 7000)
            return
        to = self.app.config_data.get("summary_email_to")
        if not to:
            self.app.toast("Set your email under Settings first.", "warn")
            return
        if not self.app.confirm("Send report?", f"Email this report to {to}.", "Send"):
            return
        rep = self.report
        cfg_now = self.app.config_data

        def work():
            gmail, _ = core.build_services()
            return core.send_email(gmail, to, rep["subject"], rep["body"], core.email_html(cfg_now, rep))

        self.app.run_job(self.btns, "Sending...", work, lambda _r: self.app.toast(f"Sent to {to}", "success"))

    def open_html(self):
        if not self.report:
            self.app.toast("Preview the report first.", "warn")
            return
        core.CACHE_DIR.mkdir(exist_ok=True)
        path = core.CACHE_DIR / "report-preview.html"
        path.write_text(self.report["html"], encoding="utf-8")
        webbrowser.open(path.as_uri())

    def copy(self):
        text = self.box.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.toast("Copied.", "success", 1800)

    def export(self):
        if not self.report:
            self.app.toast("Preview the report first.", "warn")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="schedule.csv",
                                            filetypes=[("CSV file", "*.csv")])
        if path:
            core.export_events_csv(path, self.report["events"])
            self.app.toast(f"Saved {os.path.basename(path)}", "success")


class SettingsPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app)
        actions = self.build_header("Settings", "Saved to config.json in this folder")
        button(actions, "Save changes", self.save, "primary", width=130).pack(side="left")

        self.tab_bar = segmented(self, self.TABS, self._show_tab, size=13)
        self.tab_bar.pack(anchor="w", pady=(0, 16))
        self.scroll = scroll_frame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)
        self.tab_frames = {name: ctk.CTkFrame(self.scroll, fg_color="transparent") for name in self.TABS}
        self.tab_bar.set(self.TABS[0])
        self.tab_frames[self.TABS[0]].pack(fill="x")
        self.vars = {}
        self.job_rows = []
        self._stamp = None

    TABS = ["General", "Jobs & pay", "Alerts", "Data"]

    def _show_tab(self, name):
        for frame in self.tab_frames.values():
            frame.pack_forget()
        self.tab_frames[name].pack(fill="x")
        self.scroll._parent_canvas.yview_moveto(0)

    def on_show(self):
        """The form is built once, and again only after the settings have changed underneath it."""
        if self._stamp != self.app.config_stamp:
            self._rebuild()

    def refresh(self):
        self._rebuild()

    def _rebuild(self):
        for frame in self.tab_frames.values():
            for w in frame.winfo_children():
                w.destroy()
        self.vars, self.job_rows = {}, []
        self._stamp = self.app.config_stamp
        self._build()

    def _section(self, title, hint=""):
        c = card(self._tab)
        c.pack(fill="x", pady=(0, 14), padx=(0, 6))
        label(c, title, 16, "bold").pack(anchor="w", padx=20, pady=(18, 0))
        if hint:
            label(c, hint, 12, color=C["muted"], wraplength=820, justify="left").pack(anchor="w", padx=20, pady=(2, 0))
        body = ctk.CTkFrame(c, fg_color="transparent")
        body.pack(fill="x", padx=20, pady=(10, 18))
        body.grid_columnconfigure(0, minsize=int(250 * self.app.scale))
        body.grid_columnconfigure(2, weight=1)
        return body

    def _field(self, body, row, key, text, value, width=340, hint=""):
        label(body, text, 13, color=C["muted"]).grid(row=row, column=0, sticky="nw", pady=(9, 5), padx=(0, 16))
        cell = ctk.CTkFrame(body, fg_color="transparent")
        cell.grid(row=row, column=1, sticky="w", pady=5)
        e = entry(cell, width=width)
        e.insert(0, "" if value is None else str(value))
        e.pack(anchor="w")
        self.vars[key] = e
        if hint:
            label(cell, hint, 11, color=C["muted"], wraplength=max(width, 360), justify="left", anchor="w").pack(
                anchor="w", pady=(3, 0))

    def _build(self):
        cfg = self.app.config_data
        if cfg is None:
            label(self.tab_frames["General"], "config.json could not be loaded. Fix it, then reopen this page.", 14,
                  color=C["danger"]).pack(anchor="w", pady=20)
            return

        self._tab = self.tab_frames["General"]
        b = self._section("You")
        self._field(b, 0, "display_name", "Your name", cfg.get("display_name", ""), hint="Used in the dashboard greeting")
        self._field(b, 1, "summary_email_to", "Send reports to", cfg.get("summary_email_to"))
        self.email_html = switch(b, text="Send the weekly report as a styled HTML email (plain text is always included)",
                                        progress_color=C["accent"], font=font(13))
        if cfg.get("email_html", True):
            self.email_html.select()
        self.email_html.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 2))

        b = self._section("Google & calendar", "Which emails count as your Domino's schedule, and where events go.")
        self._field(b, 0, "gmail_query", "Gmail search", cfg["gmail_query"])
        self._field(b, 1, "calendar_id", "Write shifts to calendar", cfg["calendar_id"], hint="'primary' is your main calendar")
        self._field(b, 2, "report_calendars", "Read reports from", ", ".join(cfg.get("report_calendars", [])),
                    hint="Comma separated calendar IDs")
        self._field(b, 3, "timezone", "Timezone", cfg["timezone"])
        self._field(b, 4, "event_title", "Dominos event title", cfg["event_title"])
        self._field(b, 5, "store_name", "Store name", cfg.get("store_name", ""))
        self._field(b, 6, "reminder_minutes_before", "Reminders (minutes before)",
                    ", ".join(str(m) for m in core.reminder_list(cfg)), width=160,
                    hint="Comma separated. 60, 30 = 1 hour and 30 min before")

        self._tab = self.tab_frames["Jobs & pay"]
        b = self._section("Jobs & pay", "Any calendar event whose title contains the match text counts as that job. "
                          "Turn on back-to-back OK when a job splits shifts for a break.")
        self.jobs_box = b
        for col, t in enumerate(["Job", "Title contains (comma separated)", "$ / hour", ""]):
            label(b, t, 12, "bold", C["muted"]).grid(row=0, column=col, sticky="w", padx=(0, 10))
        wages, brk = cfg.get("job_wages", {}), set(cfg.get("break_ok_categories", []))
        for name, match in cfg.get("job_match", {}).items():
            self._add_job(name, match, wages.get(name, 0), name in brk)
        button(b, "+ Add job", lambda: self._add_job("", "", 0, False), "normal", width=110).grid(
            row=99, column=0, sticky="w", pady=(10, 0))

        b = self._section("Taxes", "Optional. Adds a take-home estimate next to your pay. Set 0 to hide it.")
        self._field(b, 0, "tax_rate_percent", "Estimated tax withheld (%)", f"{float(cfg.get('tax_rate_percent') or 0):g}", width=100)

        b = self._section("Pay schedule", "Optional. Adds a Pay periods view to Earnings and shows your next payday.")
        sched = cfg.get("pay_schedule") or {}
        label(b, "How often you're paid", 13, color=C["muted"]).grid(row=0, column=0, sticky="w", pady=6, padx=(0, 16))
        self.pay_type = option_menu(b, list(core.PAY_TYPES), width=170)
        self.pay_type.set(sched.get("type", "off") if sched.get("type") in core.PAY_TYPES else "off")
        self.pay_type.grid(row=0, column=1, sticky="w", pady=6)
        self._field(b, 1, "pay_start", "A pay period start date", core.fmt_date(date.fromisoformat(sched["start"]))
                    if sched.get("start") else "", width=140, hint="Any first day of a pay period (weekly / every two weeks).")
        self._field(b, 2, "pay_delay", "Days from period end to payday", int(sched.get("delay_days") or 0), width=100)

        self._tab = self.tab_frames["Alerts"]
        b = self._section("Conflict alerts", "Overlaps are always flagged. Gaps shorter than these are called out.")
        th = cfg.get("conflict_thresholds_minutes", {})
        self._field(b, 0, "very_close", "Very close (minutes)", th.get("very_close", 30), width=100)
        self._field(b, 1, "close", "Close (minutes)", th.get("close", 60), width=100)
        self._field(b, 2, "min_rest_hours", "Minimum rest overnight (hours)", f"{float(cfg.get('min_rest_hours', 8) or 0):g}",
                    width=100, hint="Warns when a late event is followed by an early one the next day. 0 turns it off.")
        self._field(b, 3, "weekly_hours_goal", "Weekly work-hour goal", f"{float(cfg.get('weekly_hours_goal') or 0):g}",
                    width=100, hint="Shows a warning when your paid hours go over. 0 turns it off.")

        b = self._section("School", "Classes are detected by course code (like CIS 111). Adjust only if something is misfiled.")
        self._field(b, 0, "school_course_code_regex", "Course code pattern", cfg["school_course_code_regex"])
        self._field(b, 1, "school_extra_titles", "Always treat as school", ", ".join(cfg.get("school_extra_titles", [])),
                    hint="Comma separated titles")
        self._field(b, 2, "school_exceptions", "Never treat as school", ", ".join(cfg.get("school_exceptions", [])),
                    hint="Comma separated titles")

        self._tab = self.tab_frames["General"]
        b = self._section("App")
        self.auto_email = switch(b, text="Check Gmail for a new schedule when the app opens",
                                        progress_color=C["accent"], font=font(13), command=self._toggle_auto)
        if self.app.prefs.get("auto_check_email", True):
            self.auto_email.select()
        self.auto_email.grid(row=0, column=0, columnspan=3, sticky="w", pady=4)

        b = self._section("Sync between computers",
                          "Keeps your settings and import history the same on every computer signed in to this Google account. "
                          "Your calendar events are already shared by Google Calendar. Sync uses a private calendar called "
                          "\"Schedule Manager sync data\" (you can hide it, but please don't delete it).")
        self.sync_switch = switch(b, text="Sync this computer", progress_color=C["accent"], font=font(13),
                                         command=self._toggle_sync)
        if sync.enabled(self.app.config_data):
            self.sync_switch.select()
        self.sync_switch.grid(row=0, column=0, sticky="w", pady=4)
        self.sync_now_btn = button(b, "Sync now", lambda: self.app.sync_now(manual=True), "normal", width=110)
        self.sync_now_btn.grid(row=0, column=1, sticky="w", padx=(16, 0))
        self.sync_lbl = label(b, "", 12, color=C["muted"], wraplength=700, justify="left", anchor="w")
        self.sync_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.show_sync_status()

        self._tab = self.tab_frames["Data"]
        b = self._section("Connection & data", "Everything for this computer is stored in one folder. Settings and history also sync through your Google account.")
        self.conn_lbl = label(b, "", 12, color=C["muted"], wraplength=700, justify="left", anchor="w")
        self.conn_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.grid(row=0, column=0, columnspan=4, sticky="w")
        for text, cmd, kind in [("Test Google connection", self.check, "normal"),
                                ("Reset Google login", self.reset_login, "normal"),
                                ("Apply reminders to imported shifts", self.apply_reminders, "normal"),
                                ("Create desktop shortcut", self.make_shortcut, "normal"),
                                ("Back up my data", self.backup, "normal"),
                                ("Open app folder", lambda: os.startfile(BASE), "normal")]:
            button(row, text, cmd, kind).pack(side="left", padx=(0, 8), pady=2)

    def _add_job(self, name, match, wage, brk):
        r = len(self.job_rows) + 1
        n = entry(self.jobs_box, width=140, placeholder="Job name")
        n.insert(0, name)
        m = entry(self.jobs_box, width=170, placeholder="text in title")
        m.insert(0, match)
        w = entry(self.jobs_box, width=90, placeholder="0.00")
        w.insert(0, f"{wage:g}" if wage else "")
        sw = switch(self.jobs_box, text="Back-to-back OK", progress_color=C["accent"], font=font(12))
        if brk:
            sw.select()
        for col, wdg in enumerate([n, m, w, sw]):
            wdg.grid(row=r, column=col, sticky="w", padx=(0, 10), pady=4)
        rec = {"name": n, "match": m, "wage": w, "brk": sw}
        x = button(self.jobs_box, "Remove", lambda: self._remove_job(rec), "ghost", width=80)
        x.grid(row=r, column=4, sticky="w")
        rec["remove"] = x
        self.job_rows.append(rec)

    def _remove_job(self, rec):
        for k in ("name", "match", "wage", "brk", "remove"):
            rec[k].destroy()
        self.job_rows.remove(rec)

    def show_sync_status(self):
        if not hasattr(self, "sync_lbl"):
            return
        if not sync.enabled(self.app.config_data):
            text = "Sync is off on this computer."
        else:
            text = self.app.sync_text or "Not synced yet since the app opened."
        try:
            self.sync_lbl.configure(text=text)
        except tk.TclError:
            pass

    def _toggle_sync(self):
        raw = self.app.load_raw_config()
        if raw is None:
            return
        on = bool(self.sync_switch.get())
        raw["sync_enabled"] = on
        core.save_config(raw)
        self.app.reload_config()
        self.show_sync_status()
        if on:
            self.app.sync_now(manual=True)

    def _toggle_auto(self):
        self.app.prefs["auto_check_email"] = bool(self.auto_email.get())
        save_prefs(self.app.prefs)
        self.app.request_sync()

    def save(self):
        raw = self.app.load_raw_config()
        if raw is None:
            return
        g = lambda k: self.vars[k].get().strip()
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
        try:
            raw.update({
                "display_name": g("display_name"), "summary_email_to": g("summary_email_to"),
                "gmail_query": g("gmail_query"), "calendar_id": g("calendar_id") or "primary",
                "report_calendars": split(g("report_calendars")) or [g("calendar_id") or "primary"],
                "timezone": g("timezone"), "event_title": g("event_title"), "store_name": g("store_name"),
                "reminder_minutes_before": core.reminder_list({"reminder_minutes_before": g("reminder_minutes_before")}),
                "tax_rate_percent": self._tax(g("tax_rate_percent")),
                "email_html": bool(self.email_html.get()),
                "pay_schedule": self._pay_schedule(g("pay_start"), g("pay_delay")),
                "min_rest_hours": self._nonneg(g("min_rest_hours"), 24), "weekly_hours_goal": self._nonneg(g("weekly_hours_goal"), 168),
                "conflict_thresholds_minutes": {"very_close": int(g("very_close")), "close": int(g("close"))},
                "school_course_code_regex": g("school_course_code_regex"),
                "school_extra_titles": split(g("school_extra_titles")),
                "school_exceptions": split(g("school_exceptions")),
            })
            job_match, job_wages, brk = {}, {}, []
            for r in self.job_rows:
                name = r["name"].get().strip()
                if not name:
                    continue
                job_match[name] = (r["match"].get().strip() or name).lower()
                job_wages[name] = float(r["wage"].get().strip() or 0)
                if r["brk"].get():
                    brk.append(name)
            raw.update({"job_match": job_match, "job_wages": job_wages, "break_ok_categories": brk})
            from zoneinfo import ZoneInfo
            ZoneInfo(raw["timezone"])
        except (ValueError, KeyError) as e:
            self.app.toast(f"Check your entries: {e}", "error")
            return
        except Exception as e:
            self.app.toast(f"Check your entries: {e}", "error")
            return
        core.save_config(raw)
        self.app.reload_config()
        self.app.toast("Settings saved.", "success")
        self.app.request_sync()

    @staticmethod
    def _nonneg(text: str, maximum: float) -> float:
        value = float(text or 0)
        if not 0 <= value <= maximum:
            raise ValueError(f"must be between 0 and {maximum:g}")
        return value

    def _pay_schedule(self, start_text: str, delay_text: str) -> dict:
        kind = self.pay_type.get()
        start = ""
        if kind in ("weekly", "biweekly"):
            start = core.parse_date_flexible(start_text).isoformat()
        elif start_text.strip():
            start = core.parse_date_flexible(start_text).isoformat()
        delay = int(delay_text or 0)
        if not 0 <= delay <= 60:
            raise ValueError("days to payday must be between 0 and 60")
        return {"type": kind, "start": start, "delay_days": delay}

    @staticmethod
    def _tax(text: str) -> float:
        value = float(text or 0)
        if not 0 <= value <= 90:
            raise ValueError("tax must be between 0 and 90")
        return value

    def check(self):
        app = self.app

        def work():
            gmail, cal = core.build_services()
            prof = gmail.users().getProfile(userId="me").execute()
            c = cal.calendarList().get(calendarId=app.config_data["calendar_id"]).execute()
            return prof["emailAddress"], c.get("summary", app.config_data["calendar_id"])

        def done(res):
            self.conn_lbl.configure(text=f"Connected as {res[0]}. Shifts are written to '{res[1]}'.",
                                    text_color=C["success"])
            app.account_lbl.configure(text=res[0])

        app.run_job([], "Testing connection...", work, done)

    def apply_reminders(self):
        app = self.app
        text = core.describe_reminders(core.reminder_list(app.config_data))
        if not app.confirm("Update reminders?", f"Sets popup reminders of {text} before on every upcoming shift "
                           "this app imported. Events you added yourself are not touched.", "Update"):
            return

        def work():
            _, cal = core.build_services()
            return core.apply_reminders_to_imported(cal, app.config_data, app.store)

        def done(res):
            app.toast(f"Updated reminders on {res[0]} shift(s)" + (f" ({res[1]} failed)" if res[1] else "") + ".",
                      "success" if not res[1] else "warn")

        app.run_job([], "Updating reminders...", work, done)

    def reset_login(self):
        if not self.app.confirm("Reset Google login?", "Signs this app out of Google. The next action will open a "
                                "browser so you can sign in again.", "Reset", danger=True):
            return
        try:
            core.TOKEN_PATH.unlink(missing_ok=True)
            self.app.toast("Signed out. You'll be asked to sign in on the next action.", "success")
        except OSError as e:
            self.app.toast(str(e), "error")

    def make_shortcut(self):
        if getattr(sys, "frozen", False):
            try:
                import install_lib
                install_lib.create_shortcut(install_lib.desktop_dir() / "Schedule Manager.lnk", sys.executable,
                                            workdir=str(Path(sys.executable).parent), icon=sys.executable)
                self.app.toast("Shortcut created on your Desktop.", "success")
            except (OSError, subprocess.SubprocessError) as e:
                self.app.toast(f"Couldn't create shortcut: {e}", "error")
            return
        script = BASE / "create_shortcut.ps1"
        if not script.exists():
            self.app.toast("create_shortcut.ps1 is missing from the app folder.", "error")
            return
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                           check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
            self.app.toast("Shortcut created on your Desktop.", "success")
        except (subprocess.SubprocessError, OSError) as e:
            self.app.toast(f"Couldn't create shortcut: {e}", "error")

    def backup(self):
        try:
            path = core.make_backup("manual")
        except OSError as e:
            self.app.toast(f"Backup failed: {e}", "error")
            return
        self.app.toast(f"Backup saved: backups\\{path.name}", "success")


class HelpPage(Page):
    SHORTCUTS = [("Ctrl + 1 ... 0", "Jump to a page (in sidebar order)"), ("Ctrl + N", "Add an event"), ("Ctrl + F", "Find events on your calendar"),
                 ("Ctrl + K", "Command palette: type what you want to do"), ("F5", "Refresh the current page"),
                 ("Drag in the Week view", "Move / resize an event, or drag empty space to add one"),
                 ("Mouse wheel / two-finger swipe", "Scroll anywhere"), ("Esc", "Close a dialog")]
    MOVING = ["Run 'Schedule Manager Setup.exe' on the new computer, then open Schedule Manager and sign in to Google.",
              "Your settings and import history load from your Google account on the first sync.",
              "Running from the folder instead: copy it over (skip .venv), install Python 3.10+, and double-click 'Schedule Manager.vbs'."
              ] if getattr(sys, "frozen", False) else [
              "Copy the whole folder to the new computer (you can skip the .venv folder).",
              "Install Python 3.10 or newer from python.org (tick 'Add python.exe to PATH').",
              "Double-click 'Schedule Manager.vbs'. The first launch sets itself up (needs internet).",
              "Run 'Create Desktop Shortcut.bat' to get the Desktop and Start menu icons.",
              "If Google asks you to sign in again, that's normal. Settings and history sync from your Google account."]

    TABS = ["Updates", "What's new", "Shortcuts", "This computer"]

    def __init__(self, master, app):
        super().__init__(master, app)
        self.build_header("About & help", f"Schedule Manager v{core.APP_VERSION}")
        self.tab_bar = segmented(self, self.TABS, self._show_tab, size=13)
        self.tab_bar.pack(anchor="w", pady=(0, 16))
        self.scroll = scroll_frame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)
        self.tab_frames = {name: ctk.CTkFrame(self.scroll, fg_color="transparent") for name in self.TABS}
        self.tab_bar.set(self.TABS[0])
        self.tab_frames[self.TABS[0]].pack(fill="x")
        self._stamp = None

    def _show_tab(self, name):
        for frame in self.tab_frames.values():
            frame.pack_forget()
        self.tab_frames[name].pack(fill="x")
        self.scroll._parent_canvas.yview_moveto(0)

    def on_show(self):
        """Built once, and again only if the settings or the latest backup changed."""
        latest = core.latest_backup()
        stamp = (latest.name if latest else None, self.app.config_stamp)
        if self._stamp != stamp:
            self._stamp = stamp
            self._rebuild()

    def refresh(self):
        self._rebuild()

    def _rebuild(self):
        for frame in self.tab_frames.values():
            for w in frame.winfo_children():
                w.destroy()
        self._build()

    def _section(self, title):
        c = card(self._tab)
        c.pack(fill="x", pady=(0, 14), padx=(0, 6))
        label(c, title, 16, "bold").pack(anchor="w", padx=20, pady=(18, 8))
        body = ctk.CTkFrame(c, fg_color="transparent")
        body.pack(fill="x", padx=20, pady=(0, 18))
        return body

    WHATS_NEW = [
        "A new look, and the app opens filling the screen (F11 for true fullscreen). Scrolling is smoother.",
        "Import shows what will change before you confirm: new shifts, ones already on your calendar, and any conflicts.",
        "Drag events in the Week view to move or resize them, or drag on empty space to add one. Undo is always there.",
        "Today timeline on the dashboard, Month view, Find (Ctrl+F) and a command palette (Ctrl+K).",
        "Study planner: finds free time around classes and shifts and adds the blocks you pick.",
        "Earnings by week, month or pay period, with take-home and next payday.",
        "The weekly email is styled HTML, with short-rest alerts and an optional weekly hours goal.",
        "Works offline, backs itself up daily, and refreshes while you leave it open.",
        "Settings and import history sync between computers through your Google account (Settings > General).",
        "Updates: publish from your main computer and every other install offers it here under Updates.",
    ]

    def show_update_status(self):
        if not hasattr(self, "update_lbl"):
            return
        available = bool(self.app.update_check and self.app.update_check.available)
        installed = updater.is_installed()
        text = self.app.update_text or ("" if installed else "Updates apply to the installed app.")
        try:
            self.update_lbl.configure(text=text)
            self.update_check_btn.configure(state="normal" if installed else "disabled")
            if available:
                self.update_now_btn.grid()
            else:
                self.update_now_btn.grid_remove()
        except tk.TclError:
            pass

    def _toggle_update_auto(self):
        self.app.prefs["auto_check_updates"] = bool(self.update_auto.get())
        save_prefs(self.app.prefs)

    def _build(self):
        self._tab = self.tab_frames["Updates"]
        b = self._section("Updates")
        info = updater.read_build_info() if updater.is_installed() else None
        self.version_lbl = label(b, updater.describe_build(info) if info else
                                 f"Version {core.APP_VERSION}. This copy runs from the source folder, so it doesn't update itself.",
                                 14, "bold", anchor="w")
        self.version_lbl.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        label(b, "New versions are published from the computer where the code is changed and downloaded from your GitHub project. "
                 "Nothing is installed without you saying so.", 12, color=C["muted"], wraplength=820, justify="left",
              anchor="w").grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 10))
        self.update_auto = switch(b, text="Look for updates automatically", command=self._toggle_update_auto)
        if self.app.prefs.get("auto_check_updates", True):
            self.update_auto.select()
        self.update_auto.grid(row=2, column=0, sticky="w", pady=4)
        self.update_check_btn = button(b, "Check for updates", lambda: self.app.check_updates(manual=True), "normal", width=160)
        self.update_check_btn.grid(row=2, column=1, sticky="w", padx=(20, 0))
        self.update_now_btn = button(b, "Update now", self.app.offer_update, "primary", width=120)
        self.update_now_btn.grid(row=2, column=2, sticky="w", padx=(8, 0))
        self.update_lbl = label(b, "", 12, color=C["muted"], wraplength=820, justify="left", anchor="w")
        self.update_lbl.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.show_update_status()

        self._tab = self.tab_frames["What's new"]
        b = self._section("What's new in v2")
        label(b, "\n\n".join("\u2022  " + line for line in self.WHATS_NEW), 13, color=C["muted"], wraplength=860,
              justify="left", anchor="w").pack(anchor="w")

        self._tab = self.tab_frames["Shortcuts"]
        b = self._section("Keyboard & mouse")
        keys = "\n\n".join(k for k, _ in self.SHORTCUTS)
        what = "\n\n".join(w for _, w in self.SHORTCUTS)
        label(b, keys, 13, "bold", C["accent_hover"], justify="left", anchor="nw").pack(side="left", anchor="n")
        label(b, what, 13, color=C["muted"], justify="left", anchor="nw").pack(side="left", anchor="n", padx=(36, 0))

        self._tab = self.tab_frames["This computer"]
        b = self._section("Moving to another computer")
        label(b, "\n\n".join(f"{i}.  {step}" for i, step in enumerate(self.MOVING, start=1)), 13, color=C["muted"],
              wraplength=860, justify="left", anchor="w").pack(anchor="w")

        b = self._section("Your data")
        latest = core.latest_backup()
        build = updater.read_build_info() if updater.is_installed() else None
        rows = [("Version", updater.describe_build(build) if build else f"{core.APP_VERSION} (running from source)"),
                ("App folder", str(core.BASE_DIR)),
                ("Settings", "config.json   (edit under Settings)"),
                ("History & imports", "state.json   (backed up automatically once a day)"),
                ("Latest backup", latest.name if latest else "none yet"),
                ("Login", "token.json stays in this folder; 'Reset Google login' in Settings signs you out")]
        b.grid_columnconfigure(1, weight=1)
        for i, (k, v) in enumerate(rows):
            label(b, k, 13, "bold", anchor="w").grid(row=i, column=0, sticky="nw", pady=4, padx=(0, 30))
            label(b, v, 13, color=C["muted"], wraplength=640, justify="left", anchor="w").grid(row=i, column=1, sticky="nw", pady=4)

        b = self._section("Diagnostics")
        label(b, "If something looks wrong, copy the diagnostics and send them along with a description.", 13,
              color=C["muted"], wraplength=860, justify="left", anchor="w").pack(anchor="w", pady=(0, 10))
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x")
        button(row, "Copy diagnostics", self.copy_diagnostics, "normal").pack(side="left", padx=(0, 8))
        button(row, "Open error log", self.open_log, "normal").pack(side="left", padx=(0, 8))
        button(row, "Open app folder", lambda: os.startfile(BASE), "normal").pack(side="left")

    def diagnostics(self) -> str:
        import platform
        log_tail = ""
        try:
            log_tail = "".join(LOG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)[-25:])
        except OSError:
            log_tail = "(no errors logged)"
        cfg = self.app.config_data or {}
        return "\n".join([
            f"Schedule Manager v{core.APP_VERSION}", f"Python {platform.python_version()}  Tk {self.app.tk.call('info', 'patchlevel')}",
            f"Windows {platform.version()}  DPI scale {self.app.scale:.2f}", f"Folder: {BASE}",
            f"Jobs: {', '.join((cfg.get('job_match') or {}).keys())}  Timezone: {cfg.get('timezone')}",
            f"Build: {updater.describe_build(updater.read_build_info()) if updater.is_installed() else 'source'}",
            f"Tracked weeks: {len(self.app.store.data['weeks'])}", f"Sync: {self.app.sync_text or 'not run yet'}", "", "--- recent errors ---", log_tail])

    def copy_diagnostics(self):
        self.clipboard_clear()
        self.clipboard_append(self.diagnostics())
        self.app.toast("Diagnostics copied to the clipboard.", "success")

    def open_log(self):
        if LOG_PATH.exists():
            os.startfile(LOG_PATH)
        else:
            self.app.toast("No errors have been logged. That's good news.", "success")


HELPER_FLAGS = ("--uninstall", "--tablet", "--tablet-launch", "--tablet-stop")


def run_helper(flag: str, rest: list) -> None:
    """The installed exe doubles as its own uninstaller and as the tablet display, chosen by a flag."""
    if flag == "--uninstall":
        import install_lib
        install_lib.uninstall_ui()
    elif flag == "--tablet":
        import tablet_server
        tablet_server.main(rest)
    elif flag == "--tablet-launch":
        import tablet_server
        tablet_server.launch_background(quiet="--quiet" in rest)
    elif flag == "--tablet-stop":
        import tablet_server
        tablet_server.stop_background()


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if len(sys.argv) > 1 and sys.argv[1] in HELPER_FLAGS:
        run_helper(sys.argv[1], sys.argv[2:])
        return
    global _mutex_handle
    first, _mutex_handle = acquire_single_instance()
    if not first:
        focus_existing_window(f"Schedule Manager v{core.APP_VERSION}")
        return
    try:
        App().mainloop()
    except Exception:
        log_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
