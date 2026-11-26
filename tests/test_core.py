import os
import subprocess
import time
from pathlib import Path

import pytest

from cratedigger.core.audio_analysis import analyze_file
from cratedigger.core.bridge import NicotineBridge
from cratedigger.core.config import Settings
from cratedigger.core.csv_import import read_csv
from cratedigger.core.db import Database
from cratedigger.core.genres import genre_folder_for
from cratedigger.core.matching import TrackRef, score_candidate
from cratedigger.core.quality import assess_reported
from cratedigger.core.text import build_queries, camelot, key_name, version_kind

from . import nicotine_harness as nh

CSV = Path(os.environ.get("CRATEDIGGER_TEST_CSV", Path.home() / "Downloads" / "To_add.csv"))


# ------------------------------------------------------------------ matching
TENSOR = TrackRef("Tensor (LDN Mix)", "Dom Carlo", "Tensor (LDN Mix)", 260294)
ANTI = TrackRef("Anti War Dub", "Mala;Spen G", "Anti War Dub", 382131, "UKHAU2000357")
ALMA = TrackRef("Alma", "JottaFrank", "Alma", 208744)
MILITANT = TrackRef("Militant Don - Vocal", "Mala;Magugu", "Militant Don", 150642)


@pytest.mark.parametrize("track,path,dur,lo,hi", [
    (TENSOR, "@@u\\Dom Carlo\\Dom Carlo - Tensor (LDN Mix).flac", 260, 95, 100),
    (TENSOR, "@@u\\Dom Carlo - Tensor (Original Mix).mp3", 300, 0, 60),          # original instead of remix
    (ANTI, "@@u\\Mala - Anti War Dub.flac", 382, 95, 100),
    (ANTI, "@@u\\Mala - Anti War Dub (Kahn Remix).flac", 382, 0, 65),            # remix instead of original
    (ANTI, "@@u\\Mala\\02 - Anti War Dub (Live at Fabric).mp3", 390, 0, 50),    # live
    (ANTI, "@@u\\Mala - Anti War Dub (Radio Edit).mp3", 200, 0, 60),            # radio edit
    (MILITANT, "@@u\\Malaa - Militant Don (Vocal).mp3", 151, 0, 50),            # similar artist name
    (ALMA, "@@u\\JottaFrank - Alma Mater Dolorosa.wav", 209, 0, 70),            # similar title
    (ALMA, "@@u\\Someone Else - Alma.wav", 209, 0, 50),                         # different artist
    (ANTI, "@@u\\Mala - Anti War Dub [Clips].m4a", 382, 0, 45),                 # preview clip
    (MILITANT, "@@u\\Mala & Magugu - Militant Don (Vocal).flac", 151, 95, 100),
])
def test_match_scores(track, path, dur, lo, hi):
    r = score_candidate(track, path, dur)
    assert lo <= r.score <= hi, (r.score, r.flags)


def test_isrc_tag_confirms():
    r = score_candidate(ANTI, "/x/track01.flac", 382, tag_isrc="UKHAU2000357", tag_artist="Mala", tag_title="Anti War Dub")
    assert r.score >= 95 and r.detail["isrc"] == "match"
    r2 = score_candidate(ANTI, "/x/track01.flac", 382, tag_isrc="GBXXX0000001", tag_artist="Mala", tag_title="Anti War Dub")
    assert r2.score < 80


def test_versions_and_queries():
    assert version_kind("Extended Mix") == "extended"
    assert version_kind("Radio Edit") == "radio edit"
    assert version_kind("Original Mix") == "original"
    assert version_kind("41 Candies Shade K Mix") == "remix"
    assert build_queries("Spoolin - 41 Candies Shade K Mix", "Guau;Shade k")[0] == "Guau Spoolin 41 candies shade k"
    assert build_queries("CUM SE MIȘCĂ", "RAVA;NOSFE")[0] == "RAVA CUM SE MISCA"
    assert build_queries("I Want Ya", "D.A.N")[0] == "DAN I Want Ya"
    assert build_queries("Flick the Switch", "T>I;Inja")[:2] == ["TI Flick the Switch", "Inja Flick the Switch"]
    assert key_name(9, 0) == "Am" and camelot(9, 0) == "8A" and camelot(0, 1) == "8B"


