"""Load the CrateDigger Bridge plugin inside the *real* Nicotine+ code base (from the
installed Nicotine+.app) with a simulated core, and feed it real, wire-format search
responses parsed by Nicotine+'s own slskmessages. No network, no Soulseek account.
"""
from __future__ import annotations

import importlib.util
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

NICOTINE_LIB = Path("/Applications/Nicotine+.app/Contents/Resources/lib")
ROOT = Path(__file__).resolve().parents[1]


def available() -> bool:
    return (NICOTINE_LIB / "pynicotine").is_dir()


def _import_pynicotine():
    # Only the pure-Python package; keep our own interpreter's stdlib/zlib first on the path.
    shim = Path(tempfile.gettempdir()) / "cratedigger_npy"
    shim.mkdir(exist_ok=True)
    link = shim / "pynicotine"
    if not link.exists():
        os.symlink(NICOTINE_LIB / "pynicotine", link)
    if str(shim) not in sys.path:
        sys.path.append(str(shim))
    from pynicotine import slskmessages
    from pynicotine.events import events
    return slskmessages, events


def _s(b: str) -> bytes:
    e = b.encode("utf-8")
    return struct.pack("<I", len(e)) + e


def wire_search_response(username: str, token: int, files: list[tuple[str, int, dict]],
                         free=True, speed=5_000_000, queue=0) -> bytes:
    """Bytes of a FileSearchResponse exactly as a peer sends it (zlib-compressed payload)."""
    out = _s(username) + struct.pack("<II", token, len(files))
    for path, size, attrs in files:
        ext = path.rsplit(".", 1)[-1]
        out += b"\x01" + _s(path) + struct.pack("<Q", size) + _s(ext) + struct.pack("<I", len(attrs))
        for code, val in attrs.items():
            out += struct.pack("<II", code, val)
    out += struct.pack("<BII", 1 if free else 0, speed, queue)
    out += struct.pack("<I", 0)  # unknown
    out += struct.pack("<I", 0)  # private files
    return zlib.compress(out)


# Tokens registered via AddAllowedResponse (Search.add_allowed_token) in real Nicotine+.
ALLOWED_TOKENS: set[int] = set()


class FakeTransfer:
    def __init__(self, username, virtual_path, size):
        self.username, self.virtual_path, self.size = username, virtual_path, size
        self.status = "Queued"
        self.current_byte_offset = 0


class FakeCore:
    class _Users:
        login_status = 2  # online

    def __init__(self):
        self.users = self._Users()
        self.searches = []
        self.removed = []
        self.enqueued = []
        core = self

        class _Search:
            token = 1000

            def do_search(self, term, mode, room=None, users=None, switch_page=True):
                # Like Search.do_search -> add_search -> add_allowed_token: only responses to
                # allowed tokens are fully parsed by Nicotine+'s network thread.
                self.token += 1
                ALLOWED_TOKENS.add(self.token)
                core.searches.append((self.token, term, mode, switch_page))

            def remove_search(self, token):
                ALLOWED_TOKENS.discard(token)
                core.removed.append(token)

        class _Downloads:
            transfers = {}

            def enqueue_download(self, username, virtual_path, folder_path=None, size=0, file_attributes=None,
                                 bypass_filter=False):
                core.enqueued.append((username, virtual_path, size, file_attributes))
                self.transfers[(username, virtual_path)] = FakeTransfer(username, virtual_path, size)

        self.search = _Search()
        self.downloads = _Downloads()


def load_plugin(bridge_dir: Path):
    slsk, events = _import_pynicotine()
    spec = importlib.util.spec_from_file_location(
        "cratedigger_bridge_under_test", ROOT / "nicotine_plugin" / "cratedigger_bridge" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BRIDGE_DIR = str(bridge_dir)
    mod.REQ_DIR = str(bridge_dir / "requests")
    mod.RES_DIR = str(bridge_dir / "results")
    mod.MIN_INTERVAL = 0.0
    plugin = mod.Plugin()
    plugin.core = FakeCore()
    plugin.human_name = "CrateDigger Bridge"
    plugin.log = lambda msg, args=None: None
    plugin.loaded_notification()
    return plugin, slsk, events


def deliver(slsk, events, username, token, files, **kw):
    msg = slsk.FileSearchResponse()
    msg.allowed_responses = set(ALLOWED_TOKENS)  # the network thread's registered tokens
    msg.parse_network_message(wire_search_response(username, token, files, **kw))
    msg.username = username  # set by the network thread from the peer connection
    if msg.list is None:
        return msg  # Nicotine+ drops responses to tokens it did not register; never emitted
    events.emit("file-search-response", msg)
    return msg
