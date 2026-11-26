import pytest


@pytest.fixture(autouse=True)
def _no_network_freshclam(monkeypatch, request):
    """Tests never run the real freshclam (network); test_daily_update exercises the logic with a fake."""
    if request.node.name.startswith("test_daily_update"):
        return
    from cratedigger.core import service
    from cratedigger.core.security import UpdateResult
    monkeypatch.setattr(service, "update_signatures_if_new_day",
                        lambda force=False: UpdateResult(False, True, "skipped in tests"))
