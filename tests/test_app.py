def test_requires_auth(client):
    assert client.get("/api/chats").status_code == 401
    assert client.get("/api/forms").status_code == 401


def test_bad_login(client):
    res = client.post("/api/login", json={"username": "x", "password": "y"})
    assert res.status_code == 401


def test_seed_loaded_on_startup(authed):
    status = authed.get("/api/status").json()
    assert status["chunks"] > 0
    assert status["forms"] > 0
    assert status["seed_documents"] > 0


def test_retrieval_prefers_law_for_rent_increase(authed):
    from app import db, retrieval
    with db.connect() as conn:
        chunks = retrieval.search(conn, "how much notice for a rent increase?")
    assert chunks, "expected retrieval results"
    assert any(c["type"] == "law" and "90.600" in (c["section_id"] or "")
               for c in chunks[:3])


def test_chat_flow_without_llm_key(authed):
    chat = authed.post("/api/chats", json={}).json()
    res = authed.post(f"/api/chats/{chat['id']}/messages",
                      json={"input": "What form do I use for nonpayment of rent?"})
    assert res.status_code == 200
    body = res.text
    assert "event: sources" in body
    assert "event: delta" in body
    assert "event: done" in body
    # without an API key the fallback answer cites retrieval results
    assert "ANTHROPIC_API_KEY" in body

    history = authed.get(f"/api/chats/{chat['id']}/history").json()["history"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    # first message renames the chat
    chats = authed.get("/api/chats").json()["chats"]
    assert chats[0]["title"].startswith("What form do I use")


def test_forms_listing_and_matching(authed):
    forms = authed.get("/api/forms").json()["forms"]
    assert any("43" == f["form_number"] for f in forms)
    from app import db, retrieval
    with db.connect() as conn:
        matches = retrieval.matching_forms(conn, "tenant is late on rent, nonpayment")
    # The top match must be a nonpayment-of-rent form (e.g. Form 43 or 82A).
    assert matches
    top = f"{matches[0]['title']} {matches[0]['description'] or ''}".lower().replace("-", "")
    assert "nonpayment" in top or "payment" in top


def test_form_download_not_cached(authed):
    # A form indexed without a cached PDF answers 409 with the MHCO source URL.
    from app import db
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO forms (title, form_number, source_url) VALUES (?, ?, ?)",
            ("Uncached Test Form", "99Z", "https://mhco.org/example"))
        form_id = cur.lastrowid
    res = authed.get(f"/api/forms/{form_id}/download")
    assert res.status_code == 409


def test_laws_browser(authed):
    laws = authed.get("/api/laws").json()["laws"]
    assert any("ORS 90.630" == l["section_id"] for l in laws)
    searched = authed.get("/api/laws", params={"q": "sell home in place"}).json()["laws"]
    assert any("90.680" in (l["section_id"] or "") for l in searched)