def test_genre_folders():
    rules = Settings().genre_rules
    assert genre_folder_for("uk garage,rap", rules) == "UK Garage"
    assert genre_folder_for("drum and bass,liquid funk,jungle", rules) == "Drum and Bass"
    assert genre_folder_for("dub techno,uk funky", rules) == "Techno"
    assert genre_folder_for("dubstep,dub", rules) == "Dubstep"
    assert genre_folder_for("", rules) == "Unsorted"
    assert genre_folder_for("arabesk", rules) == "Unsorted"


def test_reported_quality_flags():
    q = assess_reported("mp3", 4_000_000, 320, 250)  # 4 MB over 250 s = 128 kbps
    assert q.tier == 3 and any("implies" in f for f in q.flags)
    q = assess_reported("flac", 30_000_000, None, 250)
    assert q.tier == 1 and not q.flags
    q = assess_reported("wav", 10_000_000, None, 250)
    assert any("uncompressed" in f for f in q.flags)


# ------------------------------------------------------------------ audio analysis
@pytest.fixture(scope="module")
def audio_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    f = lambda *a: subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True, cwd=d)  # noqa: E731
    f("-f", "lavfi", "-i", "anoisesrc=c=pink:r=44100:a=0.3:d=40", "-f", "lavfi", "-i", "sine=f=220:d=40",
      "-filter_complex", "[0][1]amix=inputs=2,aformat=channel_layouts=stereo", "-sample_fmt", "s16", "real.flac")
    f("-i", "real.flac", "-b:a", "128k", "lo.mp3")
    f("-i", "lo.mp3", "-sample_fmt", "s16", "fake.flac")
    f("-i", "lo.mp3", "-b:a", "320k", "up320.mp3")
    f("-i", "real.flac", "-b:a", "320k", "real320.mp3")
    (d / "disguised.flac").write_bytes((d / "lo.mp3").read_bytes())
    return d


def test_analysis_verdicts(audio_dir):
    v = {p.name: analyze_file(p, 40) for p in audio_dir.iterdir()}
    assert v["real.flac"].verdict == "ok"
    assert v["real320.mp3"].verdict == "ok"
    assert v["fake.flac"].verdict == "bad"
    assert v["up320.mp3"].verdict == "bad"
    assert v["disguised.flac"].verdict == "bad" and "misleading extension" in v["disguised.flac"].flags[0]


# ------------------------------------------------------------------ CSV
@pytest.mark.skipif(not CSV.exists(), reason="user CSV not present")
def test_read_user_csv():
    rows, errors = read_csv(CSV)
    assert len(rows) == 31 and not errors
    assert all(r["title"] and r["artists"] and r["duration_ms"] for r in rows)


