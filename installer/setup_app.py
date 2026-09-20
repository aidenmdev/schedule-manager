"""The window that installs Schedule Manager. Built into 'Schedule Manager Setup.exe' by build.py."""
from __future__ import annotations

import argparse
import ctypes
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install_lib as lib


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_payload(explicit: str = "") -> Path:
    path = Path(explicit) if explicit else bundle_dir() / "payload.zip"
    if not path.exists():
        raise lib.InstallError(f"The installer package ({path.name}) is missing. Rebuild or re-copy the setup file.")
    return path


def read_version() -> str:
    try:
        return (bundle_dir() / "payload_version.txt").read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def make_options(args) -> dict:
    return {"desktop": not args.no_desktop, "start_menu": not args.no_start_menu, "tablet_autostart": args.tablet_autostart}


def close_running(dest: Path, ask: bool) -> bool:
    """Make sure the copy being replaced isn't running. False if the person chose to stop."""
    running = lib.running_processes(dest) if dest.exists() else []
    if not running:
        return True
    if ask and not messagebox.askokcancel(lib.APP_NAME, "Schedule Manager is running. Close it and continue?"):
        return False
    lib.stop_processes([pid for pid, _ in running])
    return True


def silent_install(args) -> int:
    dest = Path(args.dir) if args.dir else (lib.installed_location() or lib.default_install_dir())
    try:
        close_running(dest, ask=False)
        exe = lib.install(find_payload(args.payload), dest, version=read_version(), **make_options(args))
    except (lib.InstallError, OSError) as e:
        print(f"Install failed: {e}")
        return 1
    if not args.no_launch:
        subprocess.Popen([str(exe)], cwd=str(dest), close_fds=True)
    return 0


class SetupWindow(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.existing = lib.installed_location()
        self.title(f"{lib.APP_NAME} Setup")
        try:
            self.iconbitmap(str(bundle_dir() / "app.ico"))
        except tk.TclError:
            pass
        self.resizable(False, False)
        self.configure(padx=26, pady=22)
        self.jobs: queue.Queue = queue.Queue()

        ttk.Label(self, text=f"{'Update' if self.existing else 'Install'} {lib.APP_NAME}", font=("Segoe UI", 17, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self, wraplength=610, justify="left", foreground="#555555",
                  text="Shows your work and school schedule from Google Calendar, imports new schedules from your email, "
                       "and keeps your settings in sync between computers. Installing needs no administrator rights."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 14))

        ttk.Label(self, text="Install to").grid(row=2, column=0, sticky="w")
        self.folder = tk.StringVar(value=args.dir or str(self.existing or lib.default_install_dir()))
        row = ttk.Frame(self)
        row.grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 12))
        ttk.Entry(row, textvariable=self.folder, width=54).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left", padx=(8, 0))

        self.desktop = tk.BooleanVar(value=not args.no_desktop)
        self.start_menu = tk.BooleanVar(value=not args.no_start_menu)
        self.autostart = tk.BooleanVar(value=args.tablet_autostart)
        self.launch = tk.BooleanVar(value=not args.no_launch)
        for r, (text, var) in enumerate([("Desktop shortcut", self.desktop), ("Start menu shortcuts", self.start_menu),
                                         ("Start the tablet display when I sign in to Windows", self.autostart)], start=4):
            ttk.Checkbutton(self, text=text, variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=1)

        self.bar = ttk.Progressbar(self, length=610, mode="determinate")
        self.bar.grid(row=7, column=0, columnspan=2, pady=(18, 4), sticky="we")
        self.status = ttk.Label(self, text=f"Version {read_version()}", foreground="#555555")
        self.status.grid(row=8, column=0, columnspan=2, sticky="w")

        self.buttons = ttk.Frame(self)
        self.buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(16, 0))
        self.launch_box = ttk.Checkbutton(self.buttons, text="Open Schedule Manager when finished", variable=self.launch)
        self.launch_box.pack(side="left", padx=(0, 16))
        self.go = ttk.Button(self.buttons, text="Update" if self.existing else "Install", command=self.start)
        self.go.pack(side="left")
        self.cancel = ttk.Button(self.buttons, text="Cancel", command=self.destroy)
        self.cancel.pack(side="left", padx=(8, 0))
        self.after(100, self.poll)

    def browse(self):
        chosen = filedialog.askdirectory(initialdir=self.folder.get() or None, title="Choose the install folder")
        if chosen:
            path = Path(chosen)
            if path.name.lower() != lib.APP_NAME.lower() and path.exists() and any(path.iterdir()):
                path = path / lib.APP_NAME
            self.folder.set(str(path).replace("/", "\\"))

    def start(self):
        dest = Path(self.folder.get().strip())
        if not str(dest).strip():
            return
        try:
            payload = find_payload()
        except lib.InstallError as e:
            messagebox.showerror(lib.APP_NAME, str(e))
            return
        if not close_running(dest, ask=True):
            return
        self.go.state(["disabled"])
        self.cancel.state(["disabled"])
        self.status.configure(text="Installing...")
        options = {"desktop": self.desktop.get(), "start_menu": self.start_menu.get(), "tablet_autostart": self.autostart.get()}

        def work():
            try:
                exe = lib.install(payload, dest, version=read_version(),
                                  progress=lambda i, n: self.jobs.put(("progress", i, n)), **options)
                self.jobs.put(("done", exe))
            except (lib.InstallError, OSError) as e:
                self.jobs.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, *rest = self.jobs.get_nowait()
                if kind == "progress":
                    self.bar.configure(maximum=rest[1], value=rest[0])
                elif kind == "done":
                    self.finish(rest[0])
                elif kind == "error":
                    self.go.state(["!disabled"])
                    self.cancel.state(["!disabled"])
                    self.status.configure(text="Something went wrong.")
                    messagebox.showerror(lib.APP_NAME, rest[0])
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def finish(self, exe: Path):
        self.status.configure(text="Done. Schedule Manager is installed.")
        if self.launch.get():
            subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
        messagebox.showinfo(lib.APP_NAME, "Schedule Manager is installed.\n\nOpen it and sign in to Google the first time. "
                                          "Your settings and history load from your Google account.")
        self.destroy()


def parse(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Install Schedule Manager.", prefix_chars="-/")
    p.add_argument("--silent", "/S", "/s", action="store_true", help="install without any windows")
    p.add_argument("--dir", default="", help="install folder")
    p.add_argument("--payload", default="", help="package to install (default: the one built into this file)")
    p.add_argument("--no-desktop", action="store_true")
    p.add_argument("--no-start-menu", action="store_true")
    p.add_argument("--tablet-autostart", action="store_true")
    p.add_argument("--no-launch", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    if args.silent:
        return silent_install(args)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    SetupWindow(args).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
