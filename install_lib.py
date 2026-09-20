"""Installing and removing the packaged app. Per-user, so no administrator rights are needed."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "Schedule Manager"
EXE_NAME = "ScheduleManager.exe"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ScheduleManager"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FOLDER_IDS = {  # Windows "known folders": these follow OneDrive redirection and renamed profiles
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "programs": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",
    "startup": "{B97D20BB-F46A-4C97-BA10-5E3608430854}",
}


class InstallError(Exception):
    """Something the person can act on; the message is meant to be shown."""


@dataclass
class Folders:
    desktop: Path
    programs: Path   # the Start menu's Programs folder
    startup: Path


def _known_folder(name: str) -> Path:
    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    raw = uuid.UUID(FOLDER_IDS[name])
    guid = GUID(raw.time_low, raw.time_mid, raw.time_hi_version, (ctypes.c_ubyte * 8)(*raw.bytes[8:]))
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
        raise InstallError(f"Windows would not say where your {name} folder is.")
    path = Path(out.value)
    ctypes.windll.ole32.CoTaskMemFree(out)
    return path


def desktop_dir() -> Path:
    return _known_folder("desktop")


def system_folders() -> Folders:
    return Folders(_known_folder("desktop"), _known_folder("programs"), _known_folder("startup"))


def default_install_dir(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("LOCALAPPDATA") or Path.home()) / "Programs" / APP_NAME


def data_dir(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("SCHEDULE_MANAGER_DATA") or (Path(env.get("LOCALAPPDATA") or Path.home()) / APP_NAME))


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _powershell(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, creationflags=NO_WINDOW, timeout=timeout)


def create_shortcut(lnk: Path, target: str, args: str = "", workdir: str = "", icon: str = "", description: str = "") -> None:
    lnk = Path(lnk)
    lnk.parent.mkdir(parents=True, exist_ok=True)
    lines = ["$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + ")",
             "$s.TargetPath = " + _ps_quote(target)]
    if args:
        lines.append("$s.Arguments = " + _ps_quote(args))
    if workdir:
        lines.append("$s.WorkingDirectory = " + _ps_quote(workdir))
    if icon:
        lines.append("$s.IconLocation = " + _ps_quote(icon + ",0"))
    if description:
        lines.append("$s.Description = " + _ps_quote(description))
    lines.append("$s.Save()")
    result = _powershell("; ".join(lines))
    if result.returncode != 0 or not lnk.exists():
        raise InstallError(f"Could not create the shortcut {lnk.name}: {result.stderr.strip() or 'unknown error'}")


def running_processes(install_dir: Path, exclude_pid: Optional[int] = None) -> list:
    """Processes started from `install_dir` (the app or the tablet display), as (pid, name)."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "
              + _ps_quote(str(install_dir).rstrip("\\") + "\\*") + " } | ForEach-Object { \"$($_.ProcessId)|$($_.Name)\" }")
    try:
        out = _powershell(script).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        pid, _, name = line.partition("|")
        if pid.strip().isdigit() and int(pid) != exclude_pid:
            found.append((int(pid), name.strip()))
    return found


def stop_processes(pids: list) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)


def _open_key(reg_key: str, write: bool):
    import winreg
    access = winreg.KEY_ALL_ACCESS if write else winreg.KEY_READ
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, access) if write else \
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, access)


def installed_location(reg_key: Optional[str] = None) -> Optional[Path]:
    import winreg
    reg_key = reg_key or REG_KEY
    try:
        with _open_key(reg_key, False) as key:
            return Path(winreg.QueryValueEx(key, "InstallLocation")[0])
    except OSError:
        return None


def _register(dest: Path, version: str, reg_key: str) -> None:
    import winreg
    exe = dest / EXE_NAME
    size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
    values = {
        "DisplayName": (winreg.REG_SZ, APP_NAME), "DisplayVersion": (winreg.REG_SZ, version),
        "InstallLocation": (winreg.REG_SZ, str(dest)), "DisplayIcon": (winreg.REG_SZ, str(exe)),
        "UninstallString": (winreg.REG_SZ, f'"{exe}" --uninstall'), "NoModify": (winreg.REG_DWORD, 1),
        "NoRepair": (winreg.REG_DWORD, 1), "EstimatedSize": (winreg.REG_DWORD, int(size_kb)),
    }
    with _open_key(reg_key, True) as key:
        for name, (kind, value) in values.items():
            winreg.SetValueEx(key, name, 0, kind, value)


def _unregister(reg_key: str) -> None:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_key)
    except OSError:
        pass


START_MENU_FOLDER = APP_NAME
SHORTCUTS = {  # file name -> (extra arguments, description)
    "Schedule Manager.lnk": ("", "Open Schedule Manager"),
    "Tablet Display.lnk": ("--tablet-launch", "Show your week on a tablet over Wi-Fi"),
    "Stop Tablet Display.lnk": ("--tablet-stop", "Turn the tablet display off"),
    "Uninstall Schedule Manager.lnk": ("--uninstall", "Remove Schedule Manager from this computer"),
}
DESKTOP_LINK = "Schedule Manager.lnk"
STARTUP_LINK = "Schedule Manager tablet display.lnk"


