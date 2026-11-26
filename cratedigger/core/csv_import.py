"""Import Spotify playlist CSV exports (Exportify-style column names)."""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .genres import genre_folder_for
from .text import build_queries

log = logging.getLogger(__name__)

# Column name aliases -> internal field. Case-insensitive.
COLUMNS = {
    "track uri": "spotify_uri", "spotify id": "spotify_uri", "uri": "spotify_uri",
    "isrc": "isrc",
    "track name": "title", "name": "title", "title": "title",
    "album name": "album", "album": "album",
    "artist name(s)": "artists", "artist name": "artists", "artists": "artists", "artist": "artists",
    "release date": "release_date",
    "duration (ms)": "duration_ms", "duration_ms": "duration_ms",
    "popularity": "popularity",
    "explicit": "explicit",
    "added at": "added_at",
    "genres": "genres", "artist genres": "genres",
    "record label": "label", "label": "label",
    "danceability": "danceability",
    "energy": "energy",
    "key": "musical_key",
    "mode": "mode",
    "tempo": "tempo", "bpm": "tempo",
    "time signature": "time_signature",
}
INT_FIELDS = {"duration_ms", "popularity", "musical_key", "mode", "time_signature"}
FLOAT_FIELDS = {"tempo", "energy", "danceability"}


@dataclass
class ImportResult:
    playlist_id: int
    added: int
    updated: int
    skipped: int
    errors: list[str]


def _conv(field: str, raw: str):
    raw = (raw or "").strip()
    if raw == "":
        return None
    if field in INT_FIELDS:
        try:
            return int(float(raw))
        except ValueError:
            return None
    if field in FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError:
            return None
    if field == "explicit":
        return 1 if raw.lower() in ("true", "1", "yes") else 0
    return raw


def read_csv(path: Path) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        mapping = {h: COLUMNS.get(h.strip().lower()) for h in reader.fieldnames}
        if "title" not in mapping.values() or "artists" not in mapping.values():
            raise ValueError("CSV needs at least 'Track Name' and 'Artist Name(s)' columns; got: "
                             + ", ".join(reader.fieldnames))
        for i, raw in enumerate(reader, start=2):
            rec: dict = {}
            for h, f in mapping.items():
                if f and f not in rec:
                    rec[f] = _conv(f, raw.get(h, ""))
            if not rec.get("title") or not rec.get("artists"):
                errors.append(f"line {i}: missing title or artist, skipped")
                continue
            rows.append(rec)
    return rows, errors


def import_csv(db: Database, path: Path | str, settings, playlist_name: str | None = None,
               reuse_playlist: bool = False) -> ImportResult:
    path = Path(path)
    rows, errors = read_csv(path)
    name = playlist_name or path.stem
    added = updated = 0
    with db.tx() as c:
        prev = c.execute("SELECT id FROM playlists WHERE name=? ORDER BY id LIMIT 1", (name,)).fetchone() if reuse_playlist else None
        if prev:  # sent again from another app: add what is new to the same playlist
            pid = prev[0]
            start = (c.execute("SELECT MAX(position) FROM playlist_tracks WHERE playlist_id=?", (pid,)).fetchone()[0] or -1) + 1
        else:
            pid = c.execute("INSERT INTO playlists(name, source_path) VALUES (?,?)", (name, str(path))).lastrowid
            start = 0
        for pos, rec in enumerate(rows, start=start):
            rec["genre_folder"] = genre_folder_for(rec.get("genres") or "", settings.genre_rules)
            rec["search_query"] = build_queries(rec["title"], rec["artists"])[0]
            existing = None
            if rec.get("spotify_uri"):
                existing = c.execute("SELECT id FROM tracks WHERE spotify_uri=?", (rec["spotify_uri"],)).fetchone()
            if existing is None and rec.get("isrc"):
                existing = c.execute("SELECT id FROM tracks WHERE isrc=? AND title=?", (rec["isrc"], rec["title"])).fetchone()
            if existing:
                tid = existing[0]
                cols = [k for k in rec if k not in ("search_query",)]
                c.execute(f"UPDATE tracks SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                          [rec[k] for k in cols] + [tid])
                updated += 1
            else:
                cols = list(rec)
                tid = c.execute(f"INSERT INTO tracks({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                [rec[k] for k in cols]).lastrowid
                added += 1
            c.execute("INSERT OR IGNORE INTO playlist_tracks(playlist_id, track_id, position) VALUES (?,?,?)",
                      (pid, tid, pos))
    log.info("Imported %s: %d added, %d updated, %d skipped", path.name, added, updated, len(errors))
    for e in errors:
        log.warning("Import %s: %s", path.name, e)
    return ImportResult(pid, added, updated, len(errors), errors)