# ------------------------------------------------------------------ plugin + bridge + service, end to end
@pytest.mark.skipif(not nh.available(), reason="Nicotine+.app not installed")
def test_bridge_roundtrip_with_real_nicotine_code(tmp_path):
    from cratedigger.core.service import Service

    bridge_dir = tmp_path / "bridge"
    plugin_dir = tmp_path / "nplugins"
    bridge = NicotineBridge(bridge_dir, plugin_dir)
    bridge.install_plugin()
    plugin, slsk, events = nh.load_plugin(bridge_dir)
    plugin._tick()
    st = bridge.status()
    assert st.installed and st.plugin_running and st.logged_in

    settings = Settings(search_window_s=0.3, search_interval_s=0, library_root=str(tmp_path / "Music" / "CrateDigger"))
    db = Database(tmp_path / "t.sqlite3")
    svc = Service(db, settings, bridge)
    csv = tmp_path / "p.csv"
    csv.write_text("Track URI,ISRC,Track Name,Album Name,Artist Name(s),Duration (ms),Genres,Tempo,Key,Mode\n"
                   "spotify:track:x1,UKHAU2000357,Anti War Dub,Anti War Dub,Mala;Spen G,382131,\"dubstep,dub\",139.0,11,0\n",
                   encoding="utf-8")
    svc.import_csv(csv)
    tid = db.scalar("SELECT id FROM tracks")
    assert svc.queue_search([tid]) == 1

    plugin._tick()  # reads request, starts search
    token, term, mode, switch = plugin.core.searches[-1]
    assert term == "Mala Anti War Dub" and mode == "global" and switch is False
    nh.deliver(slsk, events, "alice", token, [
        ("@@a\\Music\\Mala\\Mala - Anti War Dub.flac", 42_000_000, {1: 382, 4: 44100, 5: 16}),
        ("@@a\\Music\\Mala\\Mala - Anti War Dub (Kahn Remix).mp3", 15_300_000, {0: 320, 1: 383}),
        ("@@a\\Music\\Mala\\cover.jpg", 100_000, {}),
    ])
    nh.deliver(slsk, events, "bob", token, [("@@b\\dl\\Mala - Anti War Dub.flac", 42_000_000, {1: 382})], free=False)
    nh.deliver(slsk, events, "carol", 999999, [("@@c\\other.mp3", 1, {})])  # someone else's search: ignored
    time.sleep(0.35)
    plugin._tick()  # window elapsed -> done
    assert plugin.core.removed == [token]
    svc.process_bridge_events()

    cands = db.q("SELECT * FROM candidates ORDER BY score DESC")
    assert len(cands) == 3  # jpg skipped, carol ignored
    best = cands[0]
    assert best["ext"] == "flac" and best["score"] >= 95 and best["duration_s"] == 382 and best["sample_rate"] == 44100
    remix = [c for c in cands if "Remix" in c["path"]][0]
    assert remix["score"] < 65 and remix["bitrate"] == 320
    dups = [c for c in cands if c["dup_of"]]
    assert len(dups) == 1 and dups[0]["username"] == "bob"  # same size+ext: bob's copy (no free slot) is the duplicate
    assert db.scalar("SELECT status FROM tracks") == "review"

    # Download gate: refused until authorised
    with pytest.raises(PermissionError):
        svc.request_download(best["id"])
    svc.authorize(best["id"], "Uploader / rights holder explicitly authorised sharing", "test")
    svc.request_download(best["id"])
    plugin._tick()
    assert plugin.core.enqueued[0][0] == "alice" and plugin.core.enqueued[0][2] == 42_000_000
    tr = plugin.core.downloads.transfers[("alice", best["path"])]
    tr.status, tr.current_byte_offset = "Transferring", 21_000_000
    plugin._tick()
    svc.process_bridge_events()
    assert db.one("SELECT * FROM candidates WHERE id=?", (best["id"],))["dl_percent"] == 50

    # The plugin also refuses a download request that carries no authorisation
    (bridge_dir / "requests" / "0_x.json").write_text('{"type":"download","id":"d999_x","username":"eve","path":"p"}')
    plugin._tick()
    assert len(plugin.core.enqueued) == 1

    # Finish with a real file and run analysis + approval
    real = tmp_path / "dl" / "Mala - Anti War Dub.flac"
    real.parent.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=c=pink:r=44100:a=0.3:d=382.1",
                    "-ac", "2", "-sample_fmt", "s16", str(real)], check=True)
    plugin.download_finished_notification("alice", best["path"], str(real))
    svc.process_bridge_events()
    assert svc.downloads_to_analyze() == [best["id"]]
    lid = svc.analyze_candidate(best["id"])
    lf = db.one("SELECT * FROM local_files WHERE id=?", (lid,))
    assert lf["codec"] == "flac" and lf["verdict"] == "ok", lf["flags"]
    before = real.stat().st_mtime_ns, real.read_bytes()[:64]
    # Mandatory security gate: without a working virus scanner nothing gets approved
    from cratedigger.core import security
    from cratedigger.core.library import SecurityBlocked
    security._ENGINE_CACHE = (time.time(), security.EngineInfo(False, message="ClamAV not installed"))
    with pytest.raises(SecurityBlocked):
        svc.approve(lid)
    fake = tmp_path / "fakeclamscan"
    fake.write_text("#!/bin/sh\necho OK\nexit 0\n")
    fake.chmod(0o755)
    security._ENGINE_CACHE = (time.time(), security.EngineInfo(True, engine=str(fake), version="1", db_version="1",
                                                               db_age_days=0))
    lib_id = svc.approve(lid)
    assert db.scalar("SELECT av_status FROM local_files WHERE id=?", (lid,)) == "clean"
    entry = db.one("SELECT * FROM library WHERE id=?", (lib_id,))
    assert entry["library_path"].endswith("Dubstep/Mala, Spen G - Anti War Dub.flac")
    assert (real.stat().st_mtime_ns, real.read_bytes()[:64]) == before  # source untouched
    import mutagen
    tags = mutagen.File(entry["library_path"])
    assert tags["ARTIST"] == ["Mala, Spen G"] and tags["ISRC"] == ["UKHAU2000357"] and tags["INITIALKEY"] == ["Bm"]
    from cratedigger.core.library import LibraryError
    with pytest.raises(LibraryError):
        svc.approve(lid)  # duplicate
    from cratedigger.core import rekordbox
    res = rekordbox.export(db, settings)
    xml = Path(res["xml"]).read_text()
    assert res["tracks"] == 1 and "file://localhost/" in xml and 'Name="Dubstep"' in xml
    plugin.unloaded_notification()