def test_snapshot_export_and_restore(authed, monkeypatch, tmp_path):
    from app import config, db
    from ingest import snapshot

    # Redirect the snapshot target so the test never mutates the repo's kb/.
    snap_dir = tmp_path / "kb"
    monkeypatch.setattr(config, "KB_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(snapshot, "SNAPSHOT_DB", snap_dir / "chatmhc.db")
    monkeypatch.setattr(snapshot, "SNAPSHOT_FORMS", snap_dir / "forms")

    # Create a chat message in the live DB, then export a snapshot.
    chat = authed.post("/api/chats", json={}).json()
    with db.connect() as conn:
        db.add_message(conn, chat["id"], "user", "test question")
    stats = snapshot.export_snapshot()
    assert stats["chunks"] > 0 and stats["forms"] > 0
    assert config.KB_SNAPSHOT_DIR.joinpath("chatmhc.db").exists()

    # The published snapshot must not carry chat history.
    import sqlite3
    snap = sqlite3.connect(config.KB_SNAPSHOT_DIR / "chatmhc.db")
    assert snap.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert snap.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0
    snap.close()

    # Restoring into a fresh, empty data dir reproduces the content but no chats.
    fresh = tmp_path / "fresh-data"
    monkeypatch.setattr(config, "DATA_DIR", fresh)
    monkeypatch.setattr(config, "FORMS_DIR", fresh / "forms")
    monkeypatch.setattr(config, "DB_PATH", fresh / "chatmhc.db")
    assert snapshot.restore_snapshot() is True
    db.init_db()
    with db.connect() as conn:
        restored = db.content_stats(conn)
        chats = db.list_chats(conn)
    assert restored["chunks"] == stats["chunks"]
    assert restored["forms"] == stats["forms"]
    assert chats == []


def test_recency_factor_prefers_newer():
    from datetime import datetime
    from app import retrieval
    this_year = datetime.now().year
    new = retrieval._recency_factor("article", f"{this_year}-01-01")
    old = retrieval._recency_factor("article", f"{this_year - 8}-01-01")
    assert new > old
    # Laws and undated content are never decayed.
    assert retrieval._recency_factor("law", f"{this_year - 20}-01-01") == 1.0
    assert retrieval._recency_factor("article", None) == 1.0


def test_supersession_demotes_and_flags_older_same_topic():
    from app import retrieval
    shared = ("rent increase notice ninety days written notice affected tenants "
              "facility space effective")
    chunks = [
        {"type": "article", "title": "Rent Increases 2019", "date": "2019-03-01",
         "text": f"{shared} the annual cap is five percent in 2019.", "score": 1.0},
        {"type": "article", "title": "Rent Increases 2024", "date": "2024-03-01",
         "text": f"{shared} the annual cap changed to ten percent in 2024.",
         "score": 1.0},
        {"type": "law", "title": "ORS 90.600", "date": None,
         "text": f"{shared} statutory cap provisions.", "score": 3.0},
    ]
    retrieval._apply_supersession(chunks)
    older = next(c for c in chunks if c["title"].endswith("2019"))
    newer = next(c for c in chunks if c["title"].endswith("2024"))
    law = next(c for c in chunks if c["type"] == "law")
    assert older["is_current"] is False
    assert older["superseded_by"]["date"] == "2024-03-01"
    assert older["score"] < newer["score"]
    assert newer["is_current"] is True
    # Laws are never demoted or flagged.
    assert law["is_current"] is True and law["superseded_by"] is None


def test_import_forms_library(authed, tmp_path, monkeypatch):
    from app import config, db
    from ingest.forms_library import import_forms_library

    lib = tmp_path / "forms_library"
    lib.mkdir()
    (lib / "Form 07 - Pet Agreement.pdf").write_bytes(b"%PDF-1.4 pet agreement")
    (lib / "MHCO Form 43 Notice of Nonpayment.pdf").write_bytes(b"%PDF-1.4 nonpay")
    (lib / "README.md").write_text("ignored")  # non-PDF ignored
    monkeypatch.setattr(config, "FORMS_LIBRARY_DIR", lib)

    with db.connect() as conn:
        n = import_forms_library(conn)
    assert n == 2

    forms = {f["form_number"]: f for f in authed.get("/api/forms").json()["forms"]}
    assert set(forms) == {"07", "43"}
    assert forms["07"]["title"].startswith("MHCO Form 07 — Pet Agreement")
    assert forms["43"]["downloadable"]

    # The PDF is actually served (not a 409 like the un-cached seed forms).
    resp = authed.get(f"/api/forms/{forms['43']['id']}/download")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")


def test_extract_title_from_pdf():
    import pytest
    from pathlib import Path
    from ingest.forms_library import _extract_title_from_pdf

    # A real MHCO park file, named by number only, carries its title in the PDF.
    pdf = Path(__file__).resolve().parent.parent / "forms_library" \
        / "Example_Mobile_Home_Park_-_202.pdf"
    if not pdf.exists():
        pytest.skip("forms_library PDFs not present")
    assert "Vacation Occupancy" in (_extract_title_from_pdf(pdf) or "")

    # A non-PDF / unreadable file must degrade to None, never raise.
    assert _extract_title_from_pdf(__file__) is None


def test_parse_form_name_variants():
    from ingest.forms_library import _parse_form_name
    assert _parse_form_name("Form 01 - Rental Application") == (
        "01", "MHCO Form 01 — Rental Application")
    assert _parse_form_name("MHCO Form 5A Pet Agreement") == (
        "5A", "MHCO Form 5A — Pet Agreement")
    # MHCO's on-demand download naming: "<Park Name> - NN"
    assert _parse_form_name("Example Mobile Home Park - 01") == (
        "01", "MHCO Form 01")
    assert _parse_form_name("Example_Mobile_Home_Park_-_43") == (
        "43", "MHCO Form 43")
    num, title = _parse_form_name("random_handout")
    assert num is None and title == "random handout"


def test_importer_legacy_format(authed, tmp_path):
    import json
    from app import db
    from ingest.importer import import_path
    legacy = {
        "items": [{
            "title": "ORS Chapter 90",
            "url": "https://example.org",
            "sections": [
                {"section_id": "ORS 90.730",
                 "text": "A landlord shall maintain the rented space and the "
                         "facility common areas in every part safe for normal "
                         "and reasonably anticipated uses."},
            ],
        }]
    }
    f = tmp_path / "mhco_laws.json"
    f.write_text(json.dumps(legacy))
    with db.connect() as conn:
        counts = import_path(conn, f)
    assert counts == {"law": 1}
    with db.connect() as conn:
        row = conn.execute(
            "SELECT section_id FROM chunks WHERE section_id = 'ORS 90.730'"
        ).fetchone()
    assert row is not None
