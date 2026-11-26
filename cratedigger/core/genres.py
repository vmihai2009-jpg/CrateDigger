from __future__ import annotations

UNSORTED = "Unsorted"


def genre_folder_for(genres: str, rules: list[list]) -> str:
    """Pick a library folder from Spotify's comma-separated genres.

    Each Spotify genre is checked, in the order Spotify lists them, against the
    rule keywords; exact keyword matches beat substring matches so 'dub techno'
    lands in Techno rather than Dubstep.
    """
    gs = [g.strip().lower() for g in (genres or "").split(",") if g.strip()]
    if not gs:
        return UNSORTED
    # Pass 1: exact keyword match, Spotify genre order first (first genre is the most representative).
    for g in gs:
        for folder, keywords in rules:
            if g in [k.lower() for k in keywords]:
                return folder
    # Pass 2: keyword contained in genre
    for g in gs:
        for folder, keywords in rules:
            if any(k.lower() in g for k in keywords):
                return folder
    return UNSORTED
