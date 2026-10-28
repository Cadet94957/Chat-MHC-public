"""Security regression tests: headers, cookies, rate limits, input validation."""


def test_security_headers_on_static_and_api(client):
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    api = client.get("/api/chats")  # 401, but headers still apply
    assert api.headers["Cache-Control"] == "no-store"
    assert api.headers["X-Content-Type-Options"] == "nosniff"


def test_session_cookie_flags(client):
    res = client.post("/api/login",
                      json={"username": "tester", "password": "secret"})
    assert res.status_code == 200
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    # conftest sets COOKIE_SECURE=false for http TestClient; in production the
    # default (true) adds "; Secure" — covered by config default test below.


def test_cookie_secure_defaults_on(monkeypatch):
    import importlib
    from app import config
    # With no env override, Secure cookies must be the default.
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    importlib.reload(config)
    assert config.COOKIE_SECURE is True
    monkeypatch.setenv("COOKIE_SECURE", "false")
    importlib.reload(config)
    assert config.COOKIE_SECURE is False


def test_login_rate_limited(client):
    for _ in range(10):
        res = client.post("/api/login",
                          json={"username": "x", "password": "wrong"})
        assert res.status_code == 401
    res = client.post("/api/login",
                      json={"username": "tester", "password": "secret"})
    assert res.status_code == 429
    assert "Retry-After" in res.headers


def test_non_ascii_credentials_rejected_cleanly(client):
    # Non-ASCII input must yield 401, not a 500 from hmac.compare_digest.
    res = client.post("/api/login",
                      json={"username": "tester", "password": "pässwörd"})
    assert res.status_code == 401


def test_overlong_inputs_rejected(client, authed):
    res = client.post("/api/login",
                      json={"username": "x" * 300, "password": "y"})
    assert res.status_code == 422

    chat = authed.post("/api/chats", json={}).json()
    res = authed.post(f"/api/chats/{chat['id']}/messages",
                      json={"input": "x" * 5000})
    assert res.status_code == 422


def test_message_to_unknown_chat_is_404(authed):
    res = authed.post("/api/chats/no-such-chat/messages",
                      json={"input": "hello"})
    assert res.status_code == 404


def test_placeholder_secrets_refuse_startup(tmp_path, monkeypatch):
    import importlib
    import pytest
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHATBOT_PASSWORD", "change-me")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    from app import config
    importlib.reload(config)
    with pytest.raises(RuntimeError, match="placeholder"):
        config.check_security_config()


def test_download_filename_is_header_safe(authed):
    from app import config, db
    pdf = config.FORMS_DIR / "evil.pdf"
    config.ensure_dirs()
    pdf.write_bytes(b"%PDF-1.4 test")
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO forms (title, form_number, source_url, file_path)"
            " VALUES (?, ?, ?, ?)",
            ('Bad"Name\r\nSet-Cookie: x=y', "66", "https://mhco.org", "forms/evil.pdf"))
        form_id = cur.lastrowid
    res = authed.get(f"/api/forms/{form_id}/download")
    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert "\n" not in disposition and '\\"' not in disposition
    assert "set-cookie" not in res.headers


def test_download_path_containment(authed):
    from app import db
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO forms (title, form_number, source_url, file_path)"
            " VALUES (?, ?, ?, ?)",
            ("Traversal", "77", "https://mhco.org", "../../../etc/passwd"))
        form_id = cur.lastrowid
    res = authed.get(f"/api/forms/{form_id}/download")
    assert res.status_code == 404


def test_fts_query_ignores_special_chars():
    from app import retrieval
    # Quotes/operators in user text must not reach FTS5 as syntax.
    q = retrieval._fts_query('rent" OR x; DROP TABLE chunks --')
    assert '"' + 'rent' + '"' in q
    for token in q.split(" OR "):
        assert token.startswith('"') and token.endswith('"')
