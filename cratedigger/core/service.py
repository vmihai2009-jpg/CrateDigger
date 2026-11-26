"""Workflow layer: everything the UI does goes through here (and is usable headless)."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import library as lib
from .audio_analysis import analyze_file
from .bridge import NicotineBridge
from .csv_import import ImportResult, import_csv
from .db import Database, dumps, loads
from .matching import TrackRef, score_candidate
from .quality import AUDIO_EXTS, assess_reported
from .security import name_risks, scan_file, update_signatures_if_new_day
from .text import base_title, build_queries, norm, path_parts

log = logging.getLogger(__name__)

AUTH_BASES = [
    "Public domain",
    "Creative Commons licence permits copying",
    "Uploader / rights holder explicitly authorised sharing",
    "I own a licensed copy (personal format-shift where lawful)",
    "Other written permission",
]
# Nicotine+ transfer statuses that mean "still on its way"; anything else is a failure.
DL_PROGRESS = {"Queued", "Getting status", "Transferring", "Paused", "Finished"}
# Failures Nicotine+ often recovers from by itself: wait before switching to another sharer.
DL_TRANSIENT = {"Connection timeout", "Connection closed", "User logged off"}
DL_RETRY_GRACE_S = 180
DL_MAX_ATTEMPTS = 3

TRACK_STATUSES = ["new", "queued", "searching", "review", "missing", "selected", "downloading",
                  "downloaded", "in_library", "failed"]


_WEBSITE_USER = __import__("re").compile(r"https?://|www\.|\.(com|net|org|ru|info|io)\b", __import__("re").I)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Service:
    def __init__(self, db: Database, settings, bridge: NicotineBridge | None = None):
        self.db = db
        self.settings = settings
        self.bridge = bridge or NicotineBridge()

    # ================================================================ import
    def import_csv(self, path: str | Path, name: str | None = None) -> ImportResult:
        return import_csv(self.db, path, self.settings, name)

    def import_inbox(self) -> list[tuple[str, ImportResult | str]]:
        """Import every playlist CSV another app (Minapp) dropped in the inbox folder, then move it to inbox/done
        (or inbox/failed with the reason). Returns [(playlist name, result or error text)]."""
        from datetime import datetime

        from . import config
        out: list[tuple[str, ImportResult | str]] = []
        inbox = config.INBOX_DIR
        if not inbox.is_dir():
            return out
        for f in sorted(inbox.glob("*.csv")):
            try:
                res = import_csv(self.db, f, self.settings, f.stem, reuse_playlist=True)
                dest = inbox / "done"
                out.append((f.stem, res))
            except Exception as e:  # a bad file must not block the others
                log.warning("Inbox import failed for %s: %s", f.name, e)
                dest = inbox / "failed"
                out.append((f.stem, str(e)))
            try:
                dest.mkdir(exist_ok=True)
                f.replace(dest / f"{datetime.now():%Y%m%d-%H%M%S}-{f.name}")
            except OSError as e:
                log.warning("Could not move %s: %s", f.name, e)
        return out

    # ================================================================ stats
    def stats(self) -> dict:
        q = self.db.scalar
        by_status = {r[0]: r[1] for r in self.db.q("SELECT status, COUNT(*) FROM tracks GROUP BY status")}
        return {
            "tracks": q("SELECT COUNT(*) FROM tracks") or 0,
            "searched": q("SELECT COUNT(DISTINCT track_id) FROM searches WHERE status='done'") or 0,
            "matched": q("SELECT COUNT(DISTINCT track_id) FROM candidates WHERE score>=? AND state!='rejected'",
                         (self.settings.min_match_score,)) or 0,
            "review": by_status.get("review", 0) + by_status.get("selected", 0) + by_status.get("downloaded", 0),
            "missing": by_status.get("missing", 0),
            "approved": q("SELECT COUNT(*) FROM library") or 0,
            "pending_searches": q("SELECT COUNT(*) FROM searches WHERE status IN ('queued','sent','running')") or 0,
            "by_status": by_status,
        }

    # ================================================================ searching
    def queries_for(self, track) -> list[str]:
        qs = build_queries(track["title"], track["artists"])
        if track["search_query"] and track["search_query"] not in qs:
            qs.insert(0, track["search_query"])
        return qs

    def queue_search(self, track_ids: list[int], query: str | None = None, fallback: bool = False) -> int:
        """Queue searches. `query` overrides; `fallback` picks the next unused generated query."""
        if not fallback:  # user-initiated: first fetch of the day refreshes virus definitions first
            update_signatures_if_new_day()
        n = 0
        for tid in track_ids:
            t = self.db.one("SELECT * FROM tracks WHERE id=?", (tid,))
            if t is None or t["status"] == "in_library":
                continue
            if self.db.scalar("SELECT COUNT(*) FROM searches WHERE track_id=? AND status IN ('queued','sent','running')", (tid,)):
                continue  # already in flight
            q = query
            if q is None:
                used = {r[0].lower() for r in self.db.q("SELECT query FROM searches WHERE track_id=?", (tid,))}
                qs = self.queries_for(t)
                if fallback:
                    nxt = [x for x in qs if x.lower() not in used]
                    if not nxt:
                        continue
                    q = nxt[0]
                else:
                    q = qs[0]
            rid = "s" + uuid.uuid4().hex
            self.db.exec("INSERT INTO searches(track_id, query, backend, status, request_id) VALUES (?,?,?,?,?)",
                         (tid, q, "nicotine", "sent", rid))
            self.bridge.search(q, window_s=self.settings.search_window_s, interval_s=self.settings.search_interval_s,
                               max_results=self.settings.max_results_per_track, request_id=rid)
            self.db.exec("UPDATE tracks SET status='queued' WHERE id=?", (tid,))
            n += 1
        if n:
            log.info("Queued %d searches", n)
        return n

    def cancel_pending(self) -> int:
        rows = self.db.q("SELECT * FROM searches WHERE status IN ('queued','sent','running')")
        for r in rows:
            self.bridge.cancel(r["request_id"])
            self.db.exec("UPDATE searches SET status='cancelled', finished_at=? WHERE id=?", (now(), r["id"]))
            self._finalize_track(r["track_id"], allow_fallback=False)
        return len(rows)

    def process_bridge_events(self) -> set[int]:
        """Apply new plugin events to the DB. Returns ids of tracks that changed."""
        changed: set[int] = set()
        for stream, ev in self.bridge.read_new():
            try:
                if stream == "downloads":
                    tid = self._download_event(ev)
                else:
                    tid = self._search_event(stream, ev)
                if tid:
                    changed.add(tid)
            except Exception:
                log.exception("failed to apply bridge event %s %s", stream, ev.get("ev"))
        return changed

    def _search_event(self, rid: str, ev: dict) -> int | None:
        s = self.db.one("SELECT * FROM searches WHERE request_id=?", (rid,))
        if s is None:
            return None
        kind = ev.get("ev")
        if kind == "started":
            self.db.exec("UPDATE searches SET status='running', started_at=? WHERE id=?", (now(), s["id"]))
            self.db.exec("UPDATE tracks SET status='searching' WHERE id=? AND status IN ('queued','new','missing','failed','review')",
                         (s["track_id"],))
        elif kind == "results":
            if s["status"] in ("cancelled",):
                return None
            added = self._add_soulseek_results(s, ev)
            if added:
                self.db.exec("UPDATE searches SET result_count=result_count+? WHERE id=?", (added, s["id"]))
        elif kind == "failed":
            self.db.exec("UPDATE searches SET status='failed', error=?, finished_at=? WHERE id=?",
                         (ev.get("error"), now(), s["id"]))
            self._finalize_track(s["track_id"], allow_fallback=False)
            self.bridge.forget(rid)
        elif kind == "done":
            if s["status"] != "cancelled":
                self.db.exec("UPDATE searches SET status='done', finished_at=? WHERE id=?", (now(), s["id"]))
            self._finalize_track(s["track_id"], allow_fallback=True)
            self.bridge.forget(rid)
        return s["track_id"]

    def _add_soulseek_results(self, search, ev: dict) -> int:
        t = self.db.one("SELECT * FROM tracks WHERE id=?", (search["track_id"],))
        ref = TrackRef.from_row(t)
        n = 0
        with self.db.tx() as c:
            for f in ev.get("files", []):
                path = f.get("path") or ""
                _folder, _stem, ext = path_parts(path)
                if ext not in AUDIO_EXTS:
                    continue
                a = f.get("attrs") or {}
                dur = a.get("duration")
                size = f.get("size")
                m = score_candidate(ref, path, dur, tol=self.settings.duration_tolerance_s)
                qa = assess_reported(ext, size, a.get("bitrate"), dur, a.get("sample_rate"), a.get("bit_depth"), a.get("vbr"))
                risks = name_risks(path)
                if _WEBSITE_USER.search(ev.get("user") or ""):
                    qa.flags.append("sharer's username is a website (promo/repost account)")
                flags = (["SECURITY: " + r for r in risks] + m.flags + qa.flags
                         + (["private (buddy-only) share"] if f.get("private") else []))
                detail = dict(m.detail, implied_kbps=qa.implied_kbps, quality=qa.label)
                cur = c.execute(
                    """INSERT OR IGNORE INTO candidates(track_id, search_id, source, username, path, size, ext, bitrate, vbr,
                       duration_s, sample_rate, bit_depth, free_slot, upload_speed, queue_len, score, score_detail, version,
                       quality_tier, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t["id"], search["id"], "soulseek", ev.get("user"), path, size, ext, a.get("bitrate"), a.get("vbr"),
                     dur, a.get("sample_rate"), a.get("bit_depth"), int(bool(ev.get("free_slot"))), ev.get("speed"),
                     ev.get("queue"), m.score, dumps(detail), m.version, qa.tier, dumps(flags)))
                n += cur.rowcount
        return n

    def rescore_all(self) -> int:
        """Re-apply the current matching/quality/security rules to every stored Soulseek candidate."""
        tracks = {r["id"]: r for r in self.db.q("SELECT * FROM tracks")}
        n = 0
        with self.db.tx() as c:
            for r in c.execute("SELECT * FROM candidates WHERE source='soulseek'").fetchall():
                t = tracks.get(r["track_id"])
                if t is None:
                    continue
                m = score_candidate(TrackRef.from_row(t), r["path"], r["duration_s"], tol=self.settings.duration_tolerance_s)
                qa = assess_reported(r["ext"], r["size"], r["bitrate"], r["duration_s"], r["sample_rate"], r["bit_depth"],
                                     r["vbr"])
                old = loads(r["flags"], []) or []
                flags = (["SECURITY: " + x for x in name_risks(r["path"])] + m.flags + qa.flags
                         + [f for f in old if f.startswith("private")]
                         + (["sharer's username is a website (promo/repost account)"]
                            if _WEBSITE_USER.search(r["username"] or "") else []))
                detail = dict(m.detail, implied_kbps=qa.implied_kbps, quality=qa.label)
                c.execute("UPDATE candidates SET score=?, score_detail=?, version=?, quality_tier=?, flags=? WHERE id=?",
                          (m.score, dumps(detail), m.version, qa.tier, dumps(flags), r["id"]))
                n += 1
        for tid in tracks:
            if tracks[tid]["status"] in ("review", "missing"):
                self.db.exec("UPDATE tracks SET status=? WHERE id=?",
                             ("review" if self.db.scalar(
                                 "SELECT COUNT(*) FROM candidates WHERE track_id=? AND score>=? AND state!='rejected'",
                                 (tid, self.settings.min_match_score)) else "missing", tid))
        log.info("Re-scored %d candidates with current rules", n)
        return n

    def mark_duplicates(self, track_id: int) -> None:
        """Same size + extension from several users = the same file shared widely; keep the best-sourced one as primary."""
        rows = self.db.q("""SELECT id, size, ext, free_slot, upload_speed, queue_len FROM candidates
                            WHERE track_id=? AND source='soulseek' ORDER BY size""", (track_id,))
        groups: dict[tuple, list] = {}
        for r in rows:
            if r["size"]:
                groups.setdefault((r["size"], r["ext"]), []).append(r)
        with self.db.tx() as c:
            c.execute("UPDATE candidates SET dup_of=NULL WHERE track_id=?", (track_id,))
            for members in groups.values():
                if len(members) < 2:
                    continue
                members.sort(key=lambda r: (-(r["free_slot"] or 0), r["queue_len"] or 0, -(r["upload_speed"] or 0)))
                primary = members[0]["id"]
                for r in members[1:]:
                    c.execute("UPDATE candidates SET dup_of=? WHERE id=?", (primary, r["id"]))

    def _finalize_track(self, track_id: int, allow_fallback: bool) -> None:
        if self.db.scalar("SELECT COUNT(*) FROM searches WHERE track_id=? AND status IN ('queued','sent','running')", (track_id,)):
            return
        self.mark_duplicates(track_id)
        t = self.db.one("SELECT status FROM tracks WHERE id=?", (track_id,))
        if t is None or t["status"] in ("selected", "downloading", "downloaded", "in_library"):
            return
        good = self.db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=? AND score>=? AND state!='rejected'",
                              (track_id, self.settings.min_match_score))
        if good:
            self.db.exec("UPDATE tracks SET status='review' WHERE id=?", (track_id,))
            self.auto_process_track(track_id)
            return
        if allow_fallback and self.queue_search([track_id], fallback=True):
            return
        failed = self.db.scalar("SELECT COUNT(*) FROM searches WHERE track_id=? AND status='done'", (track_id,)) == 0
        self.db.exec("UPDATE tracks SET status=? WHERE id=?", ("failed" if failed else "missing", track_id))

    def check_timeouts(self) -> None:
        """Recover searches lost because Nicotine+ restarted, or that never reported 'done'."""
        st = self.bridge.status()
        if not st.plugin_running:
            return
        if st.logged_in:
            self.retry_stale_downloads()
        grace = self.settings.search_window_s + 120
        for s in self.db.q("SELECT * FROM searches WHERE status='running'"):
            started = datetime.strptime(s["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() > grace:
                self.db.exec("UPDATE searches SET status='done', finished_at=?, error='timed out' WHERE id=?", (now(), s["id"]))
                self._finalize_track(s["track_id"], allow_fallback=True)
        # 'sent' requests the plugin no longer knows about (Nicotine+ restarted): resend
        if st.pending == 0 and st.active == 0 and self.bridge.pending_requests() == 0:
            for s in self.db.q("SELECT * FROM searches WHERE status='sent'"):
                queued = datetime.strptime(s["queued_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - queued).total_seconds() > 20:
                    log.info("Re-sending lost search %s (%s)", s["request_id"], s["query"])
                    self.bridge.search(s["query"], window_s=self.settings.search_window_s,
                                       interval_s=self.settings.search_interval_s,
                                       max_results=self.settings.max_results_per_track, request_id=s["request_id"])
                    self.db.exec("UPDATE searches SET queued_at=datetime('now') WHERE id=?", (s["id"],))

    # ================================================================ candidates / downloads
    def set_candidate_state(self, cid: int, state: str) -> None:
        c = self.db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if c is None:
            return
        if state == "selected":
            self.db.exec("UPDATE candidates SET state='new' WHERE track_id=? AND state='selected'", (c["track_id"],))
        self.db.exec("UPDATE candidates SET state=? WHERE id=?", (state, cid))
        if state == "selected":
            self.db.exec("UPDATE tracks SET status='selected' WHERE id=? AND status NOT IN ('downloading','downloaded','in_library')",
                         (c["track_id"],))
        elif state in ("new", "rejected"):
            if not self.db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=? AND state='selected'", (c["track_id"],)):
                self.db.exec("UPDATE tracks SET status='review' WHERE id=? AND status='selected'", (c["track_id"],))

    def authorize(self, cid: int, basis: str, note: str = "") -> None:
        if basis not in AUTH_BASES:
            raise ValueError("unknown authorisation basis")
        self.db.exec("INSERT INTO authorizations(candidate_id, basis, note) VALUES (?,?,?)", (cid, basis, note))
        self.db.exec("UPDATE candidates SET state='authorized' WHERE id=?", (cid,))
        log.info("Candidate %d authorised: %s %s", cid, basis, note)

    def authorization_for(self, cid: int):
        return self.db.one("SELECT * FROM authorizations WHERE candidate_id=? ORDER BY id DESC", (cid,))

    def request_download(self, cid: int, refresh_av: bool = True) -> str:
        c = self.db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if c is None or c["source"] != "soulseek":
            raise ValueError("only Soulseek candidates can be downloaded")
        if refresh_av:
            update_signatures_if_new_day()
        risks = name_risks(c["path"])
        if risks:
            raise PermissionError("Refusing to download a file with a suspicious name: " + "; ".join(risks))
        auth = self.authorization_for(cid)
        if auth is None:
            raise PermissionError("Record an authorisation basis before downloading")
        attrs = {}
        for code, key in ((0, "bitrate"), (1, "duration_s"), (2, "vbr"), (4, "sample_rate"), (5, "bit_depth")):
            if c[key] is not None:
                attrs[str(code)] = int(c[key])
        rid = f"d{cid}_{uuid.uuid4().hex[:8]}"
        self.bridge.download(c["username"], c["path"], c["size"] or 0, attrs,
                             {"basis": auth["basis"], "note": auth["note"]}, request_id=rid)
        self.db.exec("UPDATE candidates SET state='queued_download', dl_status='Sent to Nicotine+' WHERE id=?", (cid,))
        self.db.exec("UPDATE tracks SET status='downloading' WHERE id=?", (c["track_id"],))
        return rid

    def _download_event(self, ev: dict) -> int | None:
        rid = ev.get("id") or ""
        if not rid.startswith("d") or "_" not in rid:
            return None
        try:
            cid = int(rid[1:].split("_")[0])
        except ValueError:
            return None
        c = self.db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if c is None:
            return None
        kind = ev.get("ev")
        if kind == "queued":
            self.db.exec("UPDATE candidates SET state='queued_download', dl_status='Queued in Nicotine+' WHERE id=?", (cid,))
        elif kind == "status":
            status = ev.get("status") or ""
            if c["state"] == "abandoned":
                return c["track_id"]  # replaced by a retry; ignore late updates
            if status not in DL_PROGRESS:
                if c["state"] != "failed":
                    self.db.exec("UPDATE candidates SET state='failed', dl_status=?, dl_failed_at=? WHERE id=?",
                                 (status, time.time(), cid))
                if status not in DL_TRANSIENT:
                    self.retry_download(cid)  # permanent (e.g. 'File not shared.'): switch sharer now
            else:
                state = "downloading" if status == "Transferring" else "queued_download"
                self.db.exec("UPDATE candidates SET state=?, dl_status=?, dl_percent=?, dl_failed_at=NULL WHERE id=?",
                             (state, status, ev.get("percent"), cid))
        elif kind == "finished":
            self.db.exec("UPDATE candidates SET state='downloaded', dl_status='Finished', dl_percent=100, local_path=? WHERE id=?",
                         (ev.get("real_path"), cid))
            self.db.exec("UPDATE tracks SET status='downloaded' WHERE id=?", (c["track_id"],))
        elif kind in ("failed", "rejected"):
            self.db.exec("UPDATE candidates SET state='failed', dl_status=?, dl_failed_at=? WHERE id=?",
                         (ev.get("error"), time.time(), cid))
            self.retry_download(cid)
        return c["track_id"]

    def retry_download(self, cid: int) -> int | None:
        """Replace a failed download with the next-best sharer's copy (same authorisation basis)."""
        c = self.db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if c is None or c["state"] != "failed":
            return None
        tid = c["track_id"]
        auth = self.authorization_for(cid)
        attempts = self.db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=? AND state IN ('failed','abandoned')",
                                  (tid,))
        nxt = None
        if auth is not None and attempts < DL_MAX_ATTEMPTS:
            bad_users = {r[0] for r in self.db.q(
                "SELECT username FROM candidates WHERE track_id=? AND state IN ('failed','abandoned')", (tid,))}
            nxt = self.best_download_candidate(tid, exclude_users=bad_users)
        self.db.exec("UPDATE candidates SET state='abandoned' WHERE id=?", (cid,))
        if nxt is None:
            self.db.exec("UPDATE tracks SET status='review' WHERE id=? AND status='downloading'", (tid,))
            log.info("Download for track %s failed (%s); no other sharer to try after %d attempts",
                     tid, c["dl_status"], attempts)
            return None
        self.authorize(nxt["id"], auth["basis"],
                       f"{auth['note'] or ''} [automatic retry: {c['username']} failed with '{c['dl_status']}']".strip())
        self.set_candidate_state(nxt["id"], "selected")
        try:
            self.request_download(nxt["id"], refresh_av=False)
        except Exception as e:
            log.warning("retry for track %s not queued: %s", tid, e)
            return None
        log.info("Retrying track %s with %s (after %s from %s)", tid, nxt["username"], c["dl_status"], c["username"])
        return nxt["id"]

    def retry_stale_downloads(self) -> int:
        """Retry transient failures that Nicotine+ did not recover from within the grace period."""
        n = 0
        # downloads stuck with an error status from before this version (no failure time recorded)
        for r in self.db.q("SELECT id, dl_status FROM candidates WHERE state IN ('queued_download','downloading')"):
            if r["dl_status"] and r["dl_status"] not in DL_PROGRESS and "Nicotine+" not in r["dl_status"]:
                self.db.exec("UPDATE candidates SET state='failed', dl_failed_at=? WHERE id=?",
                             (time.time() - DL_RETRY_GRACE_S, r["id"]))
        for r in self.db.q("SELECT id FROM candidates WHERE state='failed' AND "
                           "(dl_failed_at IS NULL OR dl_failed_at < ?)", (time.time() - DL_RETRY_GRACE_S,)):
            n += self.retry_download(r["id"]) is not None
        return n

    def downloads_to_analyze(self) -> list[int]:
        return [r[0] for r in self.db.q(
            """SELECT c.id FROM candidates c WHERE c.state='downloaded' AND c.local_path IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM local_files l WHERE l.path=c.local_path)""")]

    # ================================================================ local files
    def scan_folders(self, folders: list[str], progress: Callable[[int, int, str], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> int:
        """Match audio files in folders (read-only) against tracks not yet in the library."""
        import mutagen
        update_signatures_if_new_day()
        tracks = self.db.q("SELECT * FROM tracks WHERE status!='in_library'")
        index: dict[str, list] = {}
        for t in tracks:
            words = norm(base_title(t["title"])).split()
            if words:
                index.setdefault(max(words, key=len), []).append(t)
        files = []
        for folder in folders:
            root = Path(folder).expanduser()
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTS and not p.name.startswith("."):
                    files.append(p)
        added = 0
        for i, p in enumerate(files):
            if cancelled and cancelled():
                break
            if progress and i % 10 == 0:
                progress(i, len(files), p.name)
            tag_artist = tag_title = tag_isrc = None
            dur = None
            try:
                mf = mutagen.File(str(p), easy=True)
                if mf is not None:
                    dur = getattr(mf.info, "length", None)
                    if mf.tags:
                        g = lambda k: (mf.tags.get(k) or [None])[0]  # noqa: E731
                        tag_artist, tag_title, tag_isrc = g("artist"), g("title"), g("isrc")
            except Exception:
                pass
            text_words = set(norm(f"{p.parent.name} {p.stem} {tag_artist or ''} {tag_title or ''}").split())
            cands = [t for w, ts in index.items() if w in text_words for t in ts]
            for t in cands:
                m = score_candidate(TrackRef.from_row(t), str(p), dur, tol=self.settings.duration_tolerance_s,
                                    tag_isrc=tag_isrc, tag_artist=tag_artist, tag_title=tag_title)
                if m.score < 50:
                    continue
                ext = p.suffix.lower().lstrip(".")
                size = p.stat().st_size
                qa = assess_reported(ext, size, None, dur)
                flags = (["SECURITY: " + r for r in name_risks(p.name)] + m.flags
                         + [f for f in qa.flags if "cannot be cross-checked" not in f])
                if any(str(p).startswith(str(Path(e).expanduser())) for e in self.settings.existing_collection if e):
                    flags.append("already in your existing collection")
                cur = self.db.exec(
                    """INSERT OR IGNORE INTO candidates(track_id, source, username, path, size, ext, duration_s, score,
                       score_detail, version, quality_tier, flags, local_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t["id"], "local", "", str(p), size, ext, dur, m.score, dumps(dict(m.detail, quality=qa.label)),
                     m.version, qa.tier, dumps(flags), str(p)))
                if cur:
                    added += 1
                    if t["status"] in ("new", "missing", "failed"):
                        self.db.exec("UPDATE tracks SET status='review' WHERE id=?", (t["id"],))
        if progress:
            progress(len(files), len(files), "done")
        log.info("Folder scan: %d files, %d new local candidates", len(files), added)
        return added

    def analyze_candidate(self, cid: int, spectral: bool = True) -> int:
        """Analyse the real file behind a local/downloaded candidate. Returns local_files id."""
        c = self.db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if c is None or not c["local_path"]:
            raise ValueError("candidate has no local file yet")
        t = self.db.one("SELECT * FROM tracks WHERE id=?", (c["track_id"],))
        expected = (t["duration_ms"] or 0) / 1000 or None
        # Security first: a file that fails the structural checks is never parsed further.
        sec = scan_file(c["local_path"])
        av = dumps({"threats": sec.threats, "notes": sec.notes, "engine": sec.engine})
        if sec.status in ("infected", "suspicious"):
            self.db.exec("DELETE FROM local_files WHERE path=?", (c["local_path"],))
            p = Path(c["local_path"])
            lid = self.db.exec(
                """INSERT INTO local_files(track_id, candidate_id, path, size, verdict, flags, av_status, av_detail,
                   analyzed_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (t["id"], cid, str(p), p.stat().st_size if p.exists() else None, "bad",
                 dumps(["SECURITY: " + x for x in sec.threats]), sec.status, av, now()))
            log.warning("Security scan rejected %s: %s", p, sec.threats)
            return lid
        a = analyze_file(c["local_path"], expected, spectral=spectral, tolerance_s=self.settings.duration_tolerance_s)
        # Re-score with tag evidence (ISRC, tag artist/title)
        m = score_candidate(TrackRef.from_row(t), c["local_path"], a.duration_s, tol=self.settings.duration_tolerance_s,
                            tag_isrc=a.tag_isrc, tag_artist=a.tag_artist, tag_title=a.tag_title)
        if a.tag_artist or a.tag_title:
            from rapidfuzz import fuzz
            if a.tag_title and fuzz.token_set_ratio(norm(base_title(t["title"])), norm(a.tag_title)) < 70:
                a.add("warn", f"title tag '{a.tag_title}' doesn't match the track")
        flags = a.flags
        self.db.exec("DELETE FROM local_files WHERE path=?", (a.path,))
        lid = self.db.exec(
            """INSERT INTO local_files(track_id, candidate_id, path, size, sha1, container, codec, bitrate, sample_rate,
               bit_depth, channels, duration_s, cutoff_hz, tag_artist, tag_title, tag_album, tag_isrc, verdict, flags,
               av_status, av_detail, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t["id"], cid, a.path, a.size, a.sha1, a.container, a.codec, a.bitrate, a.sample_rate, a.bit_depth,
             a.channels, a.duration_s, a.cutoff_hz, a.tag_artist, a.tag_title, a.tag_album, a.tag_isrc, a.verdict,
             dumps(flags), sec.status, av, now()))
        self.db.exec("UPDATE candidates SET score=?, duration_s=COALESCE(?, duration_s), bitrate=COALESCE(?, bitrate), "
                     "sample_rate=COALESCE(?, sample_rate), bit_depth=COALESCE(?, bit_depth) WHERE id=?",
                     (m.score, a.duration_s, a.bitrate, a.sample_rate, a.bit_depth, cid))
        if t["status"] not in ("in_library",):
            self.db.exec("UPDATE tracks SET status='downloaded' WHERE id=?", (t["id"],))
        self._auto_approve_file(lid, sec.status, a.verdict, m.score)
        return lid

    def local_file_for_candidate(self, cid: int):
        return self.db.one("SELECT * FROM local_files WHERE candidate_id=? ORDER BY id DESC", (cid,))

    def approve(self, local_file_id: int, genre_folder: str | None = None, allow_duplicate: bool = False) -> int:
        lid = lib.approve(self.db, self.settings, local_file_id, genre_folder=genre_folder,
                          allow_duplicate=allow_duplicate)
        tid = self.db.scalar("SELECT track_id FROM library WHERE id=?", (lid,))
        try:
            from .rekordbox import refresh_exports_for_track
            refresh_exports_for_track(self.db, self.settings, tid)
        except Exception:
            log.exception("could not add approved track to exported playlist folders")
        return lid

    # ================================================================ automatic approval (Settings toggle)
    def auto_mode_active(self) -> bool:
        s = self.settings
        return bool(s.auto_approve_best) and s.auto_basis in AUTH_BASES

    def auto_process_track(self, track_id: int) -> bool:
        """Auto mode: select the best match and queue its download under the standing authorisation."""
        if not self.auto_mode_active():
            return False
        t = self.db.one("SELECT status FROM tracks WHERE id=?", (track_id,))
        if t is None or t["status"] not in ("review", "selected"):
            return False
        c = self.best_download_candidate(track_id, self.settings.auto_min_score)
        if c is None:
            return False
        if self.authorization_for(c["id"]) is None:
            self.authorize(c["id"], self.settings.auto_basis,
                           (self.settings.auto_note + " " if self.settings.auto_note else "")
                           + "[standing authorisation from Settings → automatic approval]")
        self.set_candidate_state(c["id"], "selected")
        try:
            # searches were user-initiated, which already refreshed today's virus definitions
            self.request_download(c["id"], refresh_av=False)
        except Exception as e:
            log.warning("auto download of candidate %s not queued: %s", c["id"], e)
            return False
        log.info("Auto mode: queued best match for track %s (candidate %s, score %s)", track_id, c["id"], c["score"])
        return True

    def run_auto_sweep(self) -> int:
        """Apply auto mode to every track already waiting for review (e.g. right after it is switched on)."""
        if not self.auto_mode_active():
            return 0
        if not self.bridge.status().logged_in:
            return 0
        update_signatures_if_new_day()
        n = 0
        for r in self.db.q("SELECT id FROM tracks WHERE status IN ('review','selected')"):
            n += self.auto_process_track(r["id"])
        return n

    def _auto_approve_file(self, local_file_id: int, av_status: str, verdict: str, score: int) -> None:
        """Auto mode: approve a downloaded file only if it is clean, warning-free and a strong match."""
        if not self.auto_mode_active():
            return
        if av_status != "clean" or verdict != "ok" or (score or 0) < self.settings.auto_min_score:
            log.info("Auto mode: file %s left for manual review (scan %s, quality %s, match %s)",
                     local_file_id, av_status, verdict, score)
            return
        try:
            self.approve(local_file_id)  # re-scans for viruses; raises SecurityBlocked unless clean
            log.info("Auto mode: approved file %s into the library", local_file_id)
        except lib.LibraryError as e:
            log.info("Auto mode: file %s not approved: %s", local_file_id, e)

    # ================================================================ bulk download
    def best_download_candidate(self, track_id: int, min_score: int = 85, exclude_users: set | None = None):
        """Best Soulseek candidate to fetch for a track: high match, no security/version problems, best quality."""
        rows = self.db.q("""SELECT * FROM candidates WHERE track_id=? AND source='soulseek' AND dup_of IS NULL
                            AND state IN ('new','selected') AND score>=? ORDER BY (state='selected') DESC,
                            score DESC, quality_tier ASC, free_slot DESC, queue_len ASC, upload_speed DESC""",
                         (track_id, min_score))
        # Prefer regular sharers over accounts named after websites (promo/repost bots), at equal eligibility.
        rows = sorted(rows, key=lambda r: bool(_WEBSITE_USER.search(r["username"] or "")))
        for r in rows:
            flags = loads(r["flags"], []) or []
            if any(f.startswith("SECURITY") or f.startswith("private") or "implies only" in f for f in flags):
                continue
            if exclude_users and r["username"] in exclude_users:
                continue
            return r
        return None

    def bulk_authorize_download(self, track_ids: list[int], basis: str, note: str = "") -> dict:
        """Record one authorisation for the best candidate of each track and queue the downloads."""
        update_signatures_if_new_day()
        done, skipped = [], []
        for tid in track_ids:
            t = self.db.one("SELECT status FROM tracks WHERE id=?", (tid,))
            if t is None or t["status"] in ("in_library", "downloading", "downloaded"):
                skipped.append(tid)
                continue
            c = self.best_download_candidate(tid)
            if c is None:
                skipped.append(tid)
                continue
            self.authorize(c["id"], basis, note)
            self.request_download(c["id"], refresh_av=False)
            done.append(c["id"])
        return {"queued": done, "skipped": skipped}

    def candidates_for(self, track_id: int, include_hidden: bool = False):
        sql = "SELECT * FROM candidates WHERE track_id=?"
        params: list = [track_id]
        if not include_hidden:
            sql += " AND (score>=? OR state!='new') AND dup_of IS NULL"
            params.append(self.settings.min_match_score)
        sql += " ORDER BY (state='selected') DESC, score DESC, quality_tier ASC, free_slot DESC, queue_len ASC"
        return self.db.q(sql, params)


def flags_of(row) -> list[str]:
    return loads(row["flags"], []) or []


def wait_for(cond: Callable[[], bool], timeout: float, step: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()
