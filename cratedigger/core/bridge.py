"""CrateDigger side of the Nicotine+ bridge (see nicotine_plugin/cratedigger_bridge)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

PLUGIN_SRC = Path(__file__).resolve().parents[2] / "nicotine_plugin" / "cratedigger_bridge"
HEARTBEAT_STALE_S = 8.0


@dataclass
class BridgeStatus:
    installed: bool
    plugin_running: bool
    logged_in: bool
    pending: int = 0
    active: int = 0
    message: str = ""


class NicotineBridge:
    def __init__(self, bridge_dir: Path = config.BRIDGE_DIR, plugin_dir: Path = config.NICOTINE_PLUGIN_DIR):
        self.dir = Path(bridge_dir)
        self.req_dir = self.dir / "requests"
        self.res_dir = self.dir / "results"
        self.plugin_dir = Path(plugin_dir)
        self._offsets: dict[str, int] = {}
        for d in (self.req_dir, self.res_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- plugin install
    @property
    def installed_path(self) -> Path:
        return self.plugin_dir / "cratedigger_bridge"

    def is_installed(self) -> bool:
        return (self.installed_path / "__init__.py").exists()

    def install_plugin(self) -> Path:
        """Copy the plugin into Nicotine+'s user plugin folder (only touches our own sub-folder)."""
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        dst = self.installed_path
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(PLUGIN_SRC, dst, ignore=shutil.ignore_patterns("__pycache__"))
        log.info("Installed Nicotine+ plugin to %s", dst)
        return dst

    def plugin_outdated(self) -> bool:
        try:
            return (self.installed_path / "__init__.py").read_bytes() != (PLUGIN_SRC / "__init__.py").read_bytes()
        except OSError:
            return True

    # ---------------------------------------------------------------- status
    def status(self) -> BridgeStatus:
        installed = self.is_installed()
        hb_path = self.dir / "heartbeat.json"
        try:
            hb = json.loads(hb_path.read_text("utf-8"))
        except (OSError, ValueError):
            hb = None
        if not installed:
            return BridgeStatus(False, False, False, message="Plugin not installed")
        if not hb or hb.get("state") != "running" or time.time() - float(hb.get("ts", 0)) > HEARTBEAT_STALE_S:
            return BridgeStatus(True, False, False,
                                message="Nicotine+ not running, or the CrateDigger Bridge plugin is not enabled")
        logged = bool(hb.get("logged_in"))
        return BridgeStatus(True, True, logged, int(hb.get("pending", 0)), int(hb.get("active", 0)),
                            "Connected" if logged else "Nicotine+ is running but not logged in to Soulseek")

    # ---------------------------------------------------------------- requests
    def _write_request(self, obj: dict) -> str:
        rid = obj.setdefault("id", uuid.uuid4().hex)
        # Name sorts by creation time so the plugin processes in order.
        name = f"{time.time_ns()}_{rid}.json"
        tmp = self.req_dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.req_dir / name)
        return rid

    def search(self, query: str, *, window_s: float, interval_s: float, max_results: int,
               request_id: str | None = None) -> str:
        return self._write_request({"type": "search", "id": request_id or uuid.uuid4().hex, "query": query,
                                    "window_s": window_s, "interval_s": interval_s, "max_results": max_results})

    def cancel(self, request_id: str) -> None:
        # Remove the request file if the plugin has not picked it up yet; otherwise ask it to stop.
        for p in self.req_dir.glob(f"*_{request_id}.json"):
            try:
                p.unlink()
                return
            except OSError:
                pass
        self._write_request({"type": "cancel", "target": request_id})

    def download(self, username: str, path: str, size: int, attrs: dict, authorization: dict,
                 request_id: str | None = None) -> str:
        if not authorization or not authorization.get("basis"):
            raise PermissionError("A download needs a recorded authorisation basis")
        return self._write_request({"type": "download", "id": request_id or uuid.uuid4().hex, "username": username,
                                    "path": path, "size": size, "attrs": attrs, "authorization": authorization})

    def pending_requests(self) -> int:
        return len(list(self.req_dir.glob("*.json")))

    # ---------------------------------------------------------------- results
    def read_new(self) -> list[tuple[str, dict]]:
        """Return (stream, event) pairs appended since the last call. stream = request id or 'downloads'."""
        out: list[tuple[str, dict]] = []
        for p in sorted(self.res_dir.glob("*.jsonl")):
            key = p.name
            off = self._offsets.get(key, 0)
            try:
                size = p.stat().st_size
                if size < off:
                    off = 0
                if size == off:
                    continue
                with open(p, "rb") as fh:
                    fh.seek(off)
                    data = fh.read()
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                continue
            self._offsets[key] = off + end + 1
            stream = p.stem
            for line in data[: end + 1].splitlines():
                try:
                    out.append((stream, json.loads(line)))
                except ValueError:
                    log.warning("bad bridge line in %s", key)
        return out

    def forget(self, request_id: str) -> None:
        p = self.res_dir / f"{request_id}.jsonl"
        try:
            p.unlink()
        except OSError:
            pass
        self._offsets.pop(p.name, None)
