"""Updates for the installed app.

The computer you build on emails each update to your own Gmail (installer/publish.py). The app on your
other computers finds that message, checks that it was signed by your build computer, and installs it.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional

import dominos_schedule as core

SUBJECT_TAG = "[Schedule Manager update]"
PART_BYTES = 9 * 1024 * 1024   # each email carries at most this much, before encoding
BUILD_INFO_NAME = "build_info.json"
KEY_NAME = "update_key.pub"
NEVER_SHIPPED = frozenset({"credentials.json"})   # stays as installed on each computer
MACHINE_LINE = re.compile(r"SM-UPDATE (\{.*\})")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(Exception):
    """Something about an update that is worth telling the person."""


@dataclass(frozen=True)
class BuildInfo:
    version: str
    build: int
    base: int   # which installer this build descends from; updates only apply within the same base


def read_build_info(resource_dir: Optional[Path] = None) -> Optional[BuildInfo]:
    try:
        raw = json.loads((Path(resource_dir or core.RESOURCE_DIR) / BUILD_INFO_NAME).read_text(encoding="utf-8"))
        return BuildInfo(str(raw["version"]), int(raw["build"]), int(raw["base"]))
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


def split_parts(data: bytes, size: int = 0) -> list:
    size = size or PART_BYTES
    return [data[i:i + size] for i in range(0, len(data), size)] or [b""]


def _wrapped_b64(data: bytes) -> str:
    text = base64.b64encode(data).decode("ascii")
    return "\n".join(text[i:i + 76] for i in range(0, len(text), 76)) + "\n"


def build_messages(package: bytes, manifest: dict, signature: str, to_addr: str) -> list:
    """The emails that carry an update, as raw MIME bytes. The package is text so mail scanners leave it alone."""
    sha = hashlib.sha256(package).hexdigest()
    parts = split_parts(package)
    messages = []
    for index, chunk in enumerate(parts, start=1):
        meta = {"version": manifest["version"], "build": manifest["build"], "base": manifest["base"], "sha256": sha,
                "size": len(package), "sig": signature, "part": index, "parts": len(parts)}
        msg = MIMEMultipart()
        msg["To"] = to_addr
        msg["Subject"] = (f"{SUBJECT_TAG} {manifest['version']} build {manifest['build']} "
                          f"(part {index} of {len(parts)})")
        body = (f"Schedule Manager {manifest['version']}\n\n{manifest.get('notes') or 'No notes.'}\n\n---\n"
                "Your other computers use this message to update themselves. You can ignore it or file it away, "
                "but please don't delete it until they have updated.\n\nSM-UPDATE " + json.dumps(meta, separators=(",", ":")) + "\n")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        attachment = MIMEText(_wrapped_b64(chunk), "plain", "us-ascii")
        attachment.add_header("Content-Disposition", "attachment",
                              filename=f"update-{manifest['build']}-part{index}.txt")
        msg.attach(attachment)
        messages.append(msg.as_bytes())
    return messages


def send_mime(gmail, mime: bytes) -> None:
    """Small messages go as JSON; big ones use Gmail's upload endpoint, which allows up to 35 MB."""
    if len(mime) < 2 * 1024 * 1024:
        gmail.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(mime).decode("ascii")}).execute(
            num_retries=core.RETRIES)
        return
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(mime), mimetype="message/rfc822", resumable=False)
    gmail.users().messages().send(userId="me", media_body=media).execute(num_retries=core.RETRIES)


def publish(gmail, package: bytes, manifest: dict, private_key, to_addr: Optional[str] = None,
            progress: Optional[Callable[[int, int], None]] = None) -> int:
    """Email the update to yourself. Returns how many messages were sent."""
    to_addr = to_addr or gmail.users().getProfile(userId="me").execute()["emailAddress"]
    sha = hashlib.sha256(package).hexdigest()
    signature = sign(private_key, manifest["build"], manifest["base"], sha)
    messages = build_messages(package, manifest, signature, to_addr)
    for i, mime in enumerate(messages, start=1):
        send_mime(gmail, mime)
        if progress:
            progress(i, len(messages))
    return len(messages)


