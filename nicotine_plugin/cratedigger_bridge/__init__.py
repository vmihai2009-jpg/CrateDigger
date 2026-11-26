# CrateDigger Bridge — Nicotine+ plugin (tested against Nicotine+ 3.3.x)
#
# Nicotine+ has no public remote-control API. Its plugin system, however, runs
# plugins inside the Nicotine+ process with access to the same search and
# download functions the GUI uses. This plugin exposes a tiny file-based
# request/response channel so CrateDigger can:
#
#   * run searches through *your existing, logged-in* Nicotine+ session
#     (one Soulseek login; no second client, no password stored in CrateDigger)
#   * receive the raw search responses (path, size, reported attributes)
#   * queue a download only when CrateDigger sends an explicit, user-authorised
#     download request, and report transfer status back
#
# Channel: ~/Library/Application Support/CrateDigger/bridge/
#   requests/<id>.json          CrateDigger -> plugin (one JSON object per file)
#   results/<id>.jsonl          plugin -> CrateDigger, search events
#   results/downloads.jsonl     plugin -> CrateDigger, download events
#   heartbeat.json              plugin status, rewritten every 2 s
#
# Everything here is best-effort and defensive: an error is logged and the
# request is reported as failed; it never raises into Nicotine+.

import json
import os
import ssl
import subprocess
import threading
import time
import urllib.request

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin

PLUGIN_VERSION = "0.2.0"
BRIDGE_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "CrateDigger", "bridge")
REQ_DIR = os.path.join(BRIDGE_DIR, "requests")
RES_DIR = os.path.join(BRIDGE_DIR, "results")
MIN_INTERVAL = 3.0          # hard floor between searches, whatever CrateDigger asks for
MAX_ACTIVE_SEARCHES = 4     # searches collecting results at the same time

# ---------------------------------------------------------------------------------------------------------------
# Network kill switch. Soulseek traffic (sharing/uploads, searches, downloads) is allowed ONLY when:
#   1. FortiClient VPN is not connected (checked via macOS network services, every 2 s), and
#   2. the default route goes through a VPN tunnel interface (utun*), and
#   3. NordVPN's own IP-insights service says the connection is protected and exits in REQUIRED_COUNTRY
#      (checked every 60 s and immediately when the route changes; results older than 150 s don't count).
# Otherwise Nicotine+ is disconnected from the Soulseek server and uploads are aborted; reconnect attempts are cut
# again. It fails closed: any error in a check counts as "not allowed".
REQUIRED_COUNTRY = "RO"
NORD_INSIGHTS = "https://api.nordvpn.com/v1/helpers/ips/insights"
COUNTRY_TTL = 150
COUNTRY_INTERVAL = 60


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None


def forticlient_state():
    """'connected' / 'disconnected' / 'unknown' for every FortiClient VPN service macOS knows about."""
    out = _run(["/usr/sbin/scutil", "--nc", "list"])
    if out is None:
        return "unknown"
    lines = [l for l in out.splitlines() if "forti" in l.lower()]
    if any("(Disconnected)" not in l for l in lines):
        return "connected"  # Connected, Connecting, Disconnecting... all count as connected
    return "disconnected"


def default_route_interface():
    out = _run(["/sbin/route", "-n", "get", "default"])
    if not out:
        return None
    for line in out.splitlines():
        if line.strip().startswith("interface:"):
            return line.split(":", 1)[1].strip()
    return None


