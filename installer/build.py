r"""Builds the program, the installer ('Schedule Manager Setup.exe') and update packages. Run with the build
environment's Python (build_installer.bat and publish_update.bat do that for you):
    .buildvenv\Scripts\python.exe installer\build.py          make a new installer (and a new update baseline)
    .buildvenv\Scripts\python.exe installer\publish.py        build an update and email it to yourself"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"
RELEASE = ROOT / "release"  # signing key, the installer baseline and publishing history; never committed
SEP = ";"  # PyInstaller's --add-data separator on Windows
KEEP_DISCOVERY = ("calendar.v3.json", "gmail.v1.json")  # the only Google APIs the app talks to


def run(cmd: list) -> None:
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)


def app_version() -> str:
    text = (ROOT / "dominos_schedule.py").read_text(encoding="utf-8")
    return re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text).group(1)


def repo_name() -> str:
    """The GitHub project updates are published to and read from (owner/name)."""
    saved = RELEASE / "repo.txt"
    return saved.read_text(encoding="utf-8").strip() if saved.exists() else updater.DEFAULT_REPO


def new_build_number() -> int:
    return int(datetime.now().strftime("%Y%m%d%H%M%S"))


def add_data(src: Path, dest: str = ".") -> list:
    return ["--add-data", f"{src}{SEP}{dest}"]


def ensure_keys() -> Path:
    """The signing key pair lives in release/. Losing the private key means installing a fresh setup file everywhere."""
    private, public = RELEASE / "update_signing_key.pem", RELEASE / updater.KEY_NAME
    if not (private.exists() and public.exists()):
        updater.generate_keys(RELEASE)
        print(f"Made a new update signing key in {RELEASE}. Back that folder up somewhere safe.")
    return public


def build_app(info: "updater.BuildInfo") -> Path:
    import googleapiclient
    docs = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
    public = ensure_keys()
    for folder in (DIST / "app", BUILD / "app"):
        shutil.rmtree(folder, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    info_file = BUILD / updater.BUILD_INFO_NAME
    info_file.write_text(json.dumps({"version": info.version, "build": info.build, "base": info.base, "repo": info.repo}), encoding="utf-8")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "ScheduleManager",
           "--icon", ROOT / "app.ico", "--distpath", DIST / "app", "--workpath", BUILD / "app", "--specpath", BUILD,
           "--paths", ROOT, "--collect-data", "customtkinter", "--collect-data", "tzdata", "--collect-data", "httplib2",
           "--hidden-import", "tablet_server", "--hidden-import", "install_lib", "--hidden-import", "updater",
           *add_data(ROOT / "config.example.json"), *add_data(ROOT / "app.ico"), *add_data(ROOT / "app_icon.png"),
           *add_data(info_file), *add_data(public)]
    for name in KEEP_DISCOVERY:
        cmd += add_data(docs / name, "googleapiclient/discovery_cache/documents")
    credentials = ROOT / "credentials.json"
    if credentials.exists():
        cmd += add_data(credentials)
        print("Including credentials.json so a new computer only has to sign in.")
    else:
        print("No credentials.json next to the app, so new computers will need their own copy.")
    run(cmd + [ROOT / "schedule_gui.py"])
    out = DIST / "app" / "ScheduleManager"
    for extra in (out / "_internal" / "googleapiclient" / "discovery_cache" / "documents").glob("*.json"):
        if extra.name not in KEEP_DISCOVERY:
            extra.unlink()
    return out


def make_payload(app_dir: Path) -> Path:
    BUILD.mkdir(exist_ok=True)
    payload = BUILD / "payload.zip"
    if payload.exists():
        payload.unlink()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(app_dir).as_posix())
    (BUILD / "payload_version.txt").write_text(app_version(), encoding="utf-8")
    return payload


def build_setup(payload: Path) -> Path:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
         "--name", "Schedule Manager Setup", "--icon", ROOT / "app.ico", "--distpath", DIST, "--workpath", BUILD / "setup",
         "--specpath", BUILD, "--paths", ROOT, *add_data(payload), *add_data(BUILD / "payload_version.txt"),
         *add_data(ROOT / "app.ico"), ROOT / "installer" / "setup_app.py"])
    return DIST / "Schedule Manager Setup.exe"


def main() -> None:
    for folder in (DIST, BUILD):
        shutil.rmtree(folder, ignore_errors=True)
    build = new_build_number()
    info = updater.BuildInfo(app_version(), build, base=build, repo=repo_name())  # a new installer starts a new update line
    app_dir = build_app(info)
    RELEASE.mkdir(exist_ok=True)
    (RELEASE / "baseline.json").write_text(
        json.dumps({"base": build, "version": info.version, "files": updater.tree_manifest(app_dir)}), encoding="utf-8")
    (RELEASE / "state.json").write_text(json.dumps({"ever_changed": [], "published": []}), encoding="utf-8")
    payload = make_payload(app_dir)
    exe = build_setup(payload)
    print(f"\nBuild {build}")
    print(f"Program folder: {sum(f.stat().st_size for f in app_dir.rglob('*') if f.is_file()) / 1e6:.0f} MB")
    print(f"Package:        {payload.stat().st_size / 1e6:.0f} MB")
    print(f"Installer:      {exe}  ({exe.stat().st_size / 1e6:.0f} MB)")
    print("Devices installed from this file can receive updates made with publish_update.bat.")


if __name__ == "__main__":
    main()
