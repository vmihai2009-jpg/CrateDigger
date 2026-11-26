"""Automatic review: pick the best match per track and write a reference report.

Selecting is not downloading: the chosen candidate is marked 'selected'; downloads still need an authorisation
basis recorded by the user (bulk dialog). The report is a self-contained HTML page saved next to the playlist's
exported files, so the decision can be checked later (Spotify preview link, chosen file, alternatives, flags).
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from pathlib import Path

from .db import Database, loads

log = logging.getLogger(__name__)
AUTO_SELECT_MIN = 85


def spotify_url(uri: str | None) -> str:
    if uri and uri.startswith("spotify:track:"):
        return "https://open.spotify.com/track/" + uri.rsplit(":", 1)[1]
    return ""


def review_playlist(svc, playlist_id: int) -> dict:
    db: Database = svc.db
    tids = [r[0] for r in db.q("SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position", (playlist_id,))]
    picked, manual, missing, kept = [], [], [], []
    for tid in tids:
        t = db.one("SELECT * FROM tracks WHERE id=?", (tid,))
        if t["status"] in ("in_library", "downloading", "downloaded"):
            kept.append(tid)
            continue
        if db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=? AND state='selected'", (tid,)):
            kept.append(tid)  # the user already chose; never override a manual choice
            continue
        best = svc.best_download_candidate(tid, AUTO_SELECT_MIN)
        if best is not None:
            svc.set_candidate_state(best["id"], "selected")
            picked.append(tid)
        elif db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=? AND score>=?", (tid, svc.settings.min_match_score)):
            manual.append(tid)
        else:
            missing.append(tid)
    log.info("Auto-review playlist %s: %d selected, %d need a manual look, %d missing, %d unchanged",
             playlist_id, len(picked), len(manual), len(missing), len(kept))
    report = write_report(svc, playlist_id)
    return {"selected": len(picked), "manual": len(manual), "missing": len(missing), "unchanged": len(kept),
            "report": str(report)}


def _fmt_dur(s):
    if not s:
        return ""
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def write_report(svc, playlist_id: int) -> Path:
    from .rekordbox import playlist_folder
    db = svc.db
    pl = db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    folder = playlist_folder(svc.settings, pl["name"])
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    counts = {"selected": 0, "manual": 0, "missing": 0, "library": 0}
    for t in db.q("""SELECT t.* FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id
                     WHERE pt.playlist_id=? ORDER BY pt.position""", (playlist_id,)):
        sel = db.one("SELECT * FROM candidates WHERE track_id=? AND state NOT IN ('new','rejected') "
                     "ORDER BY (state='selected') DESC, score DESC LIMIT 1", (t["id"],))
        n_good = db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=? AND score>=?", (t["id"], AUTO_SELECT_MIN))
        best_any = db.one("SELECT * FROM candidates WHERE track_id=? ORDER BY score DESC LIMIT 1", (t["id"],))
        lib = db.one("SELECT * FROM library WHERE track_id=?", (t["id"],))
        if lib:
            state, cls = "In library", "ok"
            counts["library"] += 1
        elif sel:
            state, cls = {"selected": "Selected", "authorized": "Authorised", "queued_download": "Queued",
                          "downloading": "Downloading", "downloaded": "Downloaded", "failed": "Download failed"}.get(
                sel["state"], sel["state"]), "sel"
            counts["selected"] += 1
        elif best_any and best_any["score"] >= svc.settings.min_match_score:
            state, cls = "Check manually", "warn"
            counts["manual"] += 1
        else:
            state, cls = "Not found", "bad"
            counts["missing"] += 1
        c = sel or (best_any if cls == "warn" else None)
        d = loads(c["score_detail"], {}) if c else {}
        flags = loads(c["flags"], []) if c else []
        sp = spotify_url(t["spotify_uri"])
        rows.append(f"""<tr class="{cls}">
<td><b>{html.escape(t['artists'].replace(';', ', '))}</b><br>{html.escape(t['title'])}
<div class="m">{html.escape(t['album'] or '')} · {_fmt_dur((t['duration_ms'] or 0) / 1000)} · {t['tempo'] or 0:.0f} BPM · {html.escape(t['genre_folder'] or '')}</div></td>
<td>{f'<a href="{sp}">▶ Spotify</a>' if sp else ''}</td>
<td><span class="st {cls}">{state}</span></td>
<td>{c['score'] if c else ''}</td>
<td>{html.escape(d.get('quality') or (c['ext'].upper() if c else ''))}</td>
<td>{_fmt_dur(c['duration_s']) if c else ''}{f" ({d['duration_diff_s']:+.0f}s)" if c and d.get('duration_diff_s') not in (None, 0) and abs(d['duration_diff_s']) >= 1 else ''}</td>
<td class="p">{html.escape((c['path'] or '').replace(chr(92), '/').split('/')[-1]) if c else ''}<div class="m">{html.escape(c['username'] or 'local file') if c else ''}{f' · {n_good} alternatives ≥{AUTO_SELECT_MIN}' if c and n_good > 1 else ''}</div></td>
<td class="f">{'<br>'.join(html.escape(f) for f in flags)}</td></tr>""")
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(pl['name'])} — CrateDigger review</title>
<style>
body{{background:#121314;color:#E4E6E8;font:13px -apple-system,Helvetica,sans-serif;margin:24px}}
h1{{font-size:20px;margin:0}} .sub{{color:#8B9096;margin:4px 0 16px}}
.sum span{{display:inline-block;margin-right:18px}} .sum b{{color:#B5F23D;font-size:18px}}
table{{border-collapse:collapse;width:100%;margin-top:14px}} th{{text-align:left;color:#8B9096;font-weight:500;font-size:11px;border-bottom:1px solid #2E3135;padding:6px}}
td{{border-bottom:1px solid #222427;padding:6px;vertical-align:top}} .m{{color:#8B9096;font-size:11px}} .p{{max-width:340px;word-break:break-word}}
.f{{color:#F2B33D;font-size:11px;max-width:260px}} a{{color:#B5F23D;text-decoration:none}}
.st{{padding:1px 6px;border-radius:3px;font-size:11px;white-space:nowrap}} .st.ok{{background:#2F3A1C;color:#B5F23D}}
.st.sel{{background:#2E2440;color:#C99BF2}} .st.warn{{background:#3A2E14;color:#F2B33D}} .st.bad{{background:#3A1A14;color:#F2553D}}
</style></head><body>
<h1>{html.escape(pl['name'])}</h1>
<div class="sub">CrateDigger review · {when} · auto-selection needs match ≥ {AUTO_SELECT_MIN}, no security/size flags; best quality, then free slot, then short queue.
Selected ≠ downloaded: downloads need your recorded authorisation.</div>
<div class="sum"><span><b>{counts['selected']}</b> selected</span><span><b>{counts['library']}</b> in library</span>
<span><b>{counts['manual']}</b> check manually</span><span><b>{counts['missing']}</b> not found</span></div>
<table><tr><th>Track (Spotify reference)</th><th>Preview</th><th>State</th><th>Match</th><th>Quality (reported)</th><th>Length</th><th>Chosen file</th><th>Notes</th></tr>
{''.join(rows)}</table></body></html>"""
    out = folder / "CrateDigger review.html"
    out.write_text(doc, "utf-8")
    return out