def nord_insights():
    ctx = ssl.create_default_context()
    for cafile in ("/Applications/Nicotine+.app/Contents/Resources/share/cacert.pem",
                   "/etc/ssl/cert.pem"):
        if os.path.exists(cafile):
            try:
                ctx = ssl.create_default_context(cafile=cafile)
                break
            except Exception:
                pass
    req = urllib.request.Request(NORD_INSIGHTS, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


# Soulseek file attribute codes (protocol): 0 bitrate, 1 duration, 2 VBR, 4 sample rate, 5 bit depth
ATTR_NAMES = {0: "bitrate", 1: "duration", 2: "vbr", 3: "encoder", 4: "sample_rate", 5: "bit_depth"}


def _attrs_to_dict(attrs):
    out = {}
    if not attrs:
        return out
    try:
        items = attrs.items() if hasattr(attrs, "items") else attrs
        for code, value in items:
            name = ATTR_NAMES.get(int(code))
            if name:
                out[name] = int(value)
    except Exception:
        pass
    return out


class Plugin(BasePlugin):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending = []           # queued search requests (dicts)
        self.active = {}            # token -> {"req":..., "started":..., "count":...}
        self.files_attrs = {}       # (user, path) -> original attrs object, for downloads
        self.downloads = {}         # (user, path) -> {"id":..., "status":...}
        self.last_search = 0.0
        self.timer_id = None
        self.handlers = []
        # kill switch state
        self.net_ok = False
        self.net_reason = "starting: network not verified yet"
        self.country = None          # (country_code, protected, checked_at)
        self.country_busy = False
        self.last_route = None
        self.we_disconnected = False
        self.last_net_check = 0.0

    # ---------------------------------------------------------------- lifecycle
    def loaded_notification(self):
        for d in (REQ_DIR, RES_DIR):
            os.makedirs(d, exist_ok=True)
        self.handlers = [("file-search-response", self._on_search_response),
                         ("server-login", self._on_server_login)]
        for name, fn in self.handlers:
            events.connect(name, fn)
        self.timer_id = events.schedule(delay=1, callback=self._tick, repeat=True)
        self._check_network(force=True)   # fail closed immediately at startup
        self.log("CrateDigger bridge %s loaded; channel: %s", (PLUGIN_VERSION, BRIDGE_DIR))
        self._heartbeat(force=True)

    def unloaded_notification(self):
        if self.timer_id is not None:
            events.cancel_scheduled(self.timer_id)
        for name, fn in self.handlers:
            try:
                events.disconnect(name, fn)
            except Exception:
                pass
        self._write_json(os.path.join(BRIDGE_DIR, "heartbeat.json"),
                         {"ts": time.time(), "plugin_version": PLUGIN_VERSION, "state": "unloaded"})

    # ---------------------------------------------------------------- helpers
    def _write_json(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)

    def _append(self, name, obj):
        obj["ts"] = time.time()
        with open(os.path.join(RES_DIR, name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- kill switch
    def _start_country_check(self):
        if self.country_busy:
            return
        self.country_busy = True

        def work():
            try:
                d = nord_insights()
                self.country = (str(d.get("country_code") or "").upper(), bool(d.get("protected")), time.time())
            except Exception as e:
                self.country = (None, False, time.time())
                self.net_detail_error = str(e)
            finally:
                self.country_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _evaluate(self):
        forti = forticlient_state()
        if forti != "disconnected":
            return False, "FortiClient VPN is connected" if forti == "connected" else "cannot read FortiClient state"
        iface = default_route_interface()
        if iface != self.last_route:
            self.last_route = iface
            self.country = None           # route changed: previous country result no longer counts
            self._start_country_check()
        if not iface or not iface.startswith("utun"):
            return False, f"traffic is not going through a VPN tunnel (default route: {iface or 'none'})"
        c = self.country
        if c is None or time.time() - c[2] > COUNTRY_INTERVAL:
            self._start_country_check()
        if c is None or time.time() - c[2] > COUNTRY_TTL:
            return False, "waiting for NordVPN location check"
        code, protected, _ = c
        if not protected:
            return False, "NordVPN reports this connection as NOT protected"
        if code != REQUIRED_COUNTRY:
            return False, f"NordVPN exit country is {code or 'unknown'}, not {REQUIRED_COUNTRY}"
        return True, f"NordVPN protected, exit country {code}"

    def _check_network(self, force=False):
        now = time.time()
        if not force and now - self.last_net_check < 2:
            return
        self.last_net_check = now
        try:
            ok, reason = self._evaluate()
        except Exception as e:
            ok, reason = False, f"network check failed: {e}"
        changed = ok != self.net_ok
        self.net_ok, self.net_reason = ok, reason
        if not ok:
            self._enforce_block(reason, changed)
        elif self.we_disconnected and not self._logged_in():
            self.we_disconnected = False
            self.log("CrateDigger kill switch: conditions met (%s), reconnecting", (reason,))
            try:
                self.core.connect()
            except Exception as e:
                self.log("CrateDigger kill switch: reconnect failed: %s", (e,))
        if changed:
            self._append("network.jsonl", {"ev": "network", "ok": ok, "reason": reason})

    def _enforce_block(self, reason, changed):
        try:
            uploads = getattr(self.core.uploads, "transfers", {}) or {}
            ups = list(uploads.values() if hasattr(uploads, "values") else uploads)
            active = [t for t in ups if str(getattr(t, "status", "")) in ("Transferring", "Getting status", "Queued")]
            if active:
                self.core.uploads.abort_uploads(active)
        except Exception as e:
            self.log("CrateDigger kill switch: could not abort uploads: %s", (e,))
        if self._logged_in():
            self.we_disconnected = True
            self.log("CrateDigger kill switch: disconnecting from Soulseek (%s)", (reason,))
            try:
                self.core.disconnect()
            except Exception as e:
                self.log("CrateDigger kill switch: disconnect failed: %s", (e,))
        elif changed:
            self.log("CrateDigger kill switch: Soulseek blocked (%s)", (reason,))

    def _on_server_login(self, *args, **kwargs):
        # Any (re)connection — manual, automatic or at startup — is re-checked immediately.
        self._check_network(force=True)

    def _logged_in(self):
        try:
            return int(self.core.users.login_status) != 0  # UserStatus.OFFLINE == 0
        except Exception:
            return False

    _last_hb = 0.0

    def _heartbeat(self, force=False):
        now = time.time()
        if not force and now - self._last_hb < 2:
            return
        self._last_hb = now
        self._write_json(os.path.join(BRIDGE_DIR, "heartbeat.json"), {
            "ts": now, "plugin_version": PLUGIN_VERSION, "state": "running",
            "logged_in": self._logged_in(), "pending": len(self.pending), "active": len(self.active),
            "network_ok": self.net_ok, "network_reason": self.net_reason,
            "downloads": len(self.downloads),
        })

    # ---------------------------------------------------------------- main loop
    def _tick(self):
        try:
            self._check_network()
            if not self.net_ok:
                self._heartbeat()
                return  # requests stay queued until the network is allowed again
            self._read_requests()
            self._start_searches()
            self._finish_searches()
            self._poll_downloads()
            self._heartbeat()
        except Exception as e:  # never let an exception escape into Nicotine+
            self.log("CrateDigger bridge error: %s", (e,))

    def _read_requests(self):
        try:
            names = sorted(n for n in os.listdir(REQ_DIR) if n.endswith(".json"))
        except FileNotFoundError:
            os.makedirs(REQ_DIR, exist_ok=True)
            return
        for name in names:
            path = os.path.join(REQ_DIR, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    req = json.load(fh)
            except (OSError, ValueError):
                continue  # half-written; picked up next tick
            try:
                os.remove(path)
            except OSError:
                pass
            kind = req.get("type")
            if kind == "search":
                self.pending.append(req)
            elif kind == "download":
                self._download(req)
            elif kind == "cancel":
                self.pending = [r for r in self.pending if r.get("id") != req.get("target")]
                for tok, st in list(self.active.items()):
                    if st["req"].get("id") == req.get("target"):
                        st["started"] = 0  # finishes on this tick
            elif kind == "ping":
                self._append(req["id"] + ".jsonl", {"ev": "pong"})

    def _start_searches(self):
        if not self.net_ok:
            return
        if not self.pending or len(self.active) >= MAX_ACTIVE_SEARCHES:
            return
        req = self.pending[0]
        interval = max(MIN_INTERVAL, float(req.get("interval_s", 6)))
        if time.time() - self.last_search < interval:
            return
        if not self._logged_in():
            return  # wait for login; heartbeat tells CrateDigger why nothing happens
        self.pending.pop(0)
        rid = req["id"]
        try:
            self.core.search.do_search(req["query"], "global", switch_page=False)
            token = self.core.search.token
        except Exception as e:
            self._append(rid + ".jsonl", {"ev": "failed", "error": str(e)})
            return
        self.last_search = time.time()
        self.active[token] = {"req": req, "started": time.time(), "count": 0}
        self._append(rid + ".jsonl", {"ev": "started", "token": token, "query": req["query"]})

    def _finish_searches(self):
        now = time.time()
        for token, st in list(self.active.items()):
            if now - st["started"] >= float(st["req"].get("window_s", 30)):
                del self.active[token]
                self._append(st["req"]["id"] + ".jsonl", {"ev": "done", "count": st["count"]})
                if st["req"].get("close_tab", True):
                    try:
                        self.core.search.remove_search(token)
                    except Exception:
                        pass

    # ---------------------------------------------------------------- search results
    def _on_search_response(self, msg):
        st = self.active.get(getattr(msg, "token", None))
        if st is None:
            return
        limit = int(st["req"].get("max_results", 200))
        if st["count"] >= limit:
            return
        files = []
        for private, lst in ((False, getattr(msg, "list", None) or []), (True, getattr(msg, "privatelist", None) or [])):
            for entry in lst:
                try:
                    _code, vpath, size, _ext, attrs = entry[:5]
                except (ValueError, TypeError):
                    continue
                a = _attrs_to_dict(attrs)
                files.append({"path": vpath, "size": int(size or 0), "attrs": a, "private": private})
                self.files_attrs[(msg.username, vpath)] = attrs
        if not files:
            return
        files = files[: max(0, limit - st["count"])]
        st["count"] += len(files)
        self._append(st["req"]["id"] + ".jsonl", {
            "ev": "results", "user": msg.username, "free_slot": bool(getattr(msg, "freeulslots", False)),
            "speed": int(getattr(msg, "ulspeed", 0) or 0), "queue": int(getattr(msg, "inqueue", 0) or 0),
            "files": files,
        })

    # ---------------------------------------------------------------- downloads
    def _download(self, req):
        user, vpath = req.get("username"), req.get("path")
        if not self.net_ok:
            self.pending_downloads = getattr(self, "pending_downloads", []) + [req]
            return
        rid = req.get("id")
        if not req.get("authorization"):
            self._append("downloads.jsonl", {"ev": "rejected", "id": rid, "error": "no authorisation basis supplied"})
            return
        attrs = self.files_attrs.get((user, vpath))
        if attrs is None and req.get("attrs"):
            attrs = {int(k): int(v) for k, v in req["attrs"].items()}
        try:
            self.core.downloads.enqueue_download(user, vpath, size=int(req.get("size") or 0), file_attributes=attrs)
        except Exception as e:
            self._append("downloads.jsonl", {"ev": "failed", "id": rid, "user": user, "path": vpath, "error": str(e)})
            return
        self.downloads[(user, vpath)] = {"id": rid, "status": None}
        self._append("downloads.jsonl", {"ev": "queued", "id": rid, "user": user, "path": vpath})

    def _poll_downloads(self):
        if not self.downloads:
            return
        transfers = getattr(self.core.downloads, "transfers", {}) or {}
        values = transfers.values() if hasattr(transfers, "values") else transfers
        index = {}
        for t in values:
            try:
                index[(t.username, t.virtual_path)] = t
            except AttributeError:
                continue
        for key, st in list(self.downloads.items()):
            t = index.get(key)
            if t is None:
                continue
            status = str(t.status)
            size = int(t.size or 0)
            done = int(getattr(t, "current_byte_offset", 0) or 0)
            pct = int(done * 100 / size) if size else 0
            if status != st["status"] or (status == "Transferring" and pct != st.get("pct")):
                st["status"], st["pct"] = status, pct
                self._append("downloads.jsonl", {"ev": "status", "id": st["id"], "user": key[0], "path": key[1],
                                                 "status": status, "percent": pct})

    def download_finished_notification(self, user, virtual_path, real_path):
        st = self.downloads.pop((user, virtual_path), None)
        if st is None:
            return  # not a CrateDigger download
        self._append("downloads.jsonl", {"ev": "finished", "id": st["id"], "user": user, "path": virtual_path,
                                         "real_path": real_path})
