"""Malware / safety checks. Every file must pass before it can enter the library.

Two layers:
  1. Built-in structural checks (always available, no dependencies):
       * real file signature (magic bytes) must be the audio format its extension claims
       * no executable / script / archive / installer signatures at the start of the file
       * no embedded Windows executable (DOS stub string) anywhere in the file
       * no disguised names: double extensions (track.mp3.exe), Unicode right-to-left override,
         control characters
       * must be a regular file (no symlinks, bundles or devices)
  2. ClamAV signature scan (clamdscan if the daemon runs, else clamscan). ClamAV is free and open source
     (brew install clamav). Its signature database age is reported; stale databases are warned about.

Policy (enforced in library.approve): a file is only approvable when the built-in checks pass AND ClamAV
reported it clean. If ClamAV is not installed the file is "unscanned" and cannot be approved.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

EXECUTABLE_EXTS = {
    "app", "exe", "com", "scr", "bat", "cmd", "msi", "dll", "pif", "vbs", "vbe", "js", "jse", "wsf", "wsh", "ps1",
    "jar", "sh", "zsh", "bash", "command", "py", "pl", "rb", "php", "dmg", "pkg", "mpkg", "iso", "img", "zip", "rar",
    "7z", "gz", "tgz", "bz2", "xz", "lnk", "url", "workflow", "scpt", "applescript", "terminal", "html", "htm",
    "hta", "svg", "pdf", "docm", "xlsm", "apk", "deb", "rpm", "bin", "run", "elf", "dylib", "so", "kext", "plugin",
}

# (magic bytes at offset 0, description)
DANGEROUS_MAGIC = [
    (b"MZ", "Windows executable (PE/MZ header)"),
    (b"\x7fELF", "Linux executable (ELF)"),
    (b"\xfe\xed\xfa\xce", "macOS executable (Mach-O)"),
    (b"\xfe\xed\xfa\xcf", "macOS executable (Mach-O 64)"),
    (b"\xce\xfa\xed\xfe", "macOS executable (Mach-O)"),
    (b"\xcf\xfa\xed\xfe", "macOS executable (Mach-O 64)"),
    (b"\xca\xfe\xba\xbe", "macOS universal binary or Java class"),
    (b"#!", "script (shebang)"),
    (b"PK\x03\x04", "ZIP archive (may contain an app or script)"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"xar!", "macOS installer package (xar/pkg)"),
    (b"%PDF", "PDF document"),
    (b"{\\rtf", "RTF document"),
    (b"\xd0\xcf\x11\xe0", "Microsoft Office/OLE document"),
]
TEXTY_MAGIC = [b"<?xml", b"<html", b"<!doc", b"<script", b"<svg"]

# Container signatures acceptable for each extension
AUDIO_MAGIC = {
    "flac": [(0, b"fLaC"), (0, b"ID3")],            # FLAC (occasionally with a stray ID3 header)
    "mp3": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xfa"), (0, b"\xff\xf3"), (0, b"\xff\xf2"), (0, b"\xff\xe3"),
            (0, b"\xff\xe2")],
    "wav": [(0, b"RIFF"), (0, b"RF64"), (0, b"BW64")],
    "aiff": [(0, b"FORM")], "aif": [(0, b"FORM")],
    "m4a": [(4, b"ftyp")], "aac": [(0, b"\xff\xf1"), (0, b"\xff\xf9"), (0, b"ADIF"), (0, b"ID3")], "alac": [(4, b"ftyp")],
    "ogg": [(0, b"OggS")], "opus": [(0, b"OggS")], "wma": [(0, b"\x30\x26\xb2\x75")],
}
DOS_STUB = b"This program cannot be run in DOS mode"
BIDI_CONTROL = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩", "‎",
                "‏"}

SIG_DB_STALE_DAYS = 7


# --------------------------------------------------------------------------- names (also for search results)
def name_risks(path_or_name: str) -> list[str]:
    """Problems detectable from a file name alone (used on Soulseek results before any download)."""
    name = path_or_name.replace("\\", "/").rsplit("/", 1)[-1]
    risks = []
    if any(ch in BIDI_CONTROL for ch in name):
        risks.append("hidden right-to-left/bidi control character in file name (used to disguise extensions)")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        risks.append("control characters in file name")
    parts = name.lower().split(".")
    if len(parts) >= 2 and parts[-1] in EXECUTABLE_EXTS:
        risks.append(f"not an audio file: .{parts[-1]}")
    # Only the final extension decides how macOS opens a file, so 'Track - www.site.com.mp3' is harmless while
    # 'Track.mp3.app' / 'Track.mp3.exe' is the classic disguise (caught above and below).
    if re.search(r"\.(mp3|flac|wav|aiff?|m4a)\s*\.\w{2,4}$", name.lower()) and parts[-1] not in AUDIO_MAGIC:
        risks.append(f"audio-looking name with real extension .{parts[-1]}")
    return risks


# --------------------------------------------------------------------------- ClamAV
@dataclass
class EngineInfo:
    available: bool
    engine: str = ""
    version: str = ""
    db_version: str = ""
    db_date: str = ""
    db_age_days: float | None = None
    daemon: bool = False
    message: str = ""


_ENGINE_CACHE: tuple[float, EngineInfo] | None = None


def _which(name: str) -> str | None:
    return shutil.which(name) or next((p for p in (f"/opt/homebrew/bin/{name}", f"/opt/homebrew/sbin/{name}",
                                                   f"/usr/local/bin/{name}", f"/usr/local/sbin/{name}")
                                       if Path(p).exists()), None)


def engine_info(refresh: bool = False) -> EngineInfo:
    global _ENGINE_CACHE
    if _ENGINE_CACHE and not refresh and time.time() - _ENGINE_CACHE[0] < 300:
        return _ENGINE_CACHE[1]
    clamscan = _which("clamscan")
    if not clamscan:
        info = EngineInfo(False, message="ClamAV not installed — files cannot be approved until it is "
                                         "(brew install clamav, then run freshclam)")
    else:
        info = EngineInfo(True, engine=clamscan)
        try:
            out = subprocess.run([clamscan, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
            # e.g. "ClamAV 1.5.4/27801/Tue Nov 17 08:24:02 2026"
            parts = out.split("/")
            info.version = parts[0].replace("ClamAV", "").strip()
            if len(parts) >= 3:
                info.db_version = parts[1]
                info.db_date = parts[2]
                try:
                    dt = datetime.strptime(parts[2].strip(), "%a %b %d %H:%M:%S %Y")
                    info.db_age_days = (datetime.now() - dt).total_seconds() / 86400
                except ValueError:
                    pass
        except Exception as e:
            info = EngineInfo(False, message=f"ClamAV found but not working: {e}")
        if info.available:
            cd = _which("clamdscan")
            if cd and subprocess.run([cd, "--ping", "1"], capture_output=True, timeout=10).returncode == 0:
                info.daemon, info.engine = True, cd
            if not info.db_version:
                info.available = False
                info.message = "ClamAV installed but has no virus database yet — run: freshclam"
            elif info.db_age_days is not None and info.db_age_days > SIG_DB_STALE_DAYS:
                info.message = f"ClamAV {info.version}, signatures {info.db_age_days:.0f} days old — run freshclam"
            else:
                info.message = f"ClamAV {info.version}, signatures {info.db_version} ({info.db_date.strip()})"
    _ENGINE_CACHE = (time.time(), info)
    return info


def clamav_scan(path: Path, info: EngineInfo) -> tuple[str, str]:
    """Returns (status, detail): clean | infected | error."""
    args = [info.engine, "--no-summary"]
    if info.daemon:
        args += ["--fdpass"]
    else:
        args += ["--stdout"]
    try:
        p = subprocess.run(args + [str(path)], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "error", "ClamAV scan timed out"
    out = (p.stdout + p.stderr).strip()
    if p.returncode == 0:
        return "clean", f"ClamAV {info.version} db {info.db_version}: OK"
    if p.returncode == 1:
        m = re.search(r":\s*(.+?)\s+FOUND", out)
        return "infected", f"ClamAV: {m.group(1) if m else out[-200:]}"
    return "error", f"ClamAV error: {out[-300:]}"


# --------------------------------------------------------------------------- file scan
@dataclass
class ScanResult:
    status: str = "clean"               # clean | infected | suspicious | unscanned | error
    threats: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    engine: str = ""

    @property
    def safe(self) -> bool:
        return self.status == "clean"


def structural_check(path: Path) -> list[str]:
    threats: list[str] = []
    if path.is_symlink():
        threats.append("file is a symbolic link")
        return threats
    if not path.is_file():
        threats.append("not a regular file (folder, bundle or device)")
        return threats
    threats += name_risks(path.name)
    ext = path.suffix.lower().lstrip(".")
    with open(path, "rb") as fh:
        head = fh.read(64)
    for magic, desc in DANGEROUS_MAGIC:
        if head.startswith(magic):
            threats.append(f"content is a {desc}, not audio")
            break
    low = head.lstrip().lower()
    if any(low.startswith(m) for m in TEXTY_MAGIC):
        threats.append("content is markup/script text, not audio")
    expected = AUDIO_MAGIC.get(ext)
    if expected is not None and not any(head[off:off + len(sig)] == sig for off, sig in expected):
        threats.append(f"file signature does not match .{ext} (starts with {head[:8].hex(' ')})")
    # Embedded Windows executable anywhere in the file (streamed, overlapping chunks)
    with open(path, "rb") as fh:
        prev = b""
        while chunk := fh.read(4 << 20):
            if DOS_STUB in prev[-len(DOS_STUB):] + chunk:
                threats.append("embedded Windows executable found inside the file")
                break
            prev = chunk
    return threats


def scan_file(path: Path | str) -> ScanResult:
    path = Path(path)
    r = ScanResult()
    try:
        r.threats = structural_check(path)
    except OSError as e:
        return ScanResult("error", [f"cannot read file: {e}"])
    info = engine_info()
    if info.available:
        status, detail = clamav_scan(path, info)
        r.engine = detail
        if status == "infected":
            r.threats.insert(0, detail)
            r.status = "infected"
        elif status == "error":
            r.notes.append(detail)
            r.status = "error"
        if info.db_age_days is not None and info.db_age_days > SIG_DB_STALE_DAYS:
            r.notes.append(f"virus signatures are {info.db_age_days:.0f} days old — run freshclam")
    else:
        r.status = "unscanned"
        r.notes.append(info.message)
    if r.threats and r.status != "infected":
        r.status = "suspicious"
    log.info("security scan %s: %s %s %s", path.name, r.status, r.threats, r.notes)
    return r


# --------------------------------------------------------------------------- daily signature update
# Before the first search / download / folder scan of each calendar day, the virus definitions are refreshed
# with freshclam. Later actions that day skip it. A failed update (e.g. offline) is reported but does not block:
# scanning still runs with the existing signatures, and the Dashboard shows their age.
import json as _json
import threading as _threading
from datetime import date as _date

_UPDATE_LOCK = _threading.Lock()


def _state_path() -> Path:
    from .config import APP_DIR
    return APP_DIR / "av_update.json"


def _read_state() -> dict:
    try:
        return _json.loads(_state_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def signatures_updated_today() -> bool:
    return _read_state().get("last_success_date") == _date.today().isoformat()


@dataclass
class UpdateResult:
    ran: bool          # False = already updated today, nothing done
    ok: bool
    message: str


def update_signatures_if_new_day(force: bool = False) -> UpdateResult:
    """Run freshclam once per calendar day, before tracks are fetched. Thread-safe; blocks while running."""
    with _UPDATE_LOCK:
        if not force and signatures_updated_today():
            return UpdateResult(False, True, "Virus definitions already updated today")
        fc = _which("freshclam")
        if not fc:
            return UpdateResult(False, False, "freshclam not found — ClamAV is not installed (brew install clamav)")
        log.info("Daily virus-definition update: running freshclam")
        t0 = time.time()
        try:
            p = subprocess.run([fc, "--stdout"], capture_output=True, text=True, timeout=900)
            ok, out = p.returncode == 0, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            ok, out = False, "freshclam timed out after 15 minutes"
        except OSError as e:
            ok, out = False, str(e)
        state = _read_state()
        state["last_attempt"] = datetime.now().isoformat(timespec="seconds")
        if ok:
            state["last_success_date"] = _date.today().isoformat()
        try:
            _state_path().parent.mkdir(parents=True, exist_ok=True)
            _state_path().write_text(_json.dumps(state), "utf-8")
        except OSError:
            pass
        info = engine_info(refresh=True)
        if ok:
            msg = f"Virus definitions updated ({time.time() - t0:.0f}s): {info.message}"
            log.info(msg)
        else:
            tail = " ".join(l for l in out.strip().splitlines()[-3:])
            msg = f"Virus-definition update failed — scanning with existing signatures. {tail[-300:]}"
            log.warning(msg)
        return UpdateResult(True, ok, msg)
