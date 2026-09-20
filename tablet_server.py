"""Serves a read-only week view of your calendar on the home network, meant for an old tablet.

Run it on the PC, then open http://<pc-address>:8765 in the tablet's browser. The page is plain
HTML and CSS (no flexbox, grid or modern JavaScript) so Android 4.x browsers can show it.
"""
from __future__ import annotations

import argparse
import ctypes
import html
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import dominos_schedule as core

DEFAULT_PORT = 8765
STATUS_PATH = core.BASE_DIR / "tablet_status.txt"
LOG_PATH = core.BASE_DIR / "tablet.log"
PAGE_RELOAD_SECONDS = 180
OFFSETS = range(-2, 5)  # weeks around today that can be browsed with the arrows
GUTTER = 5.5            # percent of the width used by the hour labels
DAY_W = (100 - GUTTER) / 7
DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def short_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    suffix = "a" if dt.hour < 12 else "p"
    return f"{h}{suffix}" if dt.minute == 0 else f"{h}:{dt.minute:02d}{suffix}"


def hours_text(h: float) -> str:
    return f"{h:.1f}".rstrip("0").rstrip(".") + "h"


def text_color_for(hex_color: str) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#0b0f17" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def hour_window(days: dict, default=(8, 22)) -> tuple:
    """(first hour, last hour) of the grid: 8am to 10pm, widened if anything falls outside."""
    lo, hi = default
    for events in days.values():
        for e in events:
            if e["all_day"]:
                continue
            lo = min(lo, e["start"].hour)
            if e["end"].date() > e["start"].date():
                hi = 24
            else:
                hi = max(hi, e["end"].hour + (1 if e["end"].minute else 0))
    lo, hi = max(lo, 0), min(hi, 24)
    if hi - lo < 6:
        hi = min(24, lo + 6)
        lo = max(0, hi - 6)
    return lo, hi


def lay_out(events: list) -> list:
    """[(event, column, columns)] so events that overlap in time sit side by side."""
    placed, cluster, lanes, cluster_end = [], [], [], None

    def flush():
        for ev, lane in cluster:
            placed.append((ev, lane, len(lanes)))

    for e in sorted(events, key=lambda e: (e["start"], e["end"])):
        if cluster_end is not None and e["start"] >= cluster_end:
            flush()
            cluster, lanes, cluster_end = [], [], None
        for i, lane_end in enumerate(lanes):
            if lane_end <= e["start"]:
                lanes[i] = e["end"]
                cluster.append((e, i))
                break
        else:
            lanes.append(e["end"])
            cluster.append((e, len(lanes) - 1))
        cluster_end = e["end"] if cluster_end is None else max(cluster_end, e["end"])
    flush()
    return placed


