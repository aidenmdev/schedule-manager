"""Updates for the installed app.

The computer you build on publishes each update to a branch of your GitHub repository (installer/publish.py).
The app on your other computers reads it from there, checks that it was signed by your build computer, and
installs it. Only the signature is trusted, so it doesn't matter who else can read the branch.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import dominos_schedule as core

DEFAULT_REPO = "aidenmdev/schedule-manager"
BRANCH = "updates"
META_NAME = "update.json"
PACKAGE_NAME = "package.bin"
BUILD_INFO_NAME = "build_info.json"
KEY_NAME = "update_key.pub"
NEVER_SHIPPED = frozenset({"credentials.json"})   # stays as installed on each computer
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(Exception):
    """Something about an update that is worth telling the person."""


@dataclass(frozen=True)
class BuildInfo:
    version: str
    build: int
    base: int   # which installer this build descends from; updates only apply within the same base
    repo: str = DEFAULT_REPO


def read_build_info(resource_dir: Optional[Path] = None) -> Optional[BuildInfo]:
    try:
        raw = json.loads((Path(resource_dir or core.RESOURCE_DIR) / BUILD_INFO_NAME).read_text(encoding="utf-8"))
        return BuildInfo(str(raw["version"]), int(raw["build"]), int(raw["base"]), str(raw.get("repo") or DEFAULT_REPO))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_installed() -> bool:
    """True for the installed program, False when running from source."""
    return bool(getattr(sys, "frozen", False)) and read_build_info() is not None


def describe_build(info: BuildInfo) -> str:
    stamp = str(info.build)
    when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}" if len(stamp) >= 12 else stamp
    return f"Version {info.version}, built {when}"


# ---- signing ---------------------------------------------------------------------------------------------

def generate_keys(directory: Path) -> tuple:
    """Make the signing key pair once. The private key stays on the build computer; the public half ships in the app."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private = directory / "update_signing_key.pem"
    public = directory / KEY_NAME
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public.write_text(base64.b64encode(raw).decode("ascii"), encoding="ascii")
    return private, public


def load_private_key(path: Path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def load_public_key(path_or_text):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    text = path_or_text.read_text(encoding="ascii") if isinstance(path_or_text, Path) else path_or_text
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(text.strip()))


def _signed_text(build: int, base: int, sha256: str) -> bytes:
    return f"schedule-manager-update|{build}|{base}|{sha256}".encode("ascii")


def sign(private_key, build: int, base: int, sha256: str) -> str:
    return base64.b64encode(private_key.sign(_signed_text(build, base, sha256))).decode("ascii")