@pytest.mark.skipif(not nh.available(), reason="Nicotine+.app not installed")
def test_auto_approve_mode(tmp_path):
    """Auto mode: off by default; when on, best match is selected+queued, and a clean ok file is approved."""
    from cratedigger.core import security
    from cratedigger.core.service import AUTH_BASES, Service
    bridge_dir = tmp_path / "bridge"
    bridge = NicotineBridge(bridge_dir, tmp_path / "np")
    bridge.install_plugin()
    plugin, slsk, events = nh.load_plugin(bridge_dir)
    plugin._tick()
    settings = Settings(search_window_s=0.2, search_interval_s=0, library_root=str(tmp_path / "lib"),
                        playlist_export_root=str(tmp_path / "Tracks"))
    db = Database(tmp_path / "a.sqlite3")
    svc = Service(db, settings, bridge)
    csv = tmp_path / "p.csv"
    csv.write_text("Track URI,Track Name,Album Name,Artist Name(s),Duration (ms),Genres\n"
                   "spotify:track:a1,Anti War Dub,Anti War Dub,Mala;Spen G,20000,dubstep\n", encoding="utf-8")
    svc.import_csv(csv)

    def search_and_answer():
        svc.queue_search([1])
        plugin._tick()
        tok = plugin.core.searches[-1][0]
        nh.deliver(slsk, events, "alice", tok, [("@@a\\Mala - Anti War Dub.flac", 3_500_000, {1: 20})])
        time.sleep(0.25)
        plugin._tick()
        svc.process_bridge_events()

    search_and_answer()  # auto mode off -> only 'review'
    assert db.scalar("SELECT status FROM tracks") == "review" and not plugin.core.enqueued
    settings.auto_approve_best, settings.auto_basis = True, AUTH_BASES[2]
    assert svc.run_auto_sweep() == 1
    plugin._tick()
    assert len(plugin.core.enqueued) == 1
    cand = db.one("SELECT * FROM candidates")
    assert "standing authorisation" in svc.authorization_for(cand["id"])["note"]
    real = tmp_path / "Mala - Anti War Dub.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=c=pink:r=44100:a=0.3:d=20",
                    "-ac", "2", "-sample_fmt", "s16", str(real)], check=True)
    plugin.download_finished_notification("alice", cand["path"], str(real))
    svc.process_bridge_events()
    fake = tmp_path / "clam"
    fake.write_text("#!/bin/sh\necho OK\nexit 0\n")
    fake.chmod(0o755)
    security._ENGINE_CACHE = (time.time(), security.EngineInfo(True, engine=str(fake), version="1", db_version="1",
                                                               db_age_days=0))
    svc.analyze_candidate(cand["id"])
    assert db.scalar("SELECT COUNT(*) FROM library") == 1
    assert db.scalar("SELECT status FROM tracks") == "in_library"
    # Per-playlist export + review page
    from cratedigger.core.rekordbox import export_playlist
    from cratedigger.core.review import write_report
    res = export_playlist(db, settings, 1)
    assert res["copied"] == 1 and (tmp_path / "Tracks" / "p" / "Mala, Spen G - Anti War Dub.flac").exists()
    xml = (tmp_path / "Tracks" / "CrateDigger_rekordbox.xml").read_text()
    assert '<NODE Type="0" Name="mihai"' in xml and '<NODE Type="0" Name="Tracks"' in xml and 'Name="p" Type="1"' in xml
    assert "In library" in write_report(svc, 1).read_text()
    plugin.unloaded_notification()