CSS = """
html,body{margin:0;padding:0;background:#0d1117;color:#e6edf3;font-family:Roboto,"Droid Sans",Arial,sans-serif}
a{color:#e6edf3;text-decoration:none}
#top{position:relative;height:50px}
#top .title{position:absolute;left:0;right:0;top:0;line-height:50px;text-align:center;font-size:22px;font-weight:bold}
#top .nav{position:absolute;left:8px;top:8px;z-index:2}
#top .nav a{display:inline-block;padding:0 14px;height:34px;line-height:34px;margin-right:6px;background:#21262d;border-radius:8px;font-size:16px}
#top .clock{position:absolute;right:14px;top:0;line-height:50px;font-size:22px;color:#9da7b3;z-index:2}
#heads,#allday{position:relative}
#heads{height:46px}
.head{position:absolute;top:0;height:42px;text-align:center;border-radius:8px}
.head .dn{font-size:11px;letter-spacing:1px;color:#9da7b3;padding-top:4px}
.head .dd{font-size:19px;font-weight:bold;line-height:22px}
.head.today{background:#1f6feb}
.head.today .dn{color:#dbe9ff}
.chip{position:absolute;height:19px;line-height:19px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;border-radius:5px;font-size:12px;padding:0 5px;-webkit-box-sizing:border-box;box-sizing:border-box}
#grid{position:relative;height:520px;margin-top:10px}
.col{position:absolute;top:0;bottom:0;border-left:1px solid #21262d}
.col.today{background:#111b2b}
.hl{position:absolute;border-top:1px solid #1b222b}
.hl.hr{border-top-color:#252d38}
.hlabel{position:absolute;left:0;width:4.6%;text-align:right;font-size:11px;color:#6e7885;margin-top:-8px}
.ev{position:absolute;overflow:hidden;border-radius:6px;padding:3px 6px;font-size:13px;line-height:16px;word-wrap:break-word;-webkit-box-sizing:border-box;box-sizing:border-box;border:1px solid #0d1117}
.ev b{display:block}
.ev.short b{white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.ev.narrow{padding:2px 3px;font-size:11px;line-height:14px}
.ev span{font-size:11px;opacity:.85}
.ev.done{opacity:.5}
#now{position:absolute;height:0;border-top:2px solid #ff5a5f;z-index:3;display:none}
#foot{padding:8px 12px;font-size:14px;color:#9da7b3;min-height:22px}
#foot .pill{display:inline-block;margin:0 14px 2px 0;white-space:nowrap}
#foot .dot{display:inline-block;width:9px;height:9px;border-radius:5px;margin-right:6px}
#foot .warn{color:#f0b429}
#foot .right{float:right}
.center{text-align:center;padding:80px 20px;font-size:20px;color:#9da7b3}
"""

SCRIPT = """
(function(){
  var g=document.getElementById('grid');
  function fit(){
    var top=0,el=g;
    while(el){top+=el.offsetTop;el=el.offsetParent;}
    var foot=document.getElementById('foot');
    var h=(window.innerHeight||document.documentElement.clientHeight)-top-(foot?foot.offsetHeight:0)-4;
    if(h>240){g.style.height=h+'px';}
  }
  var t0=new Date().getTime();
  var base=__BASE__;
  function pad(n){return n<10?'0'+n:''+n;}
  function tick(){
    var s=(base+(new Date().getTime()-t0)/1000)%86400;
    var m=Math.floor(s/60),h=Math.floor(m/60)%24,mm=m%60;
    var h12=h%12;if(h12===0){h12=12;}
    var c=document.getElementById('clock');
    if(c){c.innerHTML=h12+':'+pad(mm)+' '+(h>=12?'PM':'AM');}
    var n=document.getElementById('now');
    if(n){
      var p=(m-parseFloat(g.getAttribute('data-start')))/parseFloat(g.getAttribute('data-len'))*100;
      if(p<0||p>100){n.style.display='none';}else{n.style.display='block';n.style.top=p+'%';}
    }
  }
  fit();tick();setInterval(tick,20000);
  window.onresize=fit;
})();
"""


def _page(body: str, script: str = "", reload_seconds: int = PAGE_RELOAD_SECONDS) -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="{reload_seconds}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Schedule</title><style>{CSS}</style></head><body>{body}"
        f"<script>{script}</script></body></html>"
    )


def waiting_page(message: str) -> str:
    return _page(f'<div class="center">{html.escape(message)}</div>', reload_seconds=10)


def _pos(left, width, top=None, height=None) -> str:
    parts = [f"left:{left:.3f}%", f"width:{width:.3f}%"]
    if top is not None:
        parts.append(f"top:{top:.3f}%")
    if height is not None:
        parts.append(f"height:{height:.3f}%")
    return ";".join(parts)