def signature_ok(public_key, build: int, base: int, sha256: str, signature: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    try:
        public_key.verify(base64.b64decode(signature), _signed_text(build, base, sha256))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---- packages --------------------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict:
    """{relative path: sha256} for every file under `root` that updates manage."""
    root = Path(root)
    return {f.relative_to(root).as_posix(): sha256_file(f)
            for f in sorted(root.rglob("*")) if f.is_file() and f.name not in NEVER_SHIPPED}


def build_package(root: Path, baseline: dict, info: BuildInfo, notes: str, ever_changed: set) -> tuple:
    """(zip bytes, manifest, updated set of paths that have ever differed from the installer).
    Only files that differ from the installer are included, so an update is a few MB, not the whole program."""
    files = tree_manifest(root)
    included = {p for p, h in files.items() if baseline.get(p) != h} | (set(ever_changed) & set(files))
    manifest = {"version": info.version, "build": info.build, "base": info.base, "notes": notes,
                "files": files, "included": sorted(included)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        for rel in sorted(included):
            z.write(Path(root) / rel, "files/" + rel)
    return buf.getvalue(), manifest, set(ever_changed) | included


def raw_url(repo: str, name: str, branch: str = BRANCH) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{name}"


def http_get(url: str, progress: Optional[Callable[[int, int], None]] = None, timeout: int = 40) -> bytes:
    """Download a URL. Raises OSError-based errors when offline, which the app treats as 'no connection'."""
    request = urllib.request.Request(url, headers={"User-Agent": "ScheduleManager-updater"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        chunks, done = [], 0
        for block in iter(lambda: response.read(1 << 16), b""):
            chunks.append(block)
            done += len(block)
            if progress and total:
                progress(done, total)
    return b"".join(chunks)


@dataclass
class Update:
    version: str
    build: int
    base: int
    notes: str
    sha256: str
    signature: str
    size: int
    url: str


@dataclass
class UpdateCheck:
    current: BuildInfo
    available: Optional[Update] = None
    needs_new_installer: Optional[Update] = None   # a newer build exists, but it was made from a different installer


def parse_meta(raw: bytes, repo: str, branch: str = BRANCH) -> Update:
    try:
        meta = json.loads(raw.decode("utf-8"))
        return Update(str(meta["version"]), int(meta["build"]), int(meta["base"]), str(meta.get("notes") or ""),
                      str(meta["sha256"]), str(meta["sig"]), int(meta.get("size") or 0),
                      raw_url(repo, str(meta.get("file") or PACKAGE_NAME), branch))
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
        raise UpdateError("The update information on GitHub is unreadable.") from e


def find_updates(current: BuildInfo, fetch: Optional[Callable] = None, branch: str = BRANCH) -> UpdateCheck:
    """Look at the latest published update and say whether it is newer than this build."""
    fetch = fetch or http_get
    try:
        raw = fetch(raw_url(current.repo, META_NAME, branch) + f"?t={int(time.time())}")  # the query skips GitHub's cache
    except OSError as e:
        if getattr(e, "code", None) == 404:   # nothing has been published yet
            return UpdateCheck(current)
        raise
    update = parse_meta(raw, current.repo, branch)
    check = UpdateCheck(current)
    if update.build > current.build:
        if update.base == current.base:
            check.available = update
        else:
            check.needs_new_installer = update
    return check


def download(update: Update, progress: Optional[Callable[[int, int], None]] = None, fetch: Optional[Callable] = None) -> bytes:
    fetch = fetch or http_get
    data = fetch(update.url, progress)
    if update.size and len(data) != update.size:
        raise UpdateError("The download is incomplete. Try again.")
    return data


def publish(package: bytes, manifest: dict, private_key, remote: str, branch: str = BRANCH) -> None:
    """Replace the published update with this one: a single-commit branch holding update.json and package.bin.
    Uses the git already installed and signed in on this computer."""
    sha = hashlib.sha256(package).hexdigest()
    meta = {"version": manifest["version"], "build": manifest["build"], "base": manifest["base"],
            "notes": manifest.get("notes") or "", "sha256": sha, "size": len(package), "file": PACKAGE_NAME,
            "sig": sign(private_key, manifest["build"], manifest["base"], sha)}
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")

    def git(*args, cwd):
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
        if result.returncode != 0:
            raise UpdateError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:400]}")

    with tempfile.TemporaryDirectory() as work:
        Path(work, META_NAME).write_text(json.dumps(meta, indent=1), encoding="utf-8")
        Path(work, PACKAGE_NAME).write_bytes(package)
        git("init", "-q", cwd=work)
        git("checkout", "-q", "--orphan", branch, cwd=work)
        git("add", "-A", cwd=work)
        git("-c", "user.name=Schedule Manager", "-c", "user.email=updates@users.noreply.github.com", "commit", "-q",
            "-m", f"Update {manifest['version']} build {manifest['build']}", cwd=work)
        git("push", "--force", remote, f"{branch}:{branch}", cwd=work)


# ---- checking and staging ---------------------------------------------------------------------------------

@dataclass
class Staged:
    folder: Path            # holds files/ with the new versions
    removed: list           # relative paths that are no longer part of the program
    manifest: dict


def stage(package: bytes, update: Update, current: BuildInfo, public_key, install_dir: Path, stage_dir: Path) -> Staged:
    """Verify an update completely, then unpack it. Nothing about the installed program changes yet."""
    if update.base != current.base:
        raise UpdateError("This update was made for a different installer. Install the latest Schedule Manager Setup instead.")
    if update.build <= current.build:
        raise UpdateError("That update is not newer than the version you have.")
    sha = hashlib.sha256(package).hexdigest()
    if sha != update.sha256:
        raise UpdateError("The download is incomplete or damaged. Try again.")
    if not signature_ok(public_key, update.build, update.base, sha, update.signature):
        raise UpdateError("This update was not signed by your build computer, so it was not installed.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(package))
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise UpdateError("The update package is damaged.") from e
    if (manifest.get("build"), manifest.get("base")) != (update.build, update.base):
        raise UpdateError("The update package doesn't match its message.")

    stage_dir = Path(stage_dir)
    files_dir = stage_dir / "files"
    root = files_dir.resolve()
    files_dir.mkdir(parents=True, exist_ok=True)
    included = set(manifest.get("included", []))
    for rel in included:
        target = (files_dir / rel).resolve()
        if root not in target.parents or Path(rel).name in NEVER_SHIPPED or rel not in manifest["files"]:
            raise UpdateError("The update package is damaged (unexpected file path).")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read("files/" + rel))
        if sha256_file(target) != manifest["files"][rel]:
            raise UpdateError("A file in the update is damaged. Try again.")

    install_dir = Path(install_dir)
    for rel, expected in manifest["files"].items():
        if rel in included:
            continue
        local = install_dir / rel
        if not local.exists() or sha256_file(local) != expected:
            raise UpdateError("Files in this installation don't match what the update expects. "
                              "Install the latest Schedule Manager Setup once, then updates will work again.")
    removed = sorted(rel for rel in tree_manifest(install_dir) if rel not in manifest["files"])
    return Staged(stage_dir, removed, manifest)


# ---- applying ---------------------------------------------------------------------------------------------

def apply_script(staged: Staged, install_dir: Path, work_dir: Path, wait_for_pid: int, exe_name: str = "ScheduleManager.exe",
                 restart: bool = True, restart_tablet: bool = False, failure_note: Optional[Path] = None) -> Path:
    """A batch file that waits for the app to close, swaps the files in (undoing everything if anything fails),
    and starts the app again. A running program can't replace its own files, hence the helper."""
    work_dir = Path(work_dir)
    backup = work_dir / "backup"
    lines = ["@echo off", "setlocal", 'set "SYS=%SystemRoot%\\System32"', f'set "DEST={install_dir}"', f'set "STAGE={staged.folder}"', f'set "BACKUP={backup}"',
             "set tries=0", ":wait",
             f'"%SYS%\\tasklist.exe" /FI "PID eq {wait_for_pid}" 2>nul | "%SYS%\\find.exe" " {wait_for_pid} " >nul',
             "if errorlevel 1 goto gone", "set /a tries=tries+1", "if %tries% GEQ 60 goto gone", '"%SYS%\\ping.exe" -n 2 127.0.0.1 >nul',
             "goto wait", ":gone", f'"%SYS%\\taskkill.exe" /F /IM "{exe_name}" >nul 2>&1', '"%SYS%\\ping.exe" -n 2 127.0.0.1 >nul',
             'if exist "%BACKUP%" rmdir /s /q "%BACKUP%"',
             '"%SYS%\\robocopy.exe" "%DEST%" "%BACKUP%" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul', "if errorlevel 8 goto failed",
             '"%SYS%\\robocopy.exe" "%STAGE%\\files" "%DEST%" /E /IS /IT /NFL /NDL /NJH /NJS /NP /R:5 /W:2 >nul',
             "if errorlevel 8 goto rollback"]
    for rel in staged.removed:
        lines.append('del /f /q "%DEST%\\' + rel.replace("/", "\\") + '" >nul 2>&1')
    lines += ["goto finish", ":rollback",
              '"%SYS%\\robocopy.exe" "%BACKUP%" "%DEST%" /MIR /IS /IT /NFL /NDL /NJH /NJS /NP /R:5 /W:2 >nul', ":failed"]
    if failure_note:
        lines.append(f'echo The last update could not be installed and was undone. > "{failure_note}"')
    lines += [":finish", '"%SYS%\\ping.exe" -n 2 127.0.0.1 >nul']
    if restart:
        lines.append(f'start "" "%DEST%\\{exe_name}"')
        if restart_tablet:
            lines.append(f'start "" "%DEST%\\{exe_name}" --tablet-launch --quiet')
    lines += ['if exist "%BACKUP%" rmdir /s /q "%BACKUP%"', 'rmdir /s /q "%STAGE%" >nul 2>&1', '(goto) 2>nul & del "%~f0"']
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "apply-update.bat"
    script.write_bytes(("\r\n".join(lines) + "\r\n").encode("mbcs", errors="replace"))
    return script


def launch(script: Path) -> None:
    subprocess.Popen([str(script)], cwd=tempfile.gettempdir(), creationflags=NO_WINDOW, close_fds=True)
