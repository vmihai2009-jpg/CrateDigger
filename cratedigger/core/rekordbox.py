"""Rekordbox export via the documented rekordbox XML format + M3U8 playlists.

Rekordbox's own database (master.db) is never touched. Import the XML in
rekordbox: Preferences → Advanced → Database → "rekordbox xml" → choose
CrateDigger.xml, then the playlists appear under "rekordbox xml" in the tree.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from .db import Database

log = logging.getLogger(__name__)

KINDS = {".mp3": "MP3 File", ".flac": "FLAC File", ".wav": "WAV File", ".aiff": "AIFF File", ".aif": "AIFF File",
         ".m4a": "M4A File", ".aac": "M4A File"}


def location_uri(path: str) -> str:
    return "file://localhost" + quote(str(Path(path).resolve()), safe="/")


def export(db: Database, settings, out_dir: Path | None = None, include_spotify_bpm: bool = False) -> dict:
    root = Path(settings.library_root).expanduser()
    out_dir = Path(out_dir) if out_dir else root / "_Rekordbox"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = db.q("""SELECT l.*, t.popularity, t.added_at, t.id AS tid FROM library l
                   LEFT JOIN tracks t ON t.id = l.track_id ORDER BY l.genre_folder, l.artist, l.title""")
    rows = [r for r in rows if Path(r["library_path"]).exists()]
    missing = db.scalar("SELECT COUNT(*) FROM library") - len(rows)

    dj = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(dj, "PRODUCT", Name="CrateDigger", Version="0.1.0", Company="")
    coll = ET.SubElement(dj, "COLLECTION", Entries=str(len(rows)))
    ids: dict[int, int] = {}
    for i, r in enumerate(rows, start=1):
        ids[r["id"]] = i
        p = Path(r["library_path"])
        attrs = {
            "TrackID": str(i), "Name": r["title"] or "", "Artist": r["artist"] or "", "Album": r["album"] or "",
            "Genre": r["genre"] or "", "Kind": KINDS.get(p.suffix.lower(), "Audio File"),
            "Size": str(r["size"] or p.stat().st_size), "TotalTime": str(int(round(r["duration_s"] or 0))),
            "Year": r["year"] or "", "Label": r["label"] or "", "Location": location_uri(str(p)),
            "DateAdded": (r["approved_at"] or dt.date.today().isoformat())[:10],
            "BitRate": str(r["bitrate"] or 0), "SampleRate": str(r["sample_rate"] or 0),
            "Comments": f"CrateDigger ISRC {r['isrc']}" if r["isrc"] else "CrateDigger",
        }
        if include_spotify_bpm and r["bpm"]:
            attrs["AverageBpm"] = f"{float(r['bpm']):.2f}"
            if r["musical_key"]:
                attrs["Tonality"] = r["musical_key"]
        ET.SubElement(coll, "TRACK", **attrs)

    pls = ET.SubElement(dj, "PLAYLISTS")
    rootnode = ET.SubElement(pls, "NODE", Type="0", Name="ROOT")
    cd = ET.SubElement(rootnode, "NODE", Type="0", Name="CrateDigger")
    groups: list[tuple[str, list]] = []
    by_genre: dict[str, list] = {}
    for r in rows:
        by_genre.setdefault(r["genre_folder"] or "Unsorted", []).append(r)
    genre_node = ET.SubElement(cd, "NODE", Type="0", Name="Genres")
    for g in sorted(by_genre):
        groups.append((f"Genres/{g}", by_genre[g]))
        _playlist(genre_node, g, by_genre[g], ids)
    genre_node.set("Count", str(len(by_genre)))
    pl_node = ET.SubElement(cd, "NODE", Type="0", Name="Spotify playlists")
    n_pl = 0
    for pl in db.q("SELECT * FROM playlists ORDER BY id"):
        tids = [x[0] for x in db.q("SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position", (pl["id"],))]
        members = [r for tid in tids for r in rows if r["tid"] == tid]
        if members:
            n_pl += 1
            name = f"{pl['name']} ({pl['id']})"
            groups.append((f"Playlists/{name}", members))
            _playlist(pl_node, name, members, ids)
    pl_node.set("Count", str(n_pl))
    cd.set("Count", "2")
    rootnode.set("Count", "1")

    xml_path = out_dir / "CrateDigger.xml"
    tree = ET.ElementTree(dj)
    ET.indent(tree, space="  ")
    tmp = xml_path.with_suffix(".xml.tmp")
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    tmp.replace(xml_path)

    m3u_dir = out_dir / "M3U8"
    written = []
    for name, members in groups:
        p = m3u_dir / (name.replace(":", " ") + ".m3u8")
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U"]
        for r in members:
            lines.append(f"#EXTINF:{int(round(r['duration_s'] or 0))},{r['artist']} - {r['title']}")
            lines.append(str(Path(r["library_path"]).resolve()))
        p.write_text("\n".join(lines) + "\n", "utf-8")
        written.append(str(p))
    log.info("Rekordbox export: %d tracks -> %s (%d playlists)", len(rows), xml_path, len(written))
    return {"xml": str(xml_path), "tracks": len(rows), "playlists": written, "missing_files": missing}


def _playlist(parent, name: str, members: list, ids: dict[int, int]) -> None:
    node = ET.SubElement(parent, "NODE", Name=name, Type="1", KeyType="0", Entries=str(len(members)))
    for r in members:
        ET.SubElement(node, "TRACK", Key=str(ids[r["id"]]))


# =========================================================================== per-playlist export
def playlist_folder(settings, name: str) -> Path:
    from .text import clean_filename
    return Path(settings.playlist_export_root).expanduser() / clean_filename(name)


def export_playlist(db: Database, settings, playlist_id: int) -> dict:
    """Copy the playlist's approved tracks to <export_root>/<playlist name>/ and rebuild the combined XML.

    Files are APFS clones of the library copies (no extra disk space, independent files). Existing files in the
    folder are never overwritten; tracks already exported are skipped.
    """
    from .library import safe_copy
    from .text import clean_filename

    pl = db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    if pl is None:
        raise ValueError("playlist not found")
    folder = playlist_folder(settings, pl["name"])
    folder.mkdir(parents=True, exist_ok=True)
    total = db.scalar("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
    rows = db.q("""SELECT l.*, pt.position FROM playlist_tracks pt JOIN library l ON l.track_id = pt.track_id
                   WHERE pt.playlist_id=? ORDER BY pt.position""", (playlist_id,))
    copied = skipped = 0
    for r in rows:
        src = Path(r["library_path"])
        prev = db.one("SELECT path FROM exported_files WHERE playlist_id=? AND library_id=?", (playlist_id, r["id"]))
        if prev and Path(prev["path"]).exists():
            skipped += 1
            continue
        if not src.exists():
            continue
        dst = safe_copy(src, folder / clean_filename(src.name))
        db.exec("INSERT OR REPLACE INTO exported_files(playlist_id, library_id, path) VALUES (?,?,?)",
                (playlist_id, r["id"], str(dst)))
        copied += 1
    db.exec("INSERT OR REPLACE INTO playlist_exports(playlist_id, folder, exported_at) VALUES (?,?,datetime('now'))",
            (playlist_id, str(folder)))
    xml = write_tracks_xml(db, settings)
    log.info("Playlist export %s: %d copied, %d already there, %d of %d tracks approved -> %s",
             pl["name"], copied, skipped, len(rows), total, folder)
    return {"folder": str(folder), "copied": copied, "already": skipped, "approved": len(rows), "total": total,
            "xml": str(xml)}


def refresh_exports_for_track(db: Database, settings, track_id: int) -> int:
    """After a track is approved, add it to every playlist folder that was already exported. Returns count."""
    n = 0
    for e in db.q("""SELECT pe.playlist_id FROM playlist_exports pe JOIN playlist_tracks pt
                     ON pt.playlist_id = pe.playlist_id WHERE pt.track_id=?""", (track_id,)):
        n += export_playlist(db, settings, e["playlist_id"])["copied"]
    return n


def write_tracks_xml(db: Database, settings) -> Path:
    """One rekordbox XML for all exported playlists, nested under settings.rekordbox_folder_path."""
    root_dir = Path(settings.playlist_export_root).expanduser()
    root_dir.mkdir(parents=True, exist_ok=True)
    files = db.q("""SELECT ef.playlist_id, ef.path, l.*, pt.position FROM exported_files ef
                    JOIN library l ON l.id = ef.library_id
                    JOIN playlist_tracks pt ON pt.playlist_id = ef.playlist_id AND pt.track_id = l.track_id
                    ORDER BY ef.playlist_id, pt.position""")
    files = [f for f in files if Path(f["path"]).exists()]
    dj = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(dj, "PRODUCT", Name="CrateDigger", Version="0.1.0", Company="")
    coll = ET.SubElement(dj, "COLLECTION", Entries=str(len(files)))
    ids: dict[str, int] = {}
    for f in files:
        if f["path"] in ids:
            continue
        i = ids[f["path"]] = len(ids) + 1
        p = Path(f["path"])
        ET.SubElement(coll, "TRACK", TrackID=str(i), Name=f["title"] or "", Artist=f["artist"] or "",
                      Album=f["album"] or "", Genre=f["genre"] or "", Kind=KINDS.get(p.suffix.lower(), "Audio File"),
                      Size=str(p.stat().st_size), TotalTime=str(int(round(f["duration_s"] or 0))), Year=f["year"] or "",
                      Label=f["label"] or "", Location=location_uri(str(p)),
                      DateAdded=(f["approved_at"] or "")[:10], BitRate=str(f["bitrate"] or 0),
                      SampleRate=str(f["sample_rate"] or 0),
                      Comments=f"CrateDigger ISRC {f['isrc']}" if f["isrc"] else "CrateDigger")
    coll.set("Entries", str(len(ids)))
    pls = ET.SubElement(dj, "PLAYLISTS")
    node = ET.SubElement(pls, "NODE", Type="0", Name="ROOT", Count="1")
    for name in settings.rekordbox_folder_path:
        node = ET.SubElement(node, "NODE", Type="0", Name=name, Count="1")
    exported = db.q("SELECT p.* FROM playlist_exports pe JOIN playlists p ON p.id = pe.playlist_id ORDER BY p.name")
    node.set("Count", str(len(exported)))
    for pl in exported:
        members = [f for f in files if f["playlist_id"] == pl["id"]]
        pn = ET.SubElement(node, "NODE", Name=pl["name"], Type="1", KeyType="0", Entries=str(len(members)))
        for f in members:
            ET.SubElement(pn, "TRACK", Key=str(ids[f["path"]]))
        m3u = playlist_folder(settings, pl["name"]) / f"{pl['name']}.m3u8"
        lines = ["#EXTM3U"] + [x for f in members for x in
                               (f"#EXTINF:{int(round(f['duration_s'] or 0))},{f['artist']} - {f['title']}", f["path"])]
        m3u.write_text("\n".join(lines) + "\n", "utf-8")
    xml_path = root_dir / "CrateDigger_rekordbox.xml"
    tree = ET.ElementTree(dj)
    ET.indent(tree, space="  ")
    tmp = xml_path.with_suffix(".xml.tmp")
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    tmp.replace(xml_path)
    return xml_path