def _safe_extract(archive: zipfile.ZipFile, dest: Path, progress: Optional[Callable[[int, int], None]]) -> None:
    root = dest.resolve()
    members = archive.infolist()
    for i, member in enumerate(members, start=1):
        target = (dest / member.filename).resolve()
        if root != target and root not in target.parents:
            raise InstallError("The installer package is damaged (it tried to write outside the install folder).")
        archive.extract(member, dest)
        if progress:
            progress(i, len(members))


def _clear_program_files(dest: Path) -> None:
    for child in dest.iterdir():
        try:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        except OSError as e:
            raise InstallError(f"Could not replace {child.name}. Close Schedule Manager and try again. ({e})") from e


def install(payload: Path, dest: Path, *, version: str, desktop: bool = True, start_menu: bool = True,
            tablet_autostart: bool = False, folders: Optional[Folders] = None, reg_key: Optional[str] = None,
            progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Unpack `payload` into `dest` and set up shortcuts and the Add/Remove Programs entry. Returns the exe path."""
    dest = Path(dest)
    folders = folders or system_folders()
    reg_key = reg_key or REG_KEY
    if dest.exists() and any(dest.iterdir()):
        if not (dest / EXE_NAME).exists():
            raise InstallError(f"{dest} already has other files in it. Pick an empty folder, or a new one.")
        _clear_program_files(dest)   # an update: only program files live here, your data is stored elsewhere
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(payload) as archive:
            _safe_extract(archive, dest, progress)
    except zipfile.BadZipFile as e:
        raise InstallError("The installer package is damaged. Download or copy it again.") from e
    exe = dest / EXE_NAME
    if not exe.exists():
        raise InstallError(f"The package didn't contain {EXE_NAME}.")

    _register(dest, version, reg_key)
    remove_shortcuts(folders)  # so an update never leaves a link behind that was switched off this time
    if desktop:
        create_shortcut(folders.desktop / DESKTOP_LINK, str(exe), workdir=str(dest), icon=str(exe), description=SHORTCUTS[DESKTOP_LINK][1])
    if start_menu:
        for name, (args, description) in SHORTCUTS.items():
            create_shortcut(folders.programs / START_MENU_FOLDER / name, str(exe), args, str(dest), str(exe), description)
    if tablet_autostart:
        create_shortcut(folders.startup / STARTUP_LINK, str(exe), "--tablet-launch --quiet", str(dest), str(exe))
    return exe


def remove_shortcuts(folders: Folders) -> None:
    for link in (folders.desktop / DESKTOP_LINK, folders.startup / STARTUP_LINK):
        try:
            link.unlink()
        except OSError:
            pass
    shutil.rmtree(folders.programs / START_MENU_FOLDER, ignore_errors=True)


def uninstall(dest: Path, *, delete_data: bool = False, folders: Optional[Folders] = None, reg_key: Optional[str] = None,
              data: Optional[Path] = None, defer_delete: bool = False) -> None:
    """Remove the app. Your settings and sign-in stay unless `delete_data`. With `defer_delete`, the install folder
    is removed by a helper after this process exits (a running program can't delete itself)."""
    dest = Path(dest)
    folders = folders or system_folders()
    reg_key = reg_key or REG_KEY
    stop_processes([pid for pid, _ in running_processes(dest, exclude_pid=os.getpid())])
    remove_shortcuts(folders)
    _unregister(reg_key)
    if delete_data:
        shutil.rmtree(data or data_dir(), ignore_errors=True)
    if not dest.exists():
        return
    if defer_delete:
        _remove_after_exit(dest)
    else:
        shutil.rmtree(dest, ignore_errors=True)


def _remove_after_exit(dest: Path) -> None:
    """A helper script deletes the folder a few seconds from now, once this program has exited and let go of its files."""
    import tempfile
    script = Path(tempfile.gettempdir()) / f"schedule-manager-cleanup-{os.getpid()}.bat"
    lines = ["@echo off", "ping -n 4 127.0.0.1 >nul", f'rmdir /s /q "{dest}"',
             f'if exist "{dest}" (ping -n 5 127.0.0.1 >nul & rmdir /s /q "{dest}")', '(goto) 2>nul & del "%~f0"']
    script.write_text("\r\n".join(lines) + "\r\n", encoding="mbcs")
    subprocess.Popen([str(script)], cwd=tempfile.gettempdir(), creationflags=NO_WINDOW, close_fds=True)


def uninstall_ui() -> None:
    """What 'Uninstall Schedule Manager' runs (the installed exe with --uninstall)."""
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    dest = Path(sys.executable).parent
    if not messagebox.askyesno(APP_NAME, "Remove Schedule Manager from this computer?", parent=root):
        return
    delete = messagebox.askyesno(
        APP_NAME, "Also delete your saved settings and Google sign-in on this computer?\n\n"
                  "Choose No to keep them (reinstalling later picks up where you left off). Your calendar and synced "
                  "settings in Google are not affected either way.", default="no", parent=root)
    try:
        uninstall(dest, delete_data=delete, defer_delete=True)
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Could not finish uninstalling: {e}", parent=root)
        return
    messagebox.showinfo(APP_NAME, "Schedule Manager has been removed.", parent=root)
