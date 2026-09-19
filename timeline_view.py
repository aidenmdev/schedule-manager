"""Horizontal 'today' strip for the dashboard: your day at a glance with a live 'now' marker."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime

import customtkinter as ctk


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = max(2, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _fit(text: str, font, max_px: float) -> str:
    if max_px <= 0:
        return ""
    if font.measure(text) <= max_px:
        return text
    while text and font.measure(text + "…") > max_px:
        text = text[:-1]
    return text + "…" if text else ""


def _hour_label(h: int) -> str:
    h %= 24
    return f"{h % 12 or 12}{'a' if h < 12 else 'p'}"


class DayTimeline(ctk.CTkFrame):
    def __init__(self, master, colors: dict, category_color, scale: float = 1.0):
        super().__init__(master, fg_color=colors["card"], corner_radius=14)
        self.C, self.category_color, self.s = colors, category_color, max(1.0, scale)
        self.events: list = []
        self.day: date = date.today()
        self.font_small = tkfont.Font(family="Segoe UI", size=round(8 * self.s))
        self.font_bold = tkfont.Font(family="Segoe UI", size=round(9 * self.s), weight="bold")
        self.canvas = tk.Canvas(self, bg=colors["card"], highlightthickness=0, height=int(84 * self.s))
        self.canvas.pack(fill="both", expand=True, padx=14, pady=(10, 10))
        self.canvas.bind("<Configure>", lambda _e: self.redraw())

    def set_events(self, events: list, day: date | None = None):
        self.day = day or date.today()
        self.events = [e for e in events if not e["all_day"] and e["day"] == self.day]
        self.redraw()

    def hour_range(self) -> tuple:
        lo, hi = 7, 23
        for e in self.events:
            lo = min(lo, e["start"].hour)
            hi = max(hi, 24 if e["end"].date() > e["start"].date() else e["end"].hour + (1 if e["end"].minute else 0))
        return lo, min(24, hi)

    def x_for(self, dt: datetime, x0: float, width: float, lo: int, hi: int) -> float:
        minutes = (dt - datetime.combine(self.day, datetime.min.time())).total_seconds() / 60
        frac = (minutes - lo * 60) / ((hi - lo) * 60)
        return x0 + max(0.0, min(1.0, frac)) * width

    def redraw(self):
        c, s = self.canvas, self.s
        c.delete("all")
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 60)
        lo, hi = self.hour_range()
        x0, x1 = 10 * s, w - 18 * s
        width = x1 - x0
        axis_y, bar_top, bar_bot = h - 16 * s, 18 * s, h - 24 * s
        span = hi - lo
        step = 1 if span <= 10 else 2 if span <= 20 else 3
        for hr in range(lo, hi + 1, step):
            x = x0 + (hr - lo) / span * width
            c.create_line(x, bar_top - 6 * s, x, bar_bot + 4 * s, fill=self.C["border"])
            c.create_text(x, axis_y + 8 * s, text=_hour_label(hr), font=self.font_small, fill=self.C["muted"])
        if not self.events:
            c.create_text(w / 2, (bar_top + bar_bot) / 2, text="Nothing scheduled today", font=self.font_bold,
                          fill=self.C["muted"])
        for e in sorted(self.events, key=lambda e: e["start"]):
            xa = self.x_for(e["start"], x0, width, lo, hi)
            xb = self.x_for(e["end"], x0, width, lo, hi)
            _round_rect(c, xa, bar_top, max(xb, xa + 6 * s), bar_bot, 8 * s, fill=self.category_color(e["category"]),
                        outline="")
            c.create_text(xa + 7 * s, (bar_top + bar_bot) / 2, anchor="w", font=self.font_bold, fill="#ffffff",
                          text=_fit(e["summary"], self.font_bold, xb - xa - 12 * s))
        now = datetime.now()
        if now.date() == self.day:
            xn = self.x_for(now, x0, width, lo, hi)
            if x0 <= xn <= x1:
                c.create_line(xn, bar_top - 8 * s, xn, bar_bot + 6 * s, fill=self.C["danger"], width=2)
                c.create_oval(xn - 4 * s, bar_top - 12 * s, xn + 4 * s, bar_top - 4 * s, fill=self.C["danger"], outline="")
