"""Canvas-based month overview used by the Month tab."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from datetime import date, timedelta

import customtkinter as ctk

DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
HEADER_H = 30
GAP = 6


def month_grid_bounds(month_start: date) -> tuple:
    """(first Monday shown, last Sunday shown) for the month containing month_start."""
    first = month_start.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    return start, end


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


class MonthGrid(ctk.CTkFrame):
    def __init__(self, master, colors: dict, category_color, on_pick, scale: float = 1.0):
        super().__init__(master, fg_color=colors["card"], corner_radius=14)
        self.C = colors
        self.category_color = category_color
        self.on_pick = on_pick
        self.s = max(1.0, scale)
        self.month_start = date.today().replace(day=1)
        self.days: dict = {}
        self.conflicts: dict = {}
        self.paid_hours: dict = {}
        self.font_day = tkfont.Font(family="Segoe UI", size=round(10 * self.s), weight="bold")
        self.font_small = tkfont.Font(family="Segoe UI", size=round(8 * self.s))
        self.font_head = tkfont.Font(family="Segoe UI", size=round(8 * self.s), weight="bold")
        self.canvas = tk.Canvas(self, bg=colors["card"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)

    def set_data(self, month_start: date, days: dict, conflicts: dict, paid_hours: dict):
        self.month_start = month_start.replace(day=1)
        self.days, self.conflicts, self.paid_hours = days, conflicts, paid_hours
        self.redraw()

    def _layout(self):
        w, h = max(self.canvas.winfo_width(), 300), max(self.canvas.winfo_height(), 200)
        start, end = month_grid_bounds(self.month_start)
        weeks = ((end - start).days + 1) // 7
        head = HEADER_H * self.s
        cell_w = (w - GAP * self.s) / 7
        cell_h = (h - head - GAP * self.s) / weeks
        return start, weeks, head, cell_w, cell_h

    def date_at(self, x: float, y: float):
        """The date under canvas coordinates (x, y), or None."""
        start, weeks, head, cw, ch = self._layout()
        if y < head or x < 0:
            return None
        col, row = int(x // cw), int((y - head) // ch)
        if not (0 <= col < 7 and 0 <= row < weeks):
            return None
        return start + timedelta(days=row * 7 + col)

    def redraw(self):
        c = self.canvas
        c.delete("all")
        s = self.s
        start, weeks, head, cw, ch = self._layout()
        today = date.today()
        for i, name in enumerate(DAY_NAMES):
            c.create_text(i * cw + cw / 2, head / 2, text=name, font=self.font_head, fill=self.C["muted"])
        pad = 3 * s
        for r in range(weeks):
            for col in range(7):
                d = start + timedelta(days=r * 7 + col)
                x0, y0 = col * cw + pad / 2, head + r * ch + pad / 2
                x1, y1 = x0 + cw - pad, y0 + ch - pad
                in_month = d.month == self.month_start.month
                is_today = d == today
                has_overlap = any(cf["type"] == "overlap" for cf in self.conflicts.get(d, []))
                has_tight = bool(self.conflicts.get(d)) and not has_overlap
                outline = self.C["accent"] if is_today else (self.C["danger"] if has_overlap else (
                    self.C["warning"] if has_tight else ""))
                _round_rect(c, x0, y0, x1, y1, 10 * s, fill=self.C["card2"] if in_month else self.C["bg"],
                            outline=outline, width=2 if outline else 0)
                num_color = self.C["text"] if in_month else self.C["muted"]
                if is_today:
                    c.create_oval(x0 + 5 * s, y0 + 5 * s, x0 + 25 * s, y0 + 25 * s, fill=self.C["accent"], outline="")
                    num_color = "#ffffff"
                c.create_text(x0 + 15 * s, y0 + 15 * s, text=str(d.day), font=self.font_day, fill=num_color)
                hrs = self.paid_hours.get(d, 0)
                if hrs and in_month:
                    c.create_text(x1 - 7 * s, y0 + 15 * s, text=f"{hrs:g}h", anchor="e", font=self.font_small,
                                  fill=self.C["muted"])
                evs = sorted(self.days.get(d, []), key=lambda e: (not e["all_day"], e["start"] or 0))
                line_h = 15 * s
                room = int((y1 - (y0 + 30 * s) - 2 * s) // line_h)
                show = evs if len(evs) <= room else evs[:max(room - 1, 0)]
                y = y0 + 30 * s
                for e in show:
                    color = self.category_color(e["category"])
                    _round_rect(c, x0 + 6 * s, y + 2 * s, x0 + 10 * s, y + line_h - 2 * s, 2 * s, fill=color, outline="")
                    when = "" if e["all_day"] else _short_time(e["start"]) + " "
                    c.create_text(x0 + 14 * s, y + line_h / 2, anchor="w", font=self.font_small,
                                  fill=self.C["text"] if in_month else self.C["muted"],
                                  text=_fit(when + e["summary"], self.font_small, x1 - x0 - 20 * s))
                    y += line_h
                if len(evs) > len(show):
                    c.create_text(x0 + 14 * s, y + line_h / 2, anchor="w", font=self.font_small, fill=self.C["accent"],
                                  text=f"+{len(evs) - len(show)} more")

    def _on_click(self, event):
        d = self.date_at(event.x, event.y)
        if d:
            self.on_pick(d)

    def _on_motion(self, event):
        self.canvas.configure(cursor="hand2" if self.date_at(event.x, event.y) else "")


def _short_time(dt) -> str:
    t = dt.strftime("%I:%M%p").lstrip("0").lower()
    return t.replace(":00", "").replace("m", "")
