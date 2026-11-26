"""Quality assessment of *reported* search-result attributes.

Soulseek results carry whatever the sharer's client reports (bitrate, duration,
sample rate, bit depth). None of it is trusted: we cross-check it against the
file size, and label everything as "reported" until the real file has been
analysed with ffprobe/ffmpeg (see audio_analysis.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

LOSSLESS = {"flac", "wav", "aiff", "aif", "alac"}
LOSSY = {"mp3", "m4a", "aac", "ogg", "opus", "wma"}
AUDIO_EXTS = LOSSLESS | LOSSY

TIER_LABELS = {1: "FLAC", 2: "WAV/AIFF", 3: "MP3 320", 4: "256 kbps", 5: "192 kbps", 6: "low", 7: "unknown"}


@dataclass
class QualityResult:
    tier: int                     # 1 best … 7 unknown
    label: str
    implied_kbps: int | None
    flags: list[str] = field(default_factory=list)


def implied_kbps(size: int | None, duration_s: float | None) -> int | None:
    if not size or not duration_s or duration_s <= 0:
        return None
    return int(size * 8 / duration_s / 1000)


def assess_reported(ext: str, size: int | None, bitrate: int | None, duration_s: float | None,
                    sample_rate: int | None = None, bit_depth: int | None = None,
                    vbr: int | None = None) -> QualityResult:
    ext = (ext or "").lower()
    flags: list[str] = []
    ikbps = implied_kbps(size, duration_s)

    if ext == "flac" or ext == "alac":
        tier, label = 1, ext.upper()
    elif ext in ("wav", "aiff", "aif"):
        tier, label = 2, ext.upper()
    elif ext in LOSSY:
        br = bitrate or ikbps
        if br is None:
            tier, label = 7, f"{ext.upper()} ?"
        elif br >= 315:
            tier, label = 3, f"{ext.upper()} {br}"
        elif br >= 250:
            tier, label = 4, f"{ext.upper()} {br}"
        elif br >= 185:
            tier, label = 5, f"{ext.upper()} {br}"
        else:
            tier, label = 6, f"{ext.upper()} {br}"
        if vbr:
            label += " VBR"
    else:
        return QualityResult(7, ext.upper() or "?", ikbps, ["not a recognised audio file type"])

    if sample_rate and bit_depth and ext in LOSSLESS:
        label += f" {bit_depth}/{sample_rate / 1000:g}k"

    if ikbps is None:
        flags.append("no duration reported — bitrate cannot be cross-checked")
    else:
        if ext == "mp3":
            # Store-bought MP3s often embed 1–3 MB of artwork, so a file can legitimately be *bigger* than its
            # bitrate implies. Being *smaller* than the reported bitrate allows is the suspicious direction.
            excess_mb = (size - 320_000 * duration_s / 8) / 1e6
            if ikbps > 345 and excess_mb > 4:
                flags.append(f"{excess_mb:.0f} MB more data than a 320 kbps MP3 needs (wrong extension or embedded "
                             "extra data?)")
            if bitrate and not vbr and ikbps < bitrate - max(40, bitrate * 0.2):
                flags.append(f"reported {bitrate} kbps but size implies only ~{ikbps} kbps")
        elif ext in ("flac", "alac"):
            if ikbps < 400:
                flags.append(f"only ~{ikbps} kbps for {ext.upper()} — possible lossy source or mono/short")
        elif ext in ("wav", "aiff", "aif"):
            expected = (sample_rate or 44100) * (bit_depth or 16) * 2 / 1000
            if ikbps < expected * 0.85:
                flags.append(f"size implies ~{ikbps} kbps; uncompressed {ext.upper()} should be ~{expected:.0f}")
        elif ext in LOSSY and bitrate and ikbps < bitrate - max(40, bitrate * 0.25) and not vbr:
            flags.append(f"reported {bitrate} kbps but size implies only ~{ikbps} kbps")
    if sample_rate and sample_rate < 44100:
        flags.append(f"sample rate {sample_rate} Hz")
    if ext in LOSSY and bitrate and bitrate < 185:
        flags.append(f"low bitrate ({bitrate} kbps)")
    return QualityResult(tier, label, ikbps, flags)