def render_week(entry: dict, week_start: date, offset: int, config: dict, now: datetime,
                updated: datetime | None = None, problem: str = "") -> str:
    """The whole page for one week. `entry` is {'events': [...], 'stale_at': datetime | None}."""
    week_end = week_start + timedelta(days=6)
    days = core.group_events_by_day(entry["events"], week_start, week_end)
    lo, hi = hour_window(days)
    span = (hi - lo) * 60
    today = now.date()
    today_col = (today - week_start).days if week_start <= today <= week_end else -1

    if week_start.month == week_end.month:
        title = f"{week_start.strftime('%b')} {week_start.day} – {week_end.day}, {week_end.year}"
    else:
        title = f"{week_start.strftime('%b')} {week_start.day} – {week_end.strftime('%b')} {week_end.day}, {week_end.year}"

    out = [
        '<div id="top">',
        '<div class="nav">'
        f'<a href="/?w={offset - 1}">&lsaquo;</a>'
        '<a href="/">This week</a>'
        f'<a href="/?w={offset + 1}">&rsaquo;</a></div>',
        f'<div class="title">{title}</div><div class="clock" id="clock"></div></div>',
        '<div id="heads">',
    ]
    for i in range(7):
        d = week_start + timedelta(days=i)
        cls = "head today" if i == today_col else "head"
        out.append(f'<div class="{cls}" style="{_pos(GUTTER + i * DAY_W + 0.2, DAY_W - 0.4)}">'
                   f'<div class="dn">{DAY_NAMES[i]}</div><div class="dd">{d.month}/{d.day}</div></div>')
    out.append("</div>")

    all_day_rows = max((sum(1 for e in days[d] if e["all_day"]) for d in days), default=0)
    if all_day_rows:
        out.append(f'<div id="allday" style="height:{all_day_rows * 21 + 4}px">')
        for i in range(7):
            d = week_start + timedelta(days=i)
            for row, e in enumerate(x for x in days[d] if x["all_day"]):
                color = core.category_color(e["category"])
                out.append(f'<div class="chip" style="{_pos(GUTTER + i * DAY_W + 0.2, DAY_W - 0.4)};top:{row * 21}px;'
                           f'background:{color};color:{text_color_for(color)}">{html.escape(e["summary"])}</div>')
        out.append("</div>")

    out.append(f'<div id="grid" data-start="{lo * 60}" data-len="{span}">')
    for i in range(7):
        cls = "col today" if i == today_col else "col"
        out.append(f'<div class="{cls}" style="{_pos(GUTTER + i * DAY_W, DAY_W)}"></div>')
    for h in range(lo, hi + 1):
        top = (h - lo) * 60 / span * 100
        out.append(f'<div class="hl" style="left:{GUTTER}%;right:0;top:{top:.3f}%"></div>')
        if h < hi:
            label = f"{h % 12 or 12}{'a' if h < 12 else 'p'}"
            out.append(f'<div class="hlabel" style="top:{top:.3f}%">{label}</div>')

    for i in range(7):
        d = week_start + timedelta(days=i)
        timed = [e for e in days[d] if not e["all_day"]]
        for e, lane, lanes in lay_out(timed):
            start_min = max((e["start"].hour * 60 + e["start"].minute) - lo * 60, 0)
            end_min = span if e["end"].date() > e["start"].date() else min(e["end"].hour * 60 + e["end"].minute - lo * 60, span)
            end_min = max(end_min, start_min + 20)
            color = core.category_color(e["category"])
            width = DAY_W / lanes
            duration = (e["end"] - e["start"]).total_seconds() / 60
            classes = "ev" + (" done" if e["end"] <= now else "") + (" short" if duration < 75 else "") + (" narrow" if lanes > 1 else "")
            when = f"<span>{short_time(e['start'])}–{short_time(e['end'])}</span>" if duration >= 40 else ""
            out.append(
                f'<div class="{classes}" title="{html.escape(e["summary"])}" '
                f'style="{_pos(GUTTER + i * DAY_W + lane * width + 0.15, width - 0.3, start_min / span * 100, (end_min - start_min) / span * 100)};'
                f'background:{color};color:{text_color_for(color)}"><b>{html.escape(e["summary"])}</b>{when}</div>')
    if today_col >= 0:
        out.append(f'<div id="now" style="left:{GUTTER + today_col * DAY_W:.3f}%;width:{DAY_W:.3f}%"></div>')
    out.append("</div>")

    out.append('<div id="foot">')
    hours = core.compute_category_hours(entry["events"])
    right = []
    if entry.get("stale_at"):
        right.append(f'<span class="warn">Offline, showing the copy saved {core.fmt_date_short(entry["stale_at"].date())} '
                     f'{short_time(entry["stale_at"])}</span>')
    elif problem:
        right.append(f'<span class="warn">{html.escape(problem)}</span>')
    if updated:
        right.append(f"Updated {short_time(updated)}")
    out.append(f'<div class="right">{" &middot; ".join(right)}</div>')
    for label, h in hours.items():
        if h <= 0:
            continue
        out.append(f'<span class="pill"><span class="dot" style="background:{core.category_color(label)}"></span>'
                   f'{html.escape(label)} {hours_text(h)}</span>')
    if not entry["events"]:
        out.append('<span class="pill">Nothing scheduled</span>')
    out.append("</div>")

    base = now.hour * 3600 + now.minute * 60 + now.second
    return _page("".join(out), SCRIPT.replace("__BASE__", str(base)))