def test_download_retry_next_sharer(tmp_path):
    from cratedigger.core.service import AUTH_BASES, Service
    bridge = NicotineBridge(tmp_path / "b", tmp_path / "p")
    db = Database(tmp_path / "r.sqlite3")
    svc = Service(db, Settings(), bridge)
    db.exec("INSERT INTO tracks(title, artists, duration_ms, status) VALUES ('Alma','JottaFrank',208744,'review')")
    for u in ("u1", "u2", "u3", "u4"):
        db.exec("INSERT INTO candidates(track_id, source, username, path, size, ext, score, quality_tier, flags) "
                "VALUES (1,'soulseek',?,?,1,'flac',100,1,'[]')", (u, f"@@{u}\\JottaFrank - Alma.flac"))
    svc.authorize(1, AUTH_BASES[2], "x")
    svc.request_download(1, refresh_av=False)
    svc._download_event({"ev": "status", "id": "d1_a", "status": "File not shared."})   # permanent -> retry now
    assert db.scalar("SELECT state FROM candidates WHERE id=1") == "abandoned"
    nxt = db.one("SELECT * FROM candidates WHERE state='queued_download'")
    assert nxt["username"] == "u2" and "automatic retry" in svc.authorization_for(nxt["id"])["note"]
    svc._download_event({"ev": "status", "id": f"d{nxt['id']}_b", "status": "Connection timeout"})  # transient: wait
    assert db.scalar("SELECT state FROM candidates WHERE id=?", (nxt["id"],)) == "failed"
    assert svc.retry_stale_downloads() == 0
    db.exec("UPDATE candidates SET dl_failed_at=0 WHERE id=?", (nxt["id"],))
    assert svc.retry_stale_downloads() == 1                                               # grace over -> u3
    assert db.one("SELECT username FROM candidates WHERE state='queued_download'")[0] == "u3"
    c3 = db.scalar("SELECT id FROM candidates WHERE username='u3'")
    svc._download_event({"ev": "status", "id": f"d{c3}_c", "status": "Banned"})          # 3rd failure: stop
    assert db.scalar("SELECT COUNT(*) FROM candidates WHERE state='queued_download'") == 0
    assert db.scalar("SELECT status FROM tracks") == "review"