# ---- finding and downloading ----------------------------------------------------------------------------

@dataclass
class Update:
    version: str
    build: int
    base: int
    notes: str
    sha256: str
    signature: str
    size: int
    total_parts: int
    parts: dict = field(default_factory=dict)   # part number -> (message id, attachment id)

    @property
    def complete(self) -> bool:
        return sorted(self.parts) == list(range(1, self.total_parts + 1))


@dataclass
class UpdateCheck:
    current: BuildInfo
    available: Optional[Update] = None
    needs_new_installer: Optional[Update] = None   # a newer build exists, but it was made from a different installer


def _walk_parts(payload: dict):
    yield payload
    for child in payload.get("parts", []) or []:
        yield from _walk_parts(child)


def _decode_b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _read_message(message: dict) -> Optional[tuple]:
    """(meta dict, notes, attachment id) for a message that looks like an update part, else None."""
    body_text, attachment_id = "", None
    for part in _walk_parts(message.get("payload", {})):
        body = part.get("body", {}) or {}
        if part.get("filename"):
            attachment_id = body.get("attachmentId")
        elif part.get("mimeType") == "text/plain" and body.get("data"):
            body_text += _decode_b64url(body["data"]).decode("utf-8", errors="replace")
    found = MACHINE_LINE.search(body_text)
    if not found or not attachment_id:
        return None
    try:
        meta = json.loads(found.group(1))
        int(meta["build"]), int(meta["base"]), int(meta["part"]), int(meta["parts"])
        meta["sha256"], meta["sig"], meta["version"]
    except (ValueError, KeyError, TypeError):
        return None
    head = body_text.split("\n\n---\n")[0].split("\n\n", 1)
    return meta, (head[1].strip() if len(head) > 1 else ""), attachment_id


def find_updates(gmail, current: BuildInfo, limit: int = 60) -> UpdateCheck:
    """Look through your own mail for update messages newer than this build."""
    query = f'from:me subject:"{SUBJECT_TAG}" has:attachment'
    ids = []
    token = None
    while len(ids) < limit:
        resp = gmail.users().messages().list(userId="me", q=query, maxResults=min(50, limit), pageToken=token).execute(
            num_retries=core.RETRIES)
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    updates: dict = {}
    for mid in ids[:limit]:
        message = gmail.users().messages().get(userId="me", id=mid, format="full").execute(num_retries=core.RETRIES)
        read = _read_message(message)
        if read is None:
            continue
        meta, notes, attachment_id = read
        key = (int(meta["build"]), int(meta["base"]), meta["sha256"])
        update = updates.setdefault(key, Update(str(meta["version"]), int(meta["build"]), int(meta["base"]), notes,
                                                meta["sha256"], meta["sig"], int(meta.get("size", 0)), int(meta["parts"])))
        update.parts[int(meta["part"])] = (mid, attachment_id)
    check = UpdateCheck(current)
    for update in sorted(updates.values(), key=lambda u: u.build, reverse=True):
        if update.build <= current.build or not update.complete:
            continue
        if update.base == current.base:
            check.available = update
            break
        if check.needs_new_installer is None:
            check.needs_new_installer = update
    return check


def download(gmail, update: Update, progress: Optional[Callable[[int, int], None]] = None) -> bytes:
    chunks = []
    for number in sorted(update.parts):
        message_id, attachment_id = update.parts[number]
        att = gmail.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id).execute(
            num_retries=core.RETRIES)
        text = _decode_b64url(att["data"]).decode("ascii", errors="replace")
        try:
            chunks.append(base64.b64decode("".join(text.split()), validate=True))
        except ValueError as e:
            raise UpdateError("The update message is damaged. Ask the build computer to publish it again.") from e
        if progress:
            progress(number, update.total_parts)
    return b"".join(chunks)


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
