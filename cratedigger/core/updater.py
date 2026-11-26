"""Self-update from GitHub Releases.

How it works:
  1. check_latest() asks GitHub for the newest published release of REPO (drafts and pre-releases are ignored).
     A release's tag must look like v1.2.3; it is newer if that number is higher than __version__.
  2. install() downloads that release's source code, checks it really is that version, and replaces the code
     inside the installed CrateDigger.app (Contents/Resources/app). The app's private Python environment is kept;
     if requirements.txt changed, the new dependencies are installed into it first. If any step fails the old code
     stays in place.
  3. The caller restarts the app (relaunch_command()).

Your data (database, settings, library, downloads) lives outside the app and is never touched by an update.
When CrateDigger runs from the project folder instead of the .app, it can only tell you an update exists.
"""
from __future__ import annotations

import io
import json
import logging
import os
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .. import __version__

log = logging.getLogger(__name__)

REPO = "vmihai2009-jpg/CrateDigger"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
MAX_DOWNLOAD = 50 * 1024 * 1024          # the source zip is well under 1 MB; refuse anything absurd
REPLACED = ("cratedigger", "nicotine_plugin")  # folders inside Contents/Resources/app that an update swaps
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str        # "0.2.0"
    tag: str            # "v0.2.0"
    notes: str
    page_url: str
    zip_url: str


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match((text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    c, cur = parse_version(candidate), parse_version(current)
    return bool(c and cur and c > cur)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():  # some Python builds ship without CA certificates; macOS always has this bundle
        for cafile in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"):
            if os.path.exists(cafile):
                ctx.load_verify_locations(cafile)
                break
    return ctx


def _get(url: str, *, accept: str, timeout: float, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"CrateDigger/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("download is larger than expected")
    return data


def check_latest(timeout: float = 10) -> Release | None:
    """The newest release if it is newer than this copy, else None. Raises on network errors."""
    try:
        raw = _get(API_LATEST, accept="application/vnd.github+json", timeout=timeout, limit=2 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # the repository has no releases yet
            return None
        raise
    data = json.loads(raw)
    tag = data.get("tag_name") or ""
    version = ".".join(map(str, parse_version(tag) or ()))
    if not version or data.get("draft") or data.get("prerelease") or not is_newer(version):
        return None
    return Release(version=version, tag=tag, notes=(data.get("body") or "").strip(),
                   page_url=data.get("html_url") or RELEASES_PAGE,
                   zip_url=f"https://api.github.com/repos/{REPO}/zipball/{tag}")


# ------------------------------------------------------------------ installed app
def app_resources() -> Path | None:
    """Contents/Resources of the running CrateDigger.app, or None when running from the project folder."""
    # In the bundle sys.executable is .../CrateDigger.app/Contents/Resources/venv/bin/python
    res = Path(sys.executable).parent.parent.parent
    if res.name == "Resources" and res.parent.name == "Contents" and (res / "app" / "cratedigger").is_dir():
        return res
    return None


def app_bundle() -> Path | None:
    res = app_resources()
    return res.parent.parent if res else None


def can_self_update() -> bool:
    res = app_resources()
    return bool(res and os.access(res / "app", os.W_OK))


def _extract(zip_bytes: bytes, dest: Path) -> Path:
    """Unpack GitHub's source zip (one top folder) into dest safely; returns that top folder."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        root = dest.resolve()
        for name in zf.namelist():
            target = (dest / name).resolve()
            if root not in target.parents and target != root:
                raise UpdateError(f"unsafe path in download: {name}")
        zf.extractall(dest)
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise UpdateError("unexpected download layout")
    return tops[0]


def _staged_version(src: Path) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (src / "cratedigger" / "__init__.py").read_text("utf-8"))
    return m.group(1) if m else ""


def install(release: Release, *, resources: Path | None = None, download=None) -> str:
    """Replace the app's code with `release`. Returns a short summary. Old code is kept if anything fails."""
    res = resources or app_resources()
    if res is None:
        raise UpdateError("CrateDigger is running from the project folder; update it with git instead.")
    app_dir = res / "app"
    if not os.access(app_dir, os.W_OK):
        raise UpdateError(f"no permission to change {app_dir}")
    fetch = download or (lambda url: _get(url, accept="application/vnd.github+json", timeout=60, limit=MAX_DOWNLOAD))
    log.info("Downloading CrateDigger %s", release.version)
    data = fetch(release.zip_url)

    with tempfile.TemporaryDirectory(prefix="cratedigger-update-") as tmp:
        src = _extract(data, Path(tmp))
        if _staged_version(src) != release.version:
            raise UpdateError(f"downloaded code is version {_staged_version(src) or '?'}, expected {release.version}")
        for name in REPLACED:
            if not (src / name).is_dir():
                raise UpdateError(f"download is missing {name}/")

        # New dependencies first: if this fails nothing has changed yet.
        new_req = src / "requirements.txt"
        old_req = app_dir / "requirements.txt"
        if new_req.exists() and (not old_req.exists() or old_req.read_bytes() != new_req.read_bytes()):
            pip = res / "venv" / "bin" / "pip"
            log.info("Installing updated dependencies")
            r = subprocess.run([str(pip), "install", "-q", "-r", str(new_req)], capture_output=True, text=True,
                               timeout=900)
            if r.returncode != 0:
                raise UpdateError("could not install the new dependencies:\n" + (r.stderr or r.stdout)[-800:])

        # Swap folders; roll back everything on the first failure.
        moved: list[str] = []
        try:
            for name in REPLACED:
                cur, old = app_dir / name, app_dir / f"{name}.old"
                shutil.rmtree(old, ignore_errors=True)
                if cur.exists():
                    cur.rename(old)
                moved.append(name)
                shutil.copytree(src / name, cur, ignore=shutil.ignore_patterns("__pycache__"))
        except Exception as e:
            for name in moved:
                cur, old = app_dir / name, app_dir / f"{name}.old"
                shutil.rmtree(cur, ignore_errors=True)
                if old.exists():
                    old.rename(cur)
            raise UpdateError(f"could not replace the app's files ({e}); the previous version was kept") from e
        for name in REPLACED:
            shutil.rmtree(app_dir / f"{name}.old", ignore_errors=True)
        if new_req.exists():
            shutil.copy2(new_req, old_req)

    _set_bundle_version(res.parent / "Info.plist", release.version)
    log.info("Updated to CrateDigger %s", release.version)
    return f"Updated to CrateDigger {release.version}."


def _set_bundle_version(plist: Path, version: str) -> None:
    try:
        with open(plist, "rb") as fh:
            info = plistlib.load(fh)
        info["CFBundleVersion"] = info["CFBundleShortVersionString"] = version
        with open(plist, "wb") as fh:
            plistlib.dump(info, fh)
    except Exception:  # cosmetic only (Finder's "Get Info"); never fail an update over it
        log.warning("could not update Info.plist version", exc_info=True)


def relaunch_command() -> list[str] | None:
    """A command that reopens the app a moment after this process quits."""
    bundle = app_bundle()
    if bundle is None:
        return None
    return ["/bin/sh", "-c", f'sleep 2; /usr/bin/open "{bundle}"']
