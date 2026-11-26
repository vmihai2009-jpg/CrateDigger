"""The organised CrateDigger library.

Rules
  * Approving a file COPIES it into <library_root>/<Genre>/<Artist - Title>.<ext>.
    The source file is never moved, renamed, re-tagged or deleted.
  * An existing file in the library is never overwritten; a numbered name is used instead.
  * Tags are written to the library copy only.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .db import Database
from .text import camelot, clean_filename, key_name, norm, split_artists

log = logging.getLogger(__name__)


class LibraryError(Exception):
    pass


class SecurityBlocked(LibraryError):
    """Raised when a file has not passed the mandatory virus/malware scan. Never overridable."""


def display_artist(artists: str) -> str:
    return ", ".join(split_artists(artists))


def target_path(root: Path, genre_folder: str, artist: str, title: str, ext: str, pattern: str) -> Path:
    name = pattern.format(artist=artist, title=title)
    folder = root / clean_filename(genre_folder or "Unsorted")
    return folder / (clean_filename(name) + "." + ext.lower().lstrip("."))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
        i += 1


def safe_copy(src: Path, dst: Path) -> Path:
    """Copy without ever overwriting. Returns the final destination."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = _unique(dst)
    tmp = dst.with_name("." + dst.name + ".cdpart")
    # On APFS `cp -c` makes a copy-on-write clone: an independent file that uses no extra space until changed.
    if subprocess.run(["/bin/cp", "-c", "-p", str(src), str(tmp)], capture_output=True).returncode != 0:
        shutil.copy2(src, tmp)
    try:
        # os.link fails if dst appeared meanwhile, which guarantees no overwrite
        os.link(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


# --------------------------------------------------------------------------- tags
def write_tags(path: Path, meta: dict) -> None:
    """Write artist/title/album/genre/bpm/key/isrc/label/year to `path` (a library copy)."""
    import mutagen
    from mutagen.easyid3 import EasyID3
    from mutagen.easymp4 import EasyMP4Tags

    for k, frame in (("initialkey", "TKEY"), ("label", "TPUB"), ("comment_cd", "COMM")):
        if k not in EasyID3.valid_keys:
            if frame == "COMM":
                continue
            EasyID3.RegisterTextKey(k, frame)
    ext = path.suffix.lower()
    vals = {
        "artist": meta.get("artist"), "title": meta.get("title"), "album": meta.get("album"),
        "genre": meta.get("genre"), "isrc": meta.get("isrc"), "date": meta.get("year"),
    }
    bpm = meta.get("bpm")
    if bpm:
        vals["bpm"] = str(int(round(float(bpm))))
    vals = {k: v for k, v in vals.items() if v}

    if ext in (".mp3", ".wav", ".aiff", ".aif"):
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            f = MP3(str(path), ID3=EasyID3)
        elif ext == ".wav":
            from mutagen.wave import WAVE
            f = WAVE(str(path))
        else:
            from mutagen.aiff import AIFF
            f = AIFF(str(path))
        if f.tags is None:
            f.add_tags()
        if ext == ".mp3":
            for k, v in vals.items():
                f[k] = v
            if meta.get("key"):
                f["initialkey"] = meta["key"]
            if meta.get("label"):
                f["label"] = meta["label"]
            f.save()
        else:
            from mutagen.id3 import TALB, TBPM, TCON, TDRC, TIT2, TKEY, TPE1, TPUB, TSRC
            frames = {"artist": TPE1, "title": TIT2, "album": TALB, "genre": TCON, "date": TDRC, "isrc": TSRC, "bpm": TBPM}
            for k, v in vals.items():
                f.tags.add(frames[k](encoding=3, text=[v]))
            if meta.get("key"):
                f.tags.add(TKEY(encoding=3, text=[meta["key"]]))
            if meta.get("label"):
                f.tags.add(TPUB(encoding=3, text=[meta["label"]]))
            f.save()
    elif ext == ".flac" or ext == ".ogg":
        f = mutagen.File(str(path))
        if f.tags is None:
            f.add_tags()
        for k, v in vals.items():
            f.tags[k.upper()] = [v]
        if meta.get("key"):
            f.tags["INITIALKEY"] = [meta["key"]]
        if meta.get("label"):
            f.tags["LABEL"] = [meta["label"]]
        f.save()
    elif ext in (".m4a", ".mp4", ".aac"):
        from mutagen.easymp4 import EasyMP4
        f = EasyMP4(str(path))
        for k, v in vals.items():
            if k in EasyMP4Tags.Get:  # EasyMP4 supports a fixed key set (no isrc)
                f[k] = v
        f.save()
    else:
        raise LibraryError(f"tagging not supported for {ext}")


# --------------------------------------------------------------------------- approve
def approve(db: Database, settings, local_file_id: int, *, genre_folder: str | None = None,
            allow_duplicate: bool = False) -> int:
    lf = db.one("SELECT * FROM local_files WHERE id=?", (local_file_id,))
    if lf is None:
        raise LibraryError("file not found in database")
    src = Path(lf["path"])
    if not src.exists():
        raise LibraryError(f"source file no longer exists: {src}")
    # Mandatory virus/malware gate: re-scan now (the file may have changed since analysis).
    from .db import dumps
    from .security import scan_file
    sec = scan_file(src)
    db.exec("UPDATE local_files SET av_status=?, av_detail=? WHERE id=?",
            (sec.status, dumps({"threats": sec.threats, "notes": sec.notes, "engine": sec.engine}), lf["id"]))
    if sec.status != "clean":
        reason = "; ".join(sec.threats + sec.notes) or sec.status
        raise SecurityBlocked(f"Security scan {sec.status.upper()} — not added to the library: {reason}")
    tr = db.one("SELECT * FROM tracks WHERE id=?", (lf["track_id"],)) if lf["track_id"] else None
    if tr is None:
        raise LibraryError("file is not linked to a playlist track")
    dups = find_duplicates_for(db, sha1=lf["sha1"], isrc=tr["isrc"], track_id=tr["id"],
                               artist=tr["artists"], title=tr["title"])
    if dups and not allow_duplicate:
        raise LibraryError("already in library: " + "; ".join(d["library_path"] for d in dups))

    root = Path(settings.library_root).expanduser()
    folder = genre_folder or tr["genre_folder"] or "Unsorted"
    artist = display_artist(tr["artists"])
    title = tr["title"]
    dst = target_path(root, folder, artist, title, src.suffix, settings.filename_pattern)
    final = safe_copy(src, dst)
    year = (tr["release_date"] or "")[:4]
    genre = (tr["genres"] or "").split(",")[0].strip().title() if tr["genres"] else folder
    meta = {"artist": artist, "title": title, "album": tr["album"], "genre": genre, "bpm": tr["tempo"],
            "key": key_name(tr["musical_key"], tr["mode"]), "isrc": tr["isrc"], "label": tr["label"], "year": year}
    tags_ok = 0
    try:
        write_tags(final, meta)
        tags_ok = 1
    except Exception as e:
        log.warning("tagging failed for %s: %s", final, e)
    lid = db.exec(
        """INSERT INTO library(track_id, local_file_id, library_path, source_path, genre_folder, artist, title, album,
           label, year, bpm, musical_key, isrc, genre, codec, bitrate, sample_rate, duration_s, size, sha1, tags_written)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tr["id"], lf["id"], str(final), str(src), folder, artist, title, tr["album"], tr["label"], year,
         tr["tempo"], meta["key"], tr["isrc"], genre, lf["codec"], lf["bitrate"], lf["sample_rate"],
         lf["duration_s"], lf["size"], lf["sha1"], tags_ok))
    db.exec("UPDATE tracks SET status='in_library' WHERE id=?", (tr["id"],))
    log.info("Approved %s -> %s", src, final)
    return lid


def find_duplicates_for(db: Database, *, sha1=None, isrc=None, track_id=None, artist=None, title=None) -> list:
    rows = []
    seen = set()

    def add(rs):
        for r in rs:
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)

    if sha1:
        add(db.q("SELECT * FROM library WHERE sha1=?", (sha1,)))
    if track_id:
        add(db.q("SELECT * FROM library WHERE track_id=?", (track_id,)))
    if isrc:
        add(db.q("SELECT * FROM library WHERE isrc=?", (isrc,)))
    if artist and title:
        key = norm(display_artist(artist)) + "|" + norm(title)
        for r in db.q("SELECT * FROM library"):
            if norm(r["artist"]) + "|" + norm(r["title"]) == key:
                add([r])
    return rows


def duplicate_groups(db: Database) -> dict[int, int]:
    """library id -> group number, for entries sharing a hash, ISRC or normalised artist+title."""
    rows = db.q("SELECT id, sha1, isrc, artist, title FROM library ORDER BY id")
    parent: dict[int, int] = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    keys: dict[str, int] = {}
    for r in rows:
        for k in (("h", r["sha1"]), ("i", r["isrc"]), ("n", norm(r["artist"]) + "|" + norm(r["title"]))):
            if not k[1] or k[1] == "|":
                continue
            ks = k[0] + k[1]
            if ks in keys:
                parent[find(r["id"])] = find(keys[ks])
            else:
                keys[ks] = r["id"]
    groups: dict[int, list[int]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r["id"])
    out = {}
    n = 0
    for members in groups.values():
        if len(members) > 1:
            n += 1
            for m in members:
                out[m] = n
    return out


def update_entry(db: Database, settings, library_id: int, changes: dict) -> None:
    """Edit metadata (and optionally genre folder) of a library entry; re-tags and moves the library copy."""
    row = db.one("SELECT * FROM library WHERE id=?", (library_id,))
    if row is None:
        raise LibraryError("library entry not found")
    allowed = {"artist", "title", "album", "label", "year", "bpm", "musical_key", "isrc", "genre", "genre_folder"}
    changes = {k: v for k, v in changes.items() if k in allowed}
    if not changes:
        return
    new = dict(row)
    new.update(changes)
    path = Path(row["library_path"])
    root = Path(settings.library_root).expanduser()
    if not str(path.resolve()).startswith(str(root.resolve())):
        raise LibraryError("refusing to modify a file outside the CrateDigger library")
    if any(k in changes for k in ("artist", "title", "genre_folder")):
        dst = target_path(root, new["genre_folder"], new["artist"], new["title"], path.suffix, settings.filename_pattern)
        if dst != path:
            dst = _unique(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(path, dst)  # our own copy, inside the library
            path = dst
            new["library_path"] = str(dst)
    write_tags(path, {"artist": new["artist"], "title": new["title"], "album": new["album"], "genre": new["genre"],
                      "bpm": new["bpm"], "key": new["musical_key"], "isrc": new["isrc"], "label": new["label"],
                      "year": new["year"]})
    cols = list(changes) + (["library_path"] if new["library_path"] != row["library_path"] else [])
    db.exec(f"UPDATE library SET {', '.join(c + '=?' for c in cols)}, tags_written=1 WHERE id=?",
            [new[c] for c in cols] + [library_id])


def remove_entry(db: Database, settings, library_id: int, delete_copy: bool = False) -> None:
    row = db.one("SELECT * FROM library WHERE id=?", (library_id,))
    if row is None:
        return
    if delete_copy:
        p = Path(row["library_path"])
        root = Path(settings.library_root).expanduser().resolve()
        if str(p.resolve()).startswith(str(root)) and p.exists():
            p.unlink()  # only ever our own library copy
    db.exec("DELETE FROM library WHERE id=?", (library_id,))
    if row["track_id"]:
        db.exec("UPDATE tracks SET status='downloaded' WHERE id=? AND status='in_library'", (row["track_id"],))


__all__ = ["approve", "update_entry", "remove_entry", "duplicate_groups", "find_duplicates_for", "write_tags",
           "LibraryError", "camelot"]
