# CrateDigger

Spotify playlist → Soulseek (via Nicotine+) → verified, Rekordbox-ready library. Runs locally on macOS
(Apple Silicon); no paid APIs, accounts or cloud services.

## Install

Requirements: Homebrew Python 3.11+, FFmpeg and ClamAV (`brew install python ffmpeg clamav`), Nicotine+ 3.3.x.
After installing ClamAV, create its updater config once and download the virus signatures:

```bash
cp "$(brew --prefix)/etc/clamav/freshclam.conf.sample" "$(brew --prefix)/etc/clamav/freshclam.conf" && sed -i '' 's/^Example/#Example/' "$(brew --prefix)/etc/clamav/freshclam.conf" && freshclam
```

Run `freshclam` regularly (the Dashboard warns when signatures are more than 7 days old).

```bash
./scripts/setup.sh
```

This creates `.venv`, installs four dependencies (PySide6-Essentials, mutagen, rapidfuzz, numpy) and builds
`dist/CrateDigger.app`. Launch it with `open dist/CrateDigger.app`, drag it to `/Applications`, or run
`./scripts/run.sh` to see logs in the terminal. Re-run `./scripts/build_app.sh` if you move the project folder.

## Connect Nicotine+ (one time)

1. CrateDigger → Dashboard → **Install bridge plugin** (copies `nicotine_plugin/cratedigger_bridge` into
   `~/.local/share/nicotine/plugins/`).
2. Nicotine+ → Preferences → Plugins → enable **CrateDigger Bridge**.
3. Make sure Nicotine+ is logged in. The Dashboard dot turns green.

CrateDigger never sees your Soulseek password; searches run through your existing Nicotine+ session.

## Receive playlists from Minapp

In Minapp: menu → Export → CrateDigger → **Send everything to CrateDigger…** (or right-click a playlist → Port playlist →
Crate Digger). Minapp drops one CSV per playlist into `~/Library/Application Support/CrateDigger/inbox/`; CrateDigger
imports them within seconds (or at launch), moves them to `inbox/done/`, and a playlist sent again only adds new tracks.

## Workflow

1. **Playlist → Import CSV** (Exportify-style Spotify export). Tracks are de-duplicated by Spotify URI/ISRC,
   assigned a genre folder, and given a search query (editable by double-click).
2. **Search all new** / **Search selected**. Searches are paced (default one every 6 s) and collected for 30 s
   each. If nothing good comes back, the next fallback query is tried automatically (e.g. without the remix
   name, then title only). **Retry missing/failed** re-queues those tracks.
3. **Search results**: every candidate gets a match score with an explanation. Select / reject manually.
4. **Authorise & download…**: a download is only queued in Nicotine+ after you record the basis on which you
   may obtain the recording (public domain, Creative Commons, explicit permission…). The plugin itself
   refuses download requests without an authorisation. Discovery and download stay separate.
5. Finished downloads are **analysed automatically** (codec, real bitrate, sample rate, duration, spectral cutoff).
   Files you obtained elsewhere: **Library → Scan folders** matches files in your watch folders (default:
   Nicotine+ downloads) to tracks, read-only. Then **Analyse file**.
6. **Approve → library** copies the file to `~/Music/CrateDigger/<Genre>/<Artist - Title>.<ext>` and writes
   tags (artist, title, album, genre, BPM, key, ISRC, label, year) **to the copy only**.
7. **Library → Export for Rekordbox** writes `~/Music/CrateDigger/_Rekordbox/CrateDigger.xml` plus M3U8
   playlists (per genre folder and per imported Spotify playlist). In rekordbox: Preferences → Advanced →
   Database → *rekordbox xml* → choose the file; the playlists appear under “rekordbox xml”.
   rekordbox's own database is never touched.

## Matching

Score 0–100 = 38 % artist + 38 % title + 24 % duration, minus version penalties, plus album/ISRC evidence:

* Artist names are matched word-for-word: “Mala” ≠ “Malaa”. If no credited artist is in the path, the score is
  capped at 45.
* Extra unexplained title words (“Alma” vs “Alma Mater Dolorosa”) are penalised and capped at 70.
* Version detection: remix, VIP, dub, bootleg/edit, radio edit, extended/club, live, acapella, instrumental,
  sped up. A remix when you want the original, the original when Spotify names a specific mix (e.g. “LDN Mix”),
  a radio edit instead of an extended mix, or a live recording are all penalised and flagged.
* Duration: within 2 s is perfect; beyond the tolerance (6 s) the score drops and the flag says whether the file
  is longer (extended?) or shorter (edit?).
* For local files the ISRC tag is checked against Spotify's: equal = near-certain, different = −25.

Nothing is accepted automatically; a score is a ranking aid, approval is always manual.

