"""SQLite storage. One connection per thread; WAL mode so the UI can read while workers write."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source_path TEXT,
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    spotify_uri TEXT UNIQUE,
    isrc TEXT,
    title TEXT NOT NULL,
    artists TEXT NOT NULL,          -- ';' separated, as in the Spotify export
    album TEXT,
    release_date TEXT,
    duration_ms INTEGER,
    popularity INTEGER,
    explicit INTEGER,
    genres TEXT,                    -- ',' separated
    label TEXT,
    tempo REAL,
    musical_key INTEGER,
    mode INTEGER,
    energy REAL,
    danceability REAL,
    time_signature INTEGER,
    added_at TEXT,
    genre_folder TEXT,
    search_query TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    backend TEXT NOT NULL,
    status TEXT NOT NULL,           -- queued | sent | running | done | failed | cancelled
    request_id TEXT UNIQUE,
    result_count INTEGER DEFAULT 0,
    error TEXT,
    queued_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_searches_track ON searches(track_id);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL,
    source TEXT NOT NULL,           -- soulseek | local
    username TEXT,
    path TEXT NOT NULL,             -- virtual path (soulseek) or absolute path (local)
    size INTEGER,
    ext TEXT,
    bitrate INTEGER,
    vbr INTEGER,
    duration_s REAL,
    sample_rate INTEGER,
    bit_depth INTEGER,
    free_slot INTEGER,
    upload_speed INTEGER,
    queue_len INTEGER,
    score INTEGER,
    score_detail TEXT,              -- JSON
    version TEXT,
    quality_tier INTEGER,
    flags TEXT,                     -- JSON list
    dup_of INTEGER,
    state TEXT NOT NULL DEFAULT 'new',  -- new | selected | rejected | authorized | queued_download | downloading | downloaded | failed
    dl_status TEXT,
    dl_percent INTEGER,
    dl_failed_at REAL,
    local_path TEXT,
    found_at TEXT DEFAULT (datetime('now')),
    UNIQUE (track_id, source, username, path)
);
CREATE INDEX IF NOT EXISTS idx_cand_track ON candidates(track_id);

CREATE TABLE IF NOT EXISTS authorizations (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    basis TEXT NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS local_files (
    id INTEGER PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    path TEXT UNIQUE NOT NULL,
    size INTEGER,
    sha1 TEXT,
    container TEXT,
    codec TEXT,
    bitrate INTEGER,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER,
    duration_s REAL,
    cutoff_hz INTEGER,
    tag_artist TEXT,
    tag_title TEXT,
    tag_album TEXT,
    tag_isrc TEXT,
    verdict TEXT,                   -- ok | warn | bad
    flags TEXT,                     -- JSON list
    av_status TEXT,                 -- clean | infected | suspicious | unscanned | error
    av_detail TEXT,                 -- JSON {threats, notes, engine}
    analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS playlist_exports (
    playlist_id INTEGER PRIMARY KEY REFERENCES playlists(id) ON DELETE CASCADE,
    folder TEXT NOT NULL,
    exported_at TEXT
);
CREATE TABLE IF NOT EXISTS exported_files (
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    library_id INTEGER REFERENCES library(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    PRIMARY KEY (playlist_id, library_id)
);

CREATE TABLE IF NOT EXISTS library (
    id INTEGER PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    local_file_id INTEGER REFERENCES local_files(id) ON DELETE SET NULL,
    library_path TEXT UNIQUE NOT NULL,
    source_path TEXT,
    genre_folder TEXT,
    artist TEXT, title TEXT, album TEXT, label TEXT, year TEXT,
    bpm REAL, musical_key TEXT, isrc TEXT, genre TEXT,
    codec TEXT, bitrate INTEGER, sample_rate INTEGER, duration_s REAL, size INTEGER, sha1 TEXT,
    tags_written INTEGER DEFAULT 0,
    approved_at TEXT DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (idempotent)."""
        wanted = {
            "local_files": [("av_status", "TEXT"), ("av_detail", "TEXT")],
            "candidates": [("dl_status", "TEXT"), ("dl_percent", "INTEGER"), ("local_path", "TEXT"),
                           ("dl_failed_at", "REAL")],
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn().execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.conn().execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # --- helpers -------------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        r = self.one(sql, params)
        return r[0] if r else None

    def exec(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            cur = self.conn().execute(sql, tuple(params))
            return cur.lastrowid or cur.rowcount

    def tx(self):
        """Context manager for a write transaction."""
        db = self

        class _Tx:
            def __enter__(self_inner):
                db._write_lock.acquire()
                db.conn().execute("BEGIN IMMEDIATE")
                return db.conn()

            def __exit__(self_inner, et, ev, tb):
                try:
                    db.conn().execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db._write_lock.release()
                return False

        return _Tx()


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
