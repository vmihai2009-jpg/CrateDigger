"""Candidate ↔ track matching.

The score (0–100) answers "is this file the same recording as the Spotify track?".
It is never used to accept anything automatically — it ranks candidates and
explains itself through `detail` and `flags` so the user can approve manually.

Components
  artist   whole-word fuzzy match of each credited artist against the path
  title    base-title match against the filename, minus unexplained extra words
  duration distance between reported and Spotify duration
  version  penalties when the version (remix / edit / live / extended …) differs
  album    small bonus when the album name appears in the folder
  isrc     for local files with an ISRC tag: equal → near-certain, different → heavy penalty
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .text import (_BRACKETS_RE, base_title, norm, path_parts, remixer_from, split_artists, strip_track_number,
                   version_descriptor, version_kind)

NOISE_WORDS = set("""
original mix version extended radio edit feat ft featuring with vs and the a of x
remaster remastered clean dirty explicit hq lp ep single web vinyl rip cd flac wav mp3 320 kbps
320kbps 24bit 16bit 44 1khz 48khz bandcamp beatport promo master mastered final official audio
lossless aiff aif alac m4a aac 256 256kbps 192 hi res hires 24 16 96khz 44khz 24 96 www com net org
""".split())

_DOMAIN_RE = re.compile(r"\b[\w-]+\.(?:com|net|org|ru|info|xyz|to|cc|io|me|fm)\b", re.I)
_SQUARE_RE = re.compile(r"\[([^\]]*)\]")

COMPATIBLE_WITH_ORIGINAL = {"", "original", "remaster", "clean"}
VERSION_PENALTY = {
    "extended": 4, "radio edit": 18, "remix": 38, "bootleg": 32, "vip": 32, "dub": 25,
    "live": 45, "preview": 60, "acapella": 50, "instrumental": 40, "sped up": 55,
}


@dataclass
class TrackRef:
    title: str
    artists: str
    album: str = ""
    duration_ms: int | None = None
    isrc: str | None = None
    label: str = ""

    @classmethod
    def from_row(cls, r) -> "TrackRef":
        return cls(r["title"], r["artists"], r["album"] or "", r["duration_ms"], r["isrc"],
                   (r["label"] if "label" in r.keys() else "") or "")


@dataclass
class MatchResult:
    score: int
    version: str
    detail: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def _window_ratio(needle: str, hay_words: list[str]) -> float:
    """Best fuzz.ratio of `needle` against any run of hay words of similar length.

    Comparing against word windows (not substrings) is what stops 'Mala' matching
    'Malaa' at 100 %, while still tolerating 'K-65' vs 'K65'.
    """
    if not needle or not hay_words:
        return 0.0
    n = len(needle.split())
    best = 0.0
    for k in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(1, len(hay_words) - k + 1)):
            win = hay_words[i:i + k]
            if not win:
                continue
            best = max(best, fuzz.ratio(needle, " ".join(win)),
                       fuzz.ratio(needle.replace(" ", ""), "".join(win)))
            if best >= 100:
                return 100.0
    return best


def _artist_ratio(artist: str, hay_words: list[str]) -> float:
    """Artist names must match nearly exactly: 'Mala' vs 'Malaa' is a different artist.

    Long names (10+ chars) tolerate a typo; anything below the bar is heavily discounted.
    """
    r = _window_ratio(artist, hay_words)
    bar = 88 if len(artist.replace(" ", "")) >= 10 else 94
    return r if r >= bar else r * 0.6


def _remove_phrase(words: list[str], phrase: str, min_ratio: float = 88) -> list[str]:
    """Remove the best-matching occurrence of phrase from words."""
    p = phrase.split()
    if not p:
        return words
    k = len(p)
    best_i, best_r, best_k = -1, 0.0, k
    for kk in {max(1, k - 1), k, k + 1}:
        for i in range(0, len(words) - kk + 1):
            r = max(fuzz.ratio(phrase, " ".join(words[i:i + kk])),
                    fuzz.ratio(phrase.replace(" ", ""), "".join(words[i:i + kk])))
            if r > best_r:
                best_i, best_r, best_k = i, r, kk
    if best_i >= 0 and best_r >= min_ratio:
        return words[:best_i] + words[best_i + best_k:]
    return words


def duration_score(target_s: float | None, cand_s: float | None, tol: float) -> tuple[float, str | None]:
    if not target_s or not cand_s:
        return 60.0, "no duration reported — cannot compare length"
    d = cand_s - target_s
    ad = abs(d)
    if ad <= 2:
        return 100.0, None
    if ad <= tol:
        return 100.0 - 20.0 * (ad - 2) / max(tol - 2, 1), None
    sign = "+" if d > 0 else "−"
    msg = f"duration {sign}{ad:.0f}s vs Spotify"
    if ad <= 20:
        return 55.0, msg
    if ad <= 45:
        return 25.0, msg + (" (longer — extended/club mix?)" if d > 0 else " (shorter — radio edit?)")
    return 0.0, msg + (" (much longer — extended mix or different track)" if d > 0 else " (much shorter — edit or different track)")


def score_candidate(track: TrackRef, path: str, duration_s: float | None, *, tol: float = 6.0,
                    tag_isrc: str | None = None, tag_artist: str | None = None,
                    tag_title: str | None = None) -> MatchResult:
    folder, stem, _ext = path_parts(path)
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    # Context = up to two parent folders + filename. Artist often lives in the folder name.
    ctx_text = " ".join(segs[-3:-1] + [stem])
    # Drop website watermarks ('[rawzik.com]') and square-bracket tags ('[FLAC]', '[Label]') unless they name
    # a version; parenthesised parts stay because they usually carry the mix name.
    stem = _DOMAIN_RE.sub(" ", stem)
    stem = _SQUARE_RE.sub(lambda mm: mm.group(0) if version_kind(mm.group(1)) else " ", stem)
    name_text = strip_track_number(stem)
    if tag_artist or tag_title:  # local files: tags are better evidence than filenames
        name_text = f"{tag_artist or ''} - {tag_title or ''} {name_text}"
        ctx_text = f"{ctx_text} {tag_artist or ''}"
    ctx_words = norm(ctx_text).split()
    name_words = norm(name_text).split()
    flags: list[str] = []
    detail: dict = {}

    # ---- artist -------------------------------------------------------
    artists = [norm(a) for a in split_artists(track.artists)]
    per_artist = [_artist_ratio(a, ctx_words) for a in artists] or [0.0]
    primary = per_artist[0]
    others = per_artist[1:]
    artist_score = primary if not others else max(primary, 0.75 * primary + 0.25 * max(others))
    if primary < 90 and others and max(others) >= 95:
        artist_score = max(artist_score, 85.0)  # filed under a featured/co-artist
        flags.append("primary artist not in path; matched co-artist")
    detail["artist"] = round(artist_score)

    # ---- title --------------------------------------------------------
    tbase = norm(base_title(track.title))
    rest = list(name_words)
    for a in artists:
        rest = _remove_phrase(rest, a)
    title_ratio = _window_ratio(tbase, rest) if rest else 0.0
    title_set = fuzz.token_set_ratio(tbase, " ".join(rest)) if rest else 0.0
    title_score = max(title_ratio, min(title_set, title_ratio + 15))
    # Unexplained words → "similar title, different track" (e.g. 'Alma' vs 'Alma Mater')
    leftover = _remove_phrase(rest, tbase, 80) if tbase else rest
    tdesc = version_descriptor(track.title)
    known = set(norm(tdesc).split()) | NOISE_WORDS | set(norm(track.album).split()) | set(norm(track.label).split())
    for a in artists:
        known |= set(a.split())
    cand_version_text = " ".join(leftover)
    extras = [w for w in leftover if w not in known and not w.isdigit() and len(w) > 1
              and version_kind(w) == ""]
    extra_cap = None
    if extras and title_score >= 70:
        title_score = max(0.0, title_score - min(30, 8 * len(extras)))
        flags.append("extra words in title: " + " ".join(extras[:6]))
        if len(extras) >= 2:
            extra_cap = 70  # probably a different track that shares words with this title
    detail["title"] = round(title_score)

    # ---- version ------------------------------------------------------
    target_kind = version_kind(tdesc)
    target_mixer = remixer_from(tdesc) if target_kind not in ("", "original") else ""
    cand_kind = version_kind(cand_version_text)
    ctx_kind = version_kind(" ".join(segs[-2:-1]))  # e.g. folder "Live at Fabric"
    if ctx_kind == "live" and cand_kind == "":
        cand_kind = "live"
    version_pen = 0.0
    if target_kind in COMPATIBLE_WITH_ORIGINAL:
        if cand_kind not in COMPATIBLE_WITH_ORIGINAL:
            version_pen = VERSION_PENALTY.get(cand_kind, 25)
            if cand_kind != "extended":
                flags.append(f"different version: {cand_kind}")
        # Spotify descriptor without a known kind (e.g. 'Vocal'): expect those words
        dwords = [w for w in norm(tdesc).split() if w not in NOISE_WORDS]
        if dwords and not all(_window_ratio(w, name_words) >= 90 for w in dwords):
            version_pen += 12
            flags.append(f"version '{tdesc}' not named in file")
    else:
        mixer_ok = bool(target_mixer) and _window_ratio(target_mixer, name_words + ctx_words) >= 85
        if mixer_ok:
            pass
        elif cand_kind in COMPATIBLE_WITH_ORIGINAL:
            version_pen = 35
            flags.append(f"looks like the original, wanted '{tdesc}'")
        elif cand_kind == target_kind and not target_mixer:
            pass
        else:
            version_pen = 30
            flags.append(f"different {cand_kind or 'version'}; wanted '{tdesc}'")
    if target_kind == "radio edit" and cand_kind == "extended":
        version_pen = max(version_pen, 18)
        flags.append("extended mix, wanted radio edit")
    detail["version_penalty"] = round(version_pen)
    cand_desc = " ".join(m.group(1).strip() for m in _BRACKETS_RE.finditer(stem)
                         if version_kind(m.group(1)))
    shown_version = cand_desc if cand_desc and cand_kind else (cand_kind or "original/unspecified")

    # ---- duration -----------------------------------------------------
    target_s = (track.duration_ms or 0) / 1000 or None
    dscore, dmsg = duration_score(target_s, duration_s, tol)
    if dmsg:
        flags.append(dmsg)
    detail["duration"] = round(dscore)
    if target_s and duration_s:
        detail["duration_diff_s"] = round(duration_s - target_s, 1)

    # ---- combine --------------------------------------------------------
    score = 0.38 * artist_score + 0.38 * title_score + 0.24 * dscore - version_pen
    alb = norm(track.album)
    if alb and alb != tbase and len(alb) > 2 and _window_ratio(alb, ctx_words) >= 90:
        score += 4
        detail["album"] = "match"
    if extra_cap is not None:
        score = min(score, extra_cap)
    if artist_score < 70:
        score = min(score, 45)
        flags.append("artist not found in path")
    if title_score < 65:
        score = min(score, 40)
        flags.append("title does not match")
    if tag_isrc and track.isrc:
        if tag_isrc.strip().upper().replace("-", "") == track.isrc.upper():
            score = max(score, 96 - version_pen / 2)
            detail["isrc"] = "match"
        else:
            score -= 25
            detail["isrc"] = "different"
            flags.append(f"ISRC tag {tag_isrc} ≠ Spotify {track.isrc}")
    return MatchResult(int(max(0, min(100, round(score)))), shown_version, detail, flags)