## Quality checks

Search results show what the sharer's client **reports** (bitrate, length, sample rate, bit depth), labelled as
unverified, and cross-checked against file size (e.g. “reported 320 kbps but size implies ~128”).
After download, the real file is verified:

* actual codec vs extension (a `.flac` that contains MP3 → **bad**)
* spectral cutoff via FFT: lossless or “320 kbps” files whose spectrum stops at ~16 kHz are flagged as probably
  upscaled from a low-bitrate source. This is a heuristic and is reported as such.
* duration vs Spotify, sample rate, mono, low bitrate, tags that don't match the track.

Priority order: FLAC → WAV/AIFF → MP3 320 → 256 → 192.

## Virus & malware scanning (mandatory)

Every file is scanned before it can enter the library, and approval is refused unless the scan is **clean**.
There is no override.

1. **Before download**, on Soulseek results: names with double extensions (`track.mp3.exe`), executable/archive
   extensions, or hidden right-to-left override characters are flagged `SECURITY:` and cannot be downloaded.
2. **After download / on scan**, built-in structural checks: the file's real signature must match its audio
   extension; executables (Mach-O, PE, ELF), scripts, archives (ZIP/RAR/7z/gzip), installers (pkg/xar),
   documents and HTML are rejected; an embedded Windows executable anywhere in the file is rejected; symlinks and
   bundles are rejected. A file that fails is never parsed further.
3. **ClamAV** signature scan (`clamdscan` if the daemon is running, otherwise `clamscan`).
4. **At approval** the file is re-scanned, in case it changed since analysis.

Without ClamAV installed, files show as `UNSCANNED` and cannot be approved. Infected or suspicious files are
left where they are (CrateDigger never deletes your files); the Results page tells you which ones to delete.

## Data & logs

* Database, settings, logs, bridge channel: `~/Library/Application Support/CrateDigger/`
* Logs: `…/CrateDigger/logs/cratedigger.log` (also visible in the app's Activity page)
* Library: `~/Music/CrateDigger/` (configurable in Settings)

Source files (your collection, Nicotine+ downloads) are only ever read. Library writes never overwrite an
existing file.

## Architecture

```
cratedigger/core/   security (malware checks + ClamAV), csv_import, text (normalise/versions/queries), matching, quality (reported),
                    audio_analysis (ffprobe/ffmpeg/FFT), bridge (file IPC), service (workflow),
                    library (copy/tag/dupes), rekordbox (XML/M3U8), db (SQLite, WAL)
cratedigger/ui/     PySide6 pages: dashboard, playlist, results, library, activity, settings
nicotine_plugin/    cratedigger_bridge — runs inside Nicotine+
tests/              pytest; nicotine_harness.py loads the plugin against the installed Nicotine+ code
```

**Why a Nicotine+ plugin?** Nicotine+ has no public API. Its plugin system runs inside the client and can call
the same search and download functions as the GUI, so CrateDigger reuses your logged-in session instead of
opening a second Soulseek connection with the same account (the server would disconnect one of them).
Alternatives considered: `aioslsk` (a maintained Python Soulseek client: would need a separate account) and
`slskd` (a .NET daemon with REST API: extra runtime). The `bridge`/`service` split keeps a second backend
possible later.

The plugin talks to CrateDigger through JSON files in `~/Library/Application Support/CrateDigger/bridge/`:
requests in `requests/`, events in `results/*.jsonl`, status in `heartbeat.json`.

## Tests

```bash
.venv/bin/python -m pytest -q tests
```

Covers matching edge cases, quality flags, audio analysis on generated genuine/transcoded/upscaled/disguised
files, CSV import, and an end-to-end run of the real plugin code with simulated network messages
(search → results → duplicate detection → authorisation gate → download progress → analysis → approval →
tagging → Rekordbox XML).

## Updates

CrateDigger checks this repository's [Releases](https://github.com/vmihai2009-jpg/CrateDigger/releases) a few
seconds after it starts. When a newer version is published it asks whether to **Install and restart**, wait until
**Later**, or **Skip this version**. Installing downloads the release's code, checks it is the version it claims to be,
and replaces the code inside `CrateDigger.app`. Your database, settings, library and downloads are not touched. If
anything goes wrong, the current version stays in place. You can turn the start-up check off, or check by hand, under
**Settings → Updates**.

To publish an update:
1. Raise `__version__` in `cratedigger/__init__.py` (for example `0.2.0` → `0.2.1`) and merge that to `main`.
2. On GitHub, open **Releases → Draft a new release**, create the tag `v0.2.1` (it must match, with a leading `v`),
   write a few lines about what changed, and press **Publish release**.

Apps built before 0.2.0 have no updater. Rebuild them once with `./scripts/build_app.sh --install`.