class Feed:
    """Keeps a copy of the calendar in memory and refreshes it in the background."""

    def __init__(self, config: dict, interval: int = 300):
        self.config = config
        self.interval = interval
        self.tz = ZoneInfo(config["timezone"])
        self.weeks: dict = {}
        self.updated: datetime | None = None
        self.problem = ""
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def now(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None)

    def week_start(self, offset: int) -> date:
        today = self.now().date()
        return today - timedelta(days=today.weekday()) + timedelta(weeks=offset)

    def load_saved(self) -> None:
        """Show the last saved copies straight away, before Google has answered."""
        for offset in OFFSETS:
            ws = self.week_start(offset)
            saved = core._load_events_cache(self.config, ws, ws + timedelta(days=6))
            if saved:
                with self.lock:
                    self.weeks.setdefault(ws, {"events": saved[0], "stale_at": saved[1]})

    def refresh(self, calendar) -> None:
        problem = ""
        for offset in OFFSETS:
            ws = self.week_start(offset)
            try:
                events, stale_at = core.fetch_events_or_cached(calendar, self.config, ws, ws + timedelta(days=6))
            except Exception as e:
                problem = "Can't reach Google" if core.is_network_error(e) else f"Calendar error: {e}"
                continue
            with self.lock:
                self.weeks[ws] = {"events": events, "stale_at": stale_at}
        with self.lock:
            self.problem = problem
            if not problem:
                self.updated = self.now()

    def snapshot(self, ws: date):
        with self.lock:
            return self.weeks.get(ws), self.updated, self.problem

    def run(self) -> None:
        calendar = None
        while not self.stop.is_set():
            try:
                if calendar is None:
                    calendar = core.build_services()[1]
                self.refresh(calendar)
                wait = self.interval
            except (Exception, SystemExit) as e:
                with self.lock:
                    self.problem = "Can't reach Google" if core.is_network_error(e) else str(e) or type(e).__name__
                wait = 30
            self.stop.wait(wait)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows this flag lets a second copy bind the same port
    daemon_threads = True


def make_handler(feed: Feed):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, kind: str = "text/html") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, "ok", "text/plain")
            if url.path != "/":
                return self._send(404, waiting_page("Page not found."))
            try:
                offset = int(parse_qs(url.query).get("w", ["0"])[0])
            except ValueError:
                offset = 0
            offset = max(OFFSETS[0], min(OFFSETS[-1], offset))
            ws = feed.week_start(offset)
            entry, updated, problem = feed.snapshot(ws)
            if entry is None:
                return self._send(200, waiting_page(problem or "Loading your calendar..."))
            try:
                page = render_week(entry, ws, offset, feed.config, feed.now(), updated, problem)
            except Exception as e:
                return self._send(500, waiting_page(f"Something went wrong drawing this week ({type(e).__name__})."))
            self._send(200, page)

        def log_message(self, fmt, *args):
            pass

    return Handler


def lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.read() == b"ok"
    except OSError:
        return False


