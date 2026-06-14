from fastapi.testclient import TestClient


def test_health(monkeypatch):
    # init_pool is skipped in unit context; health needs no DB.
    import app.main as m

    async def _noop():  # avoid real DB connection at startup
        return None

    monkeypatch.setattr(m, "init_pool", _noop)
    monkeypatch.setattr(m, "close_pool", _noop)
    with TestClient(m.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "ficium-portal-api"
