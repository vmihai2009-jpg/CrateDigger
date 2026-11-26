"""String normalisation, version detection and search-query generation."""
from __future__ import annotations

import re
import unicodedata

_FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(feat\.?|ft\.?|featuring|with)\b.*?([\)\]]|$)", re.I)
_BRACKETS_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_SUFFIX_RE = re.compile(r"\s+-\s+(.+)$")
_TRACKNO_RE = re.compile(r"^\s*(?:[a-d]?\d{1,3}|cd\d+[-_ ]?\d+)\s*[-._)\s]+\s*", re.I)
_NONWORD_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")

# Soulseek search ignores very short words and some characters; quotes etc. hurt results.
SEARCH_STRIP_RE = re.compile(r"[\"'`´’&/\\:;,!?*()\[\]{}<>|#@$%^+=~.]")


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm(s: str | None) -> str:
    """Lowercase, accent-free, punctuation-free, single-spaced."""
    if not s:
        return ""
    s = strip_accents(s).lower().replace("&", " and ").replace("$", "s")
    s = _NONWORD_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def split_artists(artists: str) -> list[str]:
    return [a.strip() for a in re.split(r"[;]", artists or "") if a.strip()]


# --------------------------------------------------------------------------
# Version detection
# --------------------------------------------------------------------------
# Ordered: first match wins for the "kind". Keywords match whole words on the normalised text.
VERSION_KINDS: list[tuple[str, list[str]]] = [
    ("preview", ["clip", "clips", "preview", "snippet", "sample", "teaser", "promo clip"]),
    ("live", ["live", "live at", "live from", "session", "bbc session", "boiler room"]),
    ("acapella", ["acapella", "a cappella", "acappella"]),
    ("instrumental", ["instrumental", "inst"]),
    ("radio edit", ["radio edit", "radio mix", "radio version", "single edit", "short edit"]),
    ("extended", ["extended mix", "extended version", "extended", "club mix", "12 mix", "12 version", "original extended"]),
    ("vip", ["vip", "vip mix"]),
    ("original", ["original mix", "original version", "original"]),
    ("dub", ["dub mix", "dub version", "dub"]),
    ("bootleg", ["bootleg", "flip", "rework", "re edit", "reedit", "refix", "edit"]),
    ("remix", ["remix", "rmx", "mix"]),
    ("remaster", ["remaster", "remastered"]),
    ("sped up", ["sped up", "slowed", "nightcore", "8d audio"]),
    ("clean", ["clean", "clean version"]),
]


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) is not None


def version_descriptor(title: str) -> str:
    """The version part of a title: bracketed text or ' - xyz' suffix (e.g. 'LDN Mix', 'Vocal')."""
    parts = [m.group(1) for m in _BRACKETS_RE.finditer(title or "")]
    m = _DASH_SUFFIX_RE.search(_BRACKETS_RE.sub("", title or ""))
    if m:
        parts.append(m.group(1))
    parts = [p for p in parts if not re.match(r"^\s*(feat|ft|featuring|with)\b", p, re.I)]
    return " ".join(parts).strip()


def version_kind(text: str) -> str:
    """Classify a version descriptor or a whole filename. Returns '' for 'no version marker'."""
    t = norm(text)
    if not t:
        return ""
    for kind, phrases in VERSION_KINDS:
        if any(_has_phrase(t, p) for p in phrases):
            return kind
    return ""


def base_title(title: str) -> str:
    """Title with version / featuring information removed: 'Tensor (LDN Mix)' -> 'Tensor'."""
    t = _FEAT_RE.sub("", title or "")
    t = _BRACKETS_RE.sub("", t)
    t = _DASH_SUFFIX_RE.sub("", t)
    return t.strip(" -") or (title or "").strip()


def remixer_from(desc: str) -> str:
    """'41 Candies Shade K Mix' -> '41 candies shade k'; 'LDN Mix' -> 'ldn'."""
    t = norm(desc)
    t = re.sub(r"\b(remix|rmx|mix|edit|version|dub|vip|rework|bootleg|flip|refix)\b", "", t)
    return _WS_RE.sub(" ", t).strip()