def write_status(text: str) -> None:
    """The background launcher reads this to tell you the address (there is no console window)."""
    try:
        STATUS_PATH.write_text(text, encoding="utf-8")
    except OSError:
        pass


def main(argv=None) -> None:
    if sys.stdout is None or sys.stderr is None:  # started by pythonw, so there is no console
        log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    parser = argparse.ArgumentParser(description="Show your week on a tablet over the home network.")
    parser.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}, or tablet_port in config.json")
    parser.add_argument("--host", default="0.0.0.0", help="use 127.0.0.1 to allow only this computer")
    parser.add_argument("--interval", type=int, default=300, help="seconds between calendar refreshes")
    args = parser.parse_args(argv)

    try:
        config = core.load_config()
    except SystemExit as e:
        write_status(f"Could not start: {e}")
        raise
    port = args.port or int(config.get("tablet_port") or DEFAULT_PORT)
    url = f"http://{lan_address()}:{port}"
    feed = Feed(config, max(args.interval, 30))
    feed.load_saved()

    try:
        server = Server((args.host, port), make_handler(feed))
    except OSError as e:
        if already_running(port):
            write_status(f"Already running.\nOn the tablet, open: {url}")
            sys.exit(f"Already running at {url}")
        write_status(f"Could not start: port {port} is in use ({e}).")
        sys.exit(f"Can't use port {port}: {e}\nTry --port {port + 1}.")
    threading.Thread(target=feed.run, daemon=True).start()
    write_status(f"Running.\nOn the tablet, open: {url}")
    print(f"Tablet display is running.\n  On the tablet, open:  {url}\n  Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop.set()
        server.server_close()


def configured_port(config: Optional[dict] = None) -> int:
    try:
        return int((config or {}).get("tablet_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def tablet_status(port: int) -> dict:
    """{'running': bool, 'ip': this computer's address on the network, 'url': what to type on the tablet}."""
    ip = lan_address()
    return {"running": already_running(port), "ip": ip, "url": f"http://{ip}:{port}", "port": port}


def background_command(port: int) -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tablet", "--port", str(port)]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else sys.executable), str(Path(__file__).resolve()), "--port", str(port)]


def start_background(port: int) -> bool:
    """Start the server with no window and wait for it to answer. True if it is running afterwards."""
    if already_running(port):
        return True
    try:
        STATUS_PATH.unlink()
    except OSError:
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(background_command(port), creationflags=flags, close_fds=True, cwd=str(core.BASE_DIR))
    for _ in range(40):
        if already_running(port):
            return True
        time.sleep(0.25)
    return False


def stop_processes(port: Optional[int] = None) -> bool:
    """Stop the tablet server (only the one on `port`, if given). True if something was stopped."""
    only = ""
    if port:
        only = (r" -and ($_.CommandLine -match '--port\s+" + str(int(port)) + r"(\s|$)' -or $_.CommandLine -notmatch '--port')")
    script = ("$p = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne " + str(os.getpid()) +
              r" -and $_.Name -match '^(ScheduleManager|pythonw?)\.exe$' "
              r"-and $_.CommandLine -match '(--tablet(\s|$)|tablet_server\.py)'" + only + " }; "
              "if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; 'stopped' } else { 'none' }")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "stopped" in result.stdout


def _message(text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "Tablet display", 0x40)
    except (AttributeError, OSError):
        print(text)


def _saved_port() -> int:
    try:
        return configured_port(core.load_config())
    except SystemExit:
        return DEFAULT_PORT


def launch_background(quiet: bool = False) -> None:
    """The Start menu shortcut: start the server, then say where to point the tablet."""
    port = _saved_port()
    running = start_background(port)
    if quiet:
        return
    if running:
        _message(f"Running.\nOn the tablet, open: http://{lan_address()}:{port}\n\nIt keeps running in the background. "
                 "Use \"Stop Tablet Display\" to turn it off.")
    else:
        _message("The tablet display did not start. Details may be in tablet.log.")


def stop_background() -> None:
    _message("Tablet display stopped." if stop_processes() else "It was not running.")


if __name__ == "__main__":
    main()
