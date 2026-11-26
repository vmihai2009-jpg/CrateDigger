"""Verification of real audio files with ffprobe / ffmpeg (read-only).

What we can establish with confidence
  * the actual codec and container (catches .flac files that contain MP3 data, etc.)
  * real duration, sample rate, bit depth, channels, average bitrate
What is a heuristic, and labelled as such
  * spectral cutoff: lossy encoders low-pass the signal (≈16 kHz at 128 kbps,
    ≈19–20 kHz at 256–320 kbps). A "lossless" file or "320 kbps" MP3 whose
    spectrum stops at 16 kHz was very probably made from a low-bitrate source.
    Some genuine masters are band-limited, so this produces warnings, not verdicts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

EXT_CODECS = {
    "flac": {"flac"}, "mp3": {"mp3"}, "wav": {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8", "pcm_s16be"},
    "aiff": {"pcm_s16be", "pcm_s24be", "pcm_s32be", "pcm_f32be"}, "aif": {"pcm_s16be", "pcm_s24be", "pcm_s32be"},
    "m4a": {"aac", "alac"}, "aac": {"aac"}, "ogg": {"vorbis", "opus", "flac"}, "opus": {"opus"},
}
LOSSLESS_CODECS = {"flac", "alac"} | {c for c in EXT_CODECS["wav"] | EXT_CODECS["aiff"]}


@dataclass
class Analysis:
    path: str
    size: int = 0
    sha1: str = ""
    container: str = ""
    codec: str = ""
    bitrate: int | None = None        # kbps (average, as measured by ffprobe)
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    duration_s: float | None = None
    cutoff_hz: int | None = None
    tag_artist: str | None = None
    tag_title: str | None = None
    tag_album: str | None = None
    tag_isrc: str | None = None
    has_artwork: bool = False
    verdict: str = "ok"               # ok | warn | bad
    flags: list[str] = field(default_factory=list)

    def add(self, level: str, msg: str) -> None:
        self.flags.append(msg)
        order = {"ok": 0, "warn": 1, "bad": 2}
        if order[level] > order[self.verdict]:
            self.verdict = level


def sha1_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def ffprobe(path: Path) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "{}")


def spectral_cutoff(path: Path, duration_s: float | None, seconds: float = 30.0) -> int | None:
    """Highest frequency (Hz) that still carries real energy, measured on a 30 s excerpt.

    Decodes to mono 44.1 kHz float, averages Hann-windowed FFT power spectra, then
    finds where the spectrum drops more than 50 dB below its 1–8 kHz reference level.
    """
    start = max(0.0, (duration_s or 0) / 2 - seconds / 2)
    cmd = [FFMPEG, "-v", "error", "-ss", f"{start:.1f}", "-t", f"{seconds:.0f}", "-i", str(path),
           "-map", "0:a:0", "-ac", "1", "-ar", "44100", "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        log.warning("spectral decode failed for %s: %s", path, proc.stderr[:200])
        return None
    x = np.frombuffer(proc.stdout, dtype=np.float32)
    n = 8192
    if len(x) < n * 4:
        return None
    frames = len(x) // n
    x = x[: frames * n].reshape(frames, n)
    # skip near-silent frames so quiet intros don't dominate
    rms = np.sqrt((x ** 2).mean(axis=1))
    x = x[rms > max(1e-4, np.percentile(rms, 20))]
    if len(x) < 4:
        return None
    spec = (np.abs(np.fft.rfft(x * np.hanning(n), axis=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(spec + 1e-20)
    freqs = np.fft.rfftfreq(n, 1 / 44100)
    k = 9  # ~50 Hz smoothing
    db_s = np.convolve(db, np.ones(k) / k, mode="same")
    # 1) Brick-wall detection: lossy encoders leave a cliff of 20+ dB within a few
    #    hundred Hz. Dither/quantisation noise after transcoding sits *below* the cliff,
    #    so this still finds the cutoff when a lossy file was re-saved as FLAC/WAV.
    w = int(400 / (44100 / n))  # ~400 Hz windows
    best_f, best_drop = None, 0.0
    for i in range(int(10000 / (44100 / n)), len(db_s) - w - 2):
        drop = db_s[i - w:i].mean() - db_s[i + 2:i + 2 + w].mean()
        if drop > 20 and drop >= best_drop:
            best_f, best_drop = freqs[i], drop
    if best_f is not None:
        return int(best_f)
    # 2) Otherwise: highest frequency within 50 dB of the 1–8 kHz reference level.
    ref = np.median(db_s[(freqs > 1000) & (freqs < 8000)])
    above = np.where((db_s > ref - 50) & (freqs > 1000))[0]
    if len(above) == 0:
        return None
    return int(freqs[above[-1]])


def read_tags(path: Path, a: Analysis) -> None:
    try:
        import mutagen
        f = mutagen.File(str(path), easy=True)
        if f is not None and f.tags is not None:
            g = lambda k: (f.tags.get(k) or [None])[0]  # noqa: E731
            a.tag_artist, a.tag_title, a.tag_album, a.tag_isrc = g("artist"), g("title"), g("album"), g("isrc")
        raw = mutagen.File(str(path))
        if raw is not None:
            pics = getattr(raw, "pictures", None)
            if pics:
                a.has_artwork = True
            elif raw.tags is not None and hasattr(raw.tags, "keys"):
                a.has_artwork = any(str(k).startswith(("APIC", "covr", "metadata_block_picture")) for k in raw.tags.keys())
    except Exception as e:  # tags are optional evidence
        log.info("tag read failed for %s: %s", path, e)


def analyze_file(path: Path | str, expected_duration_s: float | None = None, spectral: bool = True,
                 tolerance_s: float = 6.0) -> Analysis:
    path = Path(path)
    a = Analysis(path=str(path))
    a.size = path.stat().st_size
    a.sha1 = sha1_file(path)
    try:
        info = ffprobe(path)
    except Exception as e:
        a.add("bad", f"not readable as audio ({e})")
        return a
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        a.add("bad", "no audio stream")
        return a
    s, fmt = streams[0], info.get("format", {})
    a.container = fmt.get("format_name", "")
    a.codec = s.get("codec_name", "")
    a.sample_rate = int(s["sample_rate"]) if s.get("sample_rate") else None
    a.channels = s.get("channels")
    bits = s.get("bits_per_raw_sample") or s.get("bits_per_sample")
    a.bit_depth = int(bits) if bits and int(bits) > 0 else None
    dur = s.get("duration") or fmt.get("duration")
    a.duration_s = float(dur) if dur else None
    br = s.get("bit_rate") or fmt.get("bit_rate")
    a.bitrate = int(int(br) / 1000) if br else None
    read_tags(path, a)

    ext = path.suffix.lower().lstrip(".")
    allowed = EXT_CODECS.get(ext)
    if allowed is not None and a.codec not in allowed:
        a.add("bad", f"misleading extension: .{ext} file actually contains {a.codec}")
    if a.channels == 1:
        a.add("warn", "mono file")
    if a.sample_rate and a.sample_rate < 44100:
        a.add("warn", f"sample rate {a.sample_rate} Hz")
    if expected_duration_s and a.duration_s:
        d = a.duration_s - expected_duration_s
        if abs(d) > tolerance_s:
            a.add("warn" if abs(d) < 30 else "bad",
                  f"duration {a.duration_s:.0f}s vs Spotify {expected_duration_s:.0f}s ({d:+.0f}s)")
    if a.codec == "mp3" and a.bitrate and a.bitrate < 190:
        a.add("warn", f"low bitrate MP3 ({a.bitrate} kbps)")

    if spectral:
        try:
            a.cutoff_hz = spectral_cutoff(path, a.duration_s)
        except Exception as e:
            log.warning("spectral analysis failed for %s: %s", path, e)
        c = a.cutoff_hz
        if c:
            lossless = a.codec in LOSSLESS_CODECS
            if lossless and c < 17500:
                a.add("bad", f"spectrum ends at {c / 1000:.1f} kHz — lossless file probably made from a ≤128 kbps lossy source")
            elif lossless and c < 19500:
                a.add("warn", f"spectrum ends at {c / 1000:.1f} kHz — possibly transcoded from lossy (heuristic)")
            elif a.codec == "mp3" and (a.bitrate or 0) >= 300 and c < 17500:
                a.add("bad", f"labelled {a.bitrate} kbps but spectrum ends at {c / 1000:.1f} kHz — likely upscaled from ~128 kbps")
            elif a.codec == "mp3" and (a.bitrate or 0) >= 300 and c < 19000:
                a.add("warn", f"labelled {a.bitrate} kbps but spectrum ends at {c / 1000:.1f} kHz — source may be ~192 kbps (heuristic)")
            elif a.codec == "mp3" and (a.bitrate or 0) >= 240 and c < 17500:
                a.add("warn", f"spectrum ends at {c / 1000:.1f} kHz — lower quality than the bitrate suggests")
    return a