# --------------------------------------------------------------------------
# Search queries
# --------------------------------------------------------------------------
def _clean_for_search(s: str) -> str:
    s = strip_accents(s)
    s = SEARCH_STRIP_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def build_queries(title: str, artists: str) -> list[str]:
    """Search queries for a track, most specific first.

    Soulseek search is a whole-word AND match over the full path, so we keep the
    query short: primary artist + base title, plus the version descriptor where the
    Spotify title names a specific mix. Fallbacks drop the artist / version.
    """
    arts = split_artists(artists)
    primary = _clean_for_search(arts[0]) if arts else ""
    base = _clean_for_search(base_title(title))
    desc = _clean_for_search(remixer_from(version_descriptor(title)))
    queries: list[str] = []
    # Soulseek ignores 1-letter words, so 'D.A.N' -> 'D A N' or 'T>I' -> 'T I' search on the title alone.
    initials = primary and all(len(w) <= 1 for w in primary.split())
    if initials:
        primary = primary.replace(" ", "")  # 'DAN', 'TI'
    if desc and version_kind(version_descriptor(title)) not in ("original", ""):
        queries.append(f"{primary} {base} {desc}".strip())
    queries.append(f"{primary} {base}".strip())
    if initials or len(primary) <= 2:
        for co in arts[1:3]:  # co-credited artists are a good second anchor
            c = _clean_for_search(co)
            if len(c.replace(" ", "")) > 2:
                queries.append(f"{c} {base}")
    if len(base.split()) >= 2 or len(base) >= 8:
        queries.append(base)  # title-only fallback, matching filters out wrong artists
    out: list[str] = []
    for q in queries:
        q = _WS_RE.sub(" ", q).strip()
        if q and q.lower() not in [o.lower() for o in out]:
            out.append(q)
    return out


def clean_filename(s: str, max_len: int = 150) -> str:
    """Filesystem- and Rekordbox-safe filename component (keeps unicode letters)."""
    s = unicodedata.normalize("NFC", s or "")
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = s.replace("$", "S") if s.strip() == "$" else s
    s = _WS_RE.sub(" ", s).strip(" .")
    return s[:max_len].rstrip(" .") or "Unknown"


def path_parts(path: str) -> tuple[str, str, str]:
    """Split a Soulseek virtual path ('@@abc\\Music\\Artist - Album\\01 Track.flac') or a POSIX path.

    Returns (folder_name, stem, ext) — folder is the immediate parent folder name.
    """
    p = path.replace("\\", "/")
    segs = [s for s in p.split("/") if s]
    name = segs[-1] if segs else p
    folder = segs[-2] if len(segs) >= 2 else ""
    if "." in name:
        stem, ext = name.rsplit(".", 1)
    else:
        stem, ext = name, ""
    return folder, stem, ext.lower()


def strip_track_number(stem: str) -> str:
    return _TRACKNO_RE.sub("", stem)


KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Camelot wheel: (pitch class, mode) -> code; mode 1 = major (B), 0 = minor (A)
_CAMELOT_MAJOR = {0: "8B", 7: "9B", 2: "10B", 9: "11B", 4: "12B", 11: "1B", 6: "2B", 1: "3B", 8: "4B", 3: "5B", 10: "6B", 5: "7B"}
_CAMELOT_MINOR = {9: "8A", 4: "9A", 11: "10A", 6: "11A", 1: "12A", 8: "1A", 3: "2A", 10: "3A", 5: "4A", 0: "5A", 7: "6A", 2: "7A"}


def key_name(key: int | None, mode: int | None) -> str:
    if key is None or key < 0 or key > 11:
        return ""
    return KEY_NAMES[key] + ("m" if mode == 0 else "")


def camelot(key: int | None, mode: int | None) -> str:
    if key is None or key < 0 or key > 11:
        return ""
    return (_CAMELOT_MINOR if mode == 0 else _CAMELOT_MAJOR)[key]
