"""Paths and user settings.

Everything CrateDigger writes lives in one of three places:
  * APP_DIR      ~/Library/Application Support/CrateDigger  (database, logs, bridge spool)
  * library_root ~/Music/CrateDigger by default             (organised copies of approved files)
  * export dir   <library_root>/_Rekordbox                  (rekordbox XML + M3U8 playlists)
Source files (your existing collection, Nicotine+ downloads) are only ever read.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path.home()
APP_DIR = Path(os.environ.get("CRATEDIGGER_HOME", HOME / "Library" / "Application Support" / "CrateDigger"))
DB_PATH = APP_DIR / "cratedigger.sqlite3"
LOG_DIR = APP_DIR / "logs"
BRIDGE_DIR = APP_DIR / "bridge"
INBOX_DIR = APP_DIR / "inbox"          # other apps (Minapp) drop playlist CSVs here; they are imported automatically
SETTINGS_PATH = APP_DIR / "settings.json"

NICOTINE_DATA = HOME / ".local" / "share" / "nicotine"
NICOTINE_PLUGIN_DIR = NICOTINE_DATA / "plugins"
NICOTINE_DOWNLOADS = NICOTINE_DATA / "downloads"

# Genre folders, checked in order: the first rule whose keywords match one of the
# track's Spotify genres wins. Edit in settings.json to taste.
DEFAULT_GENRE_RULES: list[list] = [
    ["Drum and Bass", ["drum and bass", "liquid funk", "neurofunk", "jump up", "dnb"]],
    ["Jungle", ["jungle", "ragga jungle"]],
    ["UK Garage", ["uk garage", "speed garage", "2-step", "bassline", "future garage"]],
    ["Dubstep", ["dubstep", "dub", "riddim", "deep dubstep"]],
    ["Breakbeat", ["breakbeat", "breaks", "big beat", "miami bass", "uk funky"]],
    ["Electro", ["electro", "electro house", "electroclash"]],
    ["Techno", ["techno", "dub techno", "minimal techno", "hard techno"]],
    ["House", ["house", "deep house", "tech house", "disco house"]],
]


@dataclass
class Settings:
    library_root: str = str(HOME / "Music" / "CrateDigger")
    watch_folders: list[str] = field(default_factory=lambda: [str(NICOTINE_DOWNLOADS)])
    existing_collection: list[str] = field(default_factory=list)  # read-only duplicate check
    search_interval_s: float = 6.0       # min gap between searches sent to Soulseek (avoid flooding)
    search_window_s: float = 30.0        # how long results are collected per search
    max_results_per_track: int = 200
    duration_tolerance_s: float = 6.0
    min_match_score: int = 55            # below this a candidate is hidden by default
    genre_rules: list[list] = field(default_factory=lambda: [list(r) for r in DEFAULT_GENRE_RULES])
    filename_pattern: str = "{artist} - {title}"
    # Per-playlist export: files go to <playlist_export_root>/<playlist name>/, and the rekordbox XML nests the
    # playlists under this folder path (e.g. mihai → Tracks → To_add).
    playlist_export_root: str = str(HOME / "Tracks")
    rekordbox_folder_path: list[str] = field(default_factory=lambda: ["mihai", "Tracks"])
    # Automatic approval of the best match (off by default). Enabling it in Settings requires choosing a
    # standing authorisation basis, which is recorded on every file it downloads.
    auto_approve_best: bool = False
    auto_basis: str = ""
    auto_note: str = ""
    auto_min_score: int = 90
    # Updates: check GitHub Releases at start-up; a version the user chose to skip is not offered again.
    check_updates: bool = True
    skipped_update: str = ""

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text("utf-8"))
                for k, v in data.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except (OSError, ValueError):
                pass
        return s

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        tmp.replace(SETTINGS_PATH)


def ensure_dirs() -> None:
    for d in (APP_DIR, LOG_DIR, INBOX_DIR, BRIDGE_DIR / "requests", BRIDGE_DIR / "results"):
        d.mkdir(parents=True, exist_ok=True)
