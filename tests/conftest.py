import importlib
import os

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHATBOT_USERNAME", "tester")
    monkeypatch.setenv("CHATBOT_PASSWORD", "secret")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-0123456789-0123456789")
    # TestClient talks plain http, so Secure cookies would never be sent back.
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from app import config
    importlib.reload(config)
    # Never restore the repo's committed kb/ snapshot: tests run on seed data.
    monkeypatch.setattr(config, "KB_SNAPSHOT_DIR", tmp_path / "kb")
    import ingest.snapshot
    importlib.reload(ingest.snapshot)
    import app.db, app.auth, app.retrieval, app.llm
    importlib.reload(app.db)
    importlib.reload(app.auth)
    importlib.reload(app.retrieval)
    importlib.reload(app.llm)
    import app.main
    importlib.reload(app.main)

    from fastapi.testclient import TestClient
    with TestClient(app.main.app) as tc:
        yield tc


@pytest.fixture()
def authed(client):
    res = client.post("/api/login",
                      json={"username": "tester", "password": "secret"})
    assert res.status_code == 200
    return client
