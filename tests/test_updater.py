import io
import json
import plistlib
import urllib.error
import zipfile

import pytest

from cratedigger.core import updater


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.10.0") == (1, 10, 0)
    assert updater.parse_version("v1.2") is None
    assert updater.is_newer("v0.10.0", "0.9.9")
    assert not updater.is_newer("v0.2.0", "0.2.0")
    assert not updater.is_newer("nonsense", "0.2.0")


def _api(monkeypatch, payload=None, status=None):
    def fake_get(url, **kw):
        if status:
            raise urllib.error.HTTPError(url, status, "x", {}, None)
        return json.dumps(payload).encode()
    monkeypatch.setattr(updater, "_get", fake_get)


def test_check_latest(monkeypatch):
    _api(monkeypatch, {"tag_name": "v99.0.0", "body": "notes", "html_url": "https://x"})
    rel = updater.check_latest()
    assert rel.version == "99.0.0" and rel.zip_url.endswith("/zipball/v99.0.0") and rel.notes == "notes"
    _api(monkeypatch, {"tag_name": "v0.0.1"})
    assert updater.check_latest() is None
    _api(monkeypatch, {"tag_name": "v99.0.0", "prerelease": True})
    assert updater.check_latest() is None
    _api(monkeypatch, status=404)  # no releases yet
    assert updater.check_latest() is None


def _zip(version, extra=None, req="numpy\n"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        top = "owner-CrateDigger-abc123/"
        zf.writestr(top + "cratedigger/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr(top + "cratedigger/new_module.py", "x = 1\n")
        zf.writestr(top + "nicotine_plugin/cratedigger_bridge/__init__.py", "# new plugin\n")
        zf.writestr(top + "requirements.txt", req)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fake_app(tmp_path):
    res = tmp_path / "CrateDigger.app" / "Contents" / "Resources"
    (res / "app" / "cratedigger").mkdir(parents=True)
    (res / "app" / "cratedigger" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (res / "app" / "nicotine_plugin" / "cratedigger_bridge").mkdir(parents=True)
    (res / "app" / "requirements.txt").write_text("numpy\n")
    with open(res.parent / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleVersion": "0.1.0", "CFBundleShortVersionString": "0.1.0"}, fh)
    return res


def _rel(v="0.3.0"):
    return updater.Release(version=v, tag="v" + v, notes="", page_url="", zip_url="zip")


def test_install_replaces_code(tmp_path):
    res = _fake_app(tmp_path)
    out = updater.install(_rel(), resources=res, download=lambda url: _zip("0.3.0"))
    assert "0.3.0" in out
    assert (res / "app" / "cratedigger" / "new_module.py").exists()
    assert "new plugin" in (res / "app" / "nicotine_plugin" / "cratedigger_bridge" / "__init__.py").read_text()
    assert not (res / "app" / "cratedigger.old").exists()
    with open(res.parent / "Info.plist", "rb") as fh:
        assert plistlib.load(fh)["CFBundleShortVersionString"] == "0.3.0"


def test_install_rejects_wrong_version(tmp_path):
    res = _fake_app(tmp_path)
    with pytest.raises(updater.UpdateError):
        updater.install(_rel(), resources=res, download=lambda url: _zip("0.2.5"))
    assert '"0.1.0"' in (res / "app" / "cratedigger" / "__init__.py").read_text()  # untouched


def test_install_rejects_path_traversal(tmp_path):
    res = _fake_app(tmp_path)
    bad = _zip("0.3.0", extra={"../../evil.txt": "x"})
    with pytest.raises(updater.UpdateError):
        updater.install(_rel(), resources=res, download=lambda url: bad)
    assert not (tmp_path / "evil.txt").exists()


def test_install_keeps_old_code_if_dependencies_fail(tmp_path, monkeypatch):
    res = _fake_app(tmp_path)

    class R:
        returncode, stderr, stdout = 1, "boom", ""
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(updater.UpdateError, match="dependencies"):
        updater.install(_rel(), resources=res, download=lambda url: _zip("0.3.0", req="numpy\nnewdep\n"))
    assert '"0.1.0"' in (res / "app" / "cratedigger" / "__init__.py").read_text()


def test_not_in_bundle_when_run_from_source():
    assert updater.app_resources() is None
