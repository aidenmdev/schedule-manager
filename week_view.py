"""Canvas-based weekly calendar grid used by the Week tab."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime, timedelta

import customtkinter as ctk

GUTTER = 58
HEADER_H = 46
CHIP_H = 20
HOUR_H = 52
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = max(2, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _fmt_t(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").replace(":00", "")


def _range(a: datetime, b: datetime) -> str:
    """'1 - 3:15 PM' (drops the first AM/PM when both share it)."""
    first, second = _fmt_t(a), _fmt_t(b)
    if first[-2:] == second[-2:]:
        first = first[:-3]
    return f"{first} - {second}"


def _fit(text: str, font, max_px: float) -> str:
    if max_px <= 0:
        return ""
    if font.measure(text) <= max_px:
        return text
    while text and font.measure(text + "…") > max_px:
        text = text[:-1]
    return text + "…" if text else ""


def _lanes(events: list) -> dict:
    """Assign each overlapping event a (lane, lane_count) so they render side by side."""
    events = sorted(events, key=lambda e: (e["start"], e["end"]))
    result = {}
    cluster, cluster_end = [], None

    def flush():
        lanes_end = []
        placed = []
        for ev in cluster:
            for i, end in enumerate(lanes_end):
                if ev["start"] >= end:
                    lanes_end[i] = ev["end"]
                    placed.append((ev, i))
                    break
            else:
                lanes_end.append(ev["end"])
                placed.append((ev, len(lanes_end) - 1))
        for ev, lane in placed:
            result[id(ev)] = (lane, len(lanes_end))

    for ev in events:
        if cluster and ev["start"] >= cluster_end:
            flush()
            cluster, cluster_end = [], None
        cluster.append(ev)
        cluster_end = ev["end"] if cluster_end is None else max(cluster_end, ev["end"])
    if cluster:
        flush()
    return result


class WeekGrid(ctk.CTkFrame):
    SNAP = 15  # minutes

    def __init__(self, master, colors: dict, category_color, on_select, scale: float = 1.0,
                 on_move=None, on_create=None):
        super().__init__(master, fg_color=colors["card"], corner_radius=14)
        self.on_move = on_move      # (event, new_start, new_end): drag to move / resize an event
        self.on_create = on_create  # (day, start, end): drag on empty space
        self._drag = None
        self._geo = None
        self._blocks = []
        self.C = colors
        self.category_color = category_color
        self.on_select = on_select
        self.week_start: date = date.today()
        self.days: dict = {}
        self.conflicts: dict = {}
        self.selected_id = None
        self._item_events = {}

        self.s = max(1.0, scale)
        self.font_title = tkfont.Font(family="Segoe UI", size=round(9 * self.s), weight="bold")
        self.font_small = tkfont.Font(family="Segoe UI", size=round(8 * self.s))
        self.font_head = tkfont.Font(family="Segoe UI", size=round(10 * self.s), weight="bold")

        self.header = tk.Canvas(self, height=int(HEADER_H * self.s), bg=colors["card"], highlightthickness=0)
        self.header.pack(fill="x", padx=(6, 0), pady=(8, 0))
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self.canvas = tk.Canvas(body, bg=colors["card"], highlightthickness=0, yscrollincrement=1)  # 1 unit = 1px
        self.scroll = ctk.CTkScrollbar(body, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self._first_draw = True
        self.scroller = None  # the app sets this to its Scroller

    def set_data(self, week_start: date, days: dict, conflicts: dict):
        self.week_start = week_start
        self.days = days
        self.conflicts = conflicts
        self.selected_id = None
        self._first_draw = True
        self.redraw()

    def _hour_range(self):
        lo, hi = 8, 21
        for evs in self.days.values():
            for e in evs:
                if e["all_day"]:
                    continue
                lo = min(lo, e["start"].hour)
                end_h = 24 if e["end"].date() > e["start"].date() else e["end"].hour + (1 if e["end"].minute else 0)
                hi = max(hi, end_h)
        return max(0, lo - 0), min(24, hi + 1)

    def redraw(self):
        c, h = self.canvas, self.header
        c.delete("all")
        h.delete("all")
        width = max(c.winfo_width(), 400)
        s = self.s
        gutter = GUTTER * s
        col_w = (width - gutter - 4) / 7
        lo, hi = self._hour_range()
        hour_h = HOUR_H * s
        total_h = (hi - lo) * hour_h
        c.configure(scrollregion=(0, 0, width, total_h + 10))
        self._item_events.clear()
        self._blocks = []
        self._geo = {"gutter": gutter, "col_w": col_w, "lo": lo, "hour_h": hour_h, "total_h": total_h}

        conflict_ids = {}
        for cs in self.conflicts.values():
            for cf in cs:
                for ev in (cf["a"], cf["b"]):
                    key = ev.get("id") or id(ev)
                    conflict_ids[key] = max(conflict_ids.get(key, 0), 2 if cf["type"] == "overlap" else 1)

        today = date.today()
        chips_rows = max([sum(1 for e in evs if e["all_day"]) for evs in self.days.values()] or [0])
        h.configure(height=int((HEADER_H + chips_rows * CHIP_H) * s))

        # header: day names + all-day chips
        for i in range(7):
            d = self.week_start + timedelta(days=i)
            x0 = gutter + i * col_w
            is_today = d == today
            if is_today:
                _round_rect(h, x0 + 3, 4 * s, x0 + col_w - 3, (HEADER_H - 4) * s, 10 * s, fill=self.C["accent"], outline="")
            h.create_text(x0 + col_w / 2, 16 * s, text=DAY_NAMES[i].upper(), font=self.font_small,
                          fill="#ffffff" if is_today else self.C["muted"])
            h.create_text(x0 + col_w / 2, 32 * s, text=f"{d.month}/{d.day}", font=self.font_head,
                          fill="#ffffff" if is_today else self.C["text"])
            row = 0
            for e in self.days.get(d, []):
                if not e["all_day"]:
                    continue
                y0 = (HEADER_H + row * CHIP_H) * s
                _round_rect(h, x0 + 3, y0, x0 + col_w - 3, y0 + (CHIP_H - 3) * s, 6 * s,
                            fill=self.category_color(e["category"]), outline="")
                h.create_text(x0 + 8, y0 + (CHIP_H - 3) * s / 2, text=e["summary"], anchor="w",
                              font=self.font_small, fill="#ffffff", width=col_w - 14)
                row += 1

        # hour grid
        for hr in range(lo, hi + 1):
            y = (hr - lo) * hour_h
            c.create_line(gutter, y, width, y, fill=self.C["border"])
            if hr < hi:
                label = datetime(2000, 1, 1, hr % 24).strftime("%I %p").lstrip("0")
                c.create_text(gutter - 8, y + 2, text=label, anchor="ne", font=self.font_small, fill=self.C["muted"])
        for i in range(8):
            x = gutter + i * col_w
            c.create_line(x, 0, x, total_h, fill=self.C["border"])

        # events
        for i in range(7):
            d = self.week_start + timedelta(days=i)
            x0 = gutter + i * col_w
            if d == today:
                c.create_rectangle(x0 + 1, 0, x0 + col_w, total_h, fill=self.C["today_bg"], outline="", tags="bg")
                c.tag_lower("bg")
            timed = [e for e in self.days.get(d, []) if not e["all_day"]]
            lanes = _lanes(timed)
            for e in timed:
                lane, count = lanes[id(e)]
                start_min = (e["start"].hour * 60 + e["start"].minute)
                end_dt = e["end"]
                end_min = 24 * 60 if end_dt.date() > d else end_dt.hour * 60 + end_dt.minute
                y1 = (start_min / 60 - lo) * hour_h + 1
                y2 = (end_min / 60 - lo) * hour_h - 1
                lane_w = (col_w - 8) / count
                ex1 = x0 + 4 + lane * lane_w
                ex2 = ex1 + lane_w - 2
                color = self.category_color(e["category"])
                sev = conflict_ids.get(e.get("id") or id(e), 0)
                selected = (e.get("id") or id(e)) == self.selected_id
                outline = "#ffffff" if selected else (self.C["warning"] if sev else "")
                item = _round_rect(c, ex1, y1, ex2, y2, 8 * s, fill=color,
                                   outline=outline, width=2 if (selected or sev) else 0)
                self._item_events[item] = e
                self._blocks.append((ex1, y1, ex2, y2, e))
                bh = y2 - y1
                avail = ex2 - ex1 - 12 * s
                title = _fit(e["summary"], self.font_title, avail)
                times = _range(e["start"], e["end"])
                if bh >= 44 * s and avail >= 40 * s:
                    ids = [
                        c.create_text(ex1 + 7 * s, y1 + 5 * s, text=title, anchor="nw", font=self.font_title, fill="#ffffff"),
                        c.create_text(ex1 + 7 * s, y1 + 5 * s + 16 * s, anchor="nw", font=self.font_small,
                                      text=_fit(times, self.font_small, avail), fill="#e8eeff"),
                    ]
                else:
                    line = _fit(f"{e['summary']}  {_fmt_t(e['start'])}", self.font_small, avail)
                    ids = [c.create_text(ex1 + 7 * s, y1 + bh / 2, text=line, anchor="w", font=self.font_small,
                                         fill="#ffffff")]
                for tid in ids:
                    self._item_events[tid] = e
                if sev == 2:
                    c.create_text(ex2 - 6, y1 + 4, text="!", anchor="ne", font=self.font_head, fill=self.C["warning"])

        # "now" line
        if self.week_start <= today <= self.week_start + timedelta(days=6):
            now = datetime.now()
            y = ((now.hour * 60 + now.minute) / 60 - lo) * hour_h
            if 0 <= y <= total_h:
                x0 = gutter + (today - self.week_start).days * col_w
                c.create_line(x0, y, x0 + col_w, y, fill=self.C["danger"], width=2)
                c.create_oval(x0 - 4, y - 4, x0 + 4, y + 4, fill=self.C["danger"], outline="")

        if self._first_draw:
            self._first_draw = False
            first = min((e["start"].hour * 60 + e["start"].minute
                         for evs in self.days.values() for e in evs if not e["all_day"]), default=8 * 60)
            target = max(0, (first / 60 - lo - 1) * hour_h) / max(total_h, 1)
            self.after(30, lambda: self.canvas.yview_moveto(min(target, 0.9)))

    def _on_wheel(self, event):
        distance = -event.delta / 120 * 96 * self.s  # about three lines per notch
        if self.scroller is not None:
            self.scroller.add(self.canvas, distance)  # eased and coalesced, like every other scrolling area
        else:
            px = int(distance)
            self.canvas.yview_scroll(px or (-1 if event.delta > 0 else 1), "units")

    def _block_at(self, x, y):
        for ex1, y1, ex2, y2, ev in reversed(self._blocks):
            if ex1 <= x <= ex2 and y1 <= y <= y2:
                return ex1, y1, ex2, y2, ev
        return None

    def _day_at(self, x):
        g = self._geo
        if not g or x < g["gutter"]:
            return None
        col = int((x - g["gutter"]) // g["col_w"])
        return self.week_start + timedelta(days=col) if 0 <= col < 7 else None

    def _time_at(self, day, y) -> datetime:
        """Snap a canvas y position to the nearest 15 minutes on `day`."""
        g = self._geo
        minutes = g["lo"] * 60 + (y / g["hour_h"]) * 60
        minutes = max(0, min(24 * 60, round(minutes / self.SNAP) * self.SNAP))
        return datetime(day.year, day.month, day.day) + timedelta(minutes=minutes)

    @staticmethod
    def _draggable(ev) -> bool:
        return not ev["all_day"] and ev["start"].date() == ev["end"].date()

    def _canvas_xy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _on_press(self, event):
        x, y = self._canvas_xy(event)
        self._drag = None
        hit = self._block_at(x, y)
        if hit:
            ex1, y1, ex2, y2, ev = hit
            self.selected_id = ev.get("id") or id(ev)
            self.redraw()
            self.on_select(ev)
            if self.on_move and self._draggable(ev):
                mode = "resize" if y >= y2 - 8 * self.s else "move"
                self._drag = {"mode": mode, "ev": ev, "x0": x, "y0": y, "moved": False, "target": None}
            return
        self.selected_id = None
        self.redraw()
        self.on_select(None)
        day = self._day_at(x)
        if self.on_create and day is not None and 0 <= y <= self._geo["total_h"]:
            self._drag = {"mode": "create", "day": day, "x0": x, "y0": y, "moved": False, "target": None}

    def _on_drag(self, event):
        d = self._drag
        if not d or not self._geo:
            return
        x, y = self._canvas_xy(event)
        if not d["moved"] and abs(y - d["y0"]) < 6 * self.s and abs(x - d["x0"]) < 6 * self.s:
            return
        d["moved"] = True
        g = self._geo
        if d["mode"] == "create":
            day = d["day"]
            a, b = sorted((self._time_at(day, d["y0"]), self._time_at(day, y)))
            if b - a < timedelta(minutes=self.SNAP):
                b = a + timedelta(minutes=self.SNAP)
            target = (day, a, b)
        else:
            ev = d["ev"]
            dur = ev["end"] - ev["start"]
            if d["mode"] == "move":
                day = self._day_at(x) or ev["day"]
                shift = timedelta(minutes=round((y - d["y0"]) / g["hour_h"] * 60 / self.SNAP) * self.SNAP)
                start = datetime(day.year, day.month, day.day, ev["start"].hour, ev["start"].minute) + shift
                start = max(datetime(day.year, day.month, day.day), min(start, datetime(day.year, day.month, day.day) + timedelta(hours=24) - dur))
                target = (day, start, start + dur)
            else:  # resize the bottom edge
                end = self._time_at(ev["day"], y)
                end = max(ev["start"] + timedelta(minutes=self.SNAP), end)
                target = (ev["day"], ev["start"], end)
        d["target"] = target
        self._draw_ghost(*target)

    def _draw_ghost(self, day, start, end):
        g, s = self._geo, self.s
        c = self.canvas
        c.delete("ghost")
        col = (day - self.week_start).days
        x1 = g["gutter"] + col * g["col_w"] + 4
        x2 = x1 + g["col_w"] - 8
        y1 = ((start.hour * 60 + start.minute) / 60 - g["lo"]) * g["hour_h"]
        y2 = ((end.hour * 60 + end.minute) / 60 - g["lo"]) * g["hour_h"] if end.date() == start.date() else g["total_h"]
        _round_rect(c, x1, y1, x2, y2, 8 * s, fill=self.C["select"], outline=self.C["accent"], width=2, dash=(5, 3), tags="ghost")
        c.create_text(x1 + 7 * s, y1 + 5 * s, anchor="nw", font=self.font_small, fill="#ffffff", tags="ghost",
                      text=f"{_fmt_t(start)} - {_fmt_t(end)}")

    def _on_release(self, _event):
        d, self._drag = self._drag, None
        self.canvas.delete("ghost")
        if not d or not d["moved"] or not d["target"]:
            return
        day, start, end = d["target"]
        if d["mode"] == "create":
            if self.on_create:
                self.on_create(day, start, end)
        else:
            ev = d["ev"]
            if (start, end) != (ev["start"], ev["end"]) and self.on_move:
                self.on_move(ev, start, end)

    def _on_hover(self, event):
        if self._drag:
            return
        x, y = self._canvas_xy(event)
        hit = self._block_at(x, y)
        cursor = ""
        if hit and self.on_move and self._draggable(hit[4]):
            cursor = "sb_v_double_arrow" if y >= hit[3] - 8 * self.s else "hand2"
        elif hit:
            cursor = "hand2"
        elif self.on_create and self._day_at(x) is not None:
            cursor = "crosshair"
        self.canvas.configure(cursor=cursor)
