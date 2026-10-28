"""Regression tests for keeping the knowledge base intact when mhco.org blocks
the scraper, and for retrieval relevance."""
import sqlite3
from unittest import mock

import pytest
import requests


def _resp(status, headers=None, text="<html></html>"):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = text.encode()
    r.url = "https://mhco.org/x"
    return r


def test_snapshot_guard_refuses_shrunken_export():
    from ingest.snapshot import SnapshotShrankError, check_not_shrunk
    old = {"article": 235, "law": 2, "member": 33}
    with pytest.raises(SnapshotShrankError):
        check_not_shrunk({"law": 2}, old)                    # articles vanished
    with pytest.raises(SnapshotShrankError):
        check_not_shrunk({"article": 100, "law": 2}, old)    # more than half lost
    check_not_shrunk({"article": 200, "law": 2}, old)        # normal churn is fine
    check_not_shrunk({"article": 5}, {})                     # nothing published yet


def test_export_snapshot_keeps_existing_kb_when_content_drops(authed, monkeypatch, tmp_path):
    from app import config, db
    from ingest import snapshot
    snap_dir = tmp_path / "kb-guard"
    monkeypatch.setattr(config, "KB_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(snapshot, "SNAPSHOT_DB", snap_dir / "chatmhc.db")
    monkeypatch.setattr(snapshot, "SNAPSHOT_FORMS", snap_dir / "forms")
    snapshot.export_snapshot()                       # publish seed content
    published = (snap_dir / "chatmhc.db").read_bytes()

    with db.connect() as conn:                       # simulate a blocked scrape
        conn.execute("DELETE FROM documents WHERE type = 'article'")
    with pytest.raises(snapshot.SnapshotShrankError):
        snapshot.export_snapshot()
    assert (snap_dir / "chatmhc.db").read_bytes() == published


def test_rate_limit_stops_crawl_and_keeps_articles(authed, monkeypatch):
    from app import db
    from ingest import polite, scraper as scraper_mod
    sleeps = []
    http = polite.PoliteSession(delay=0, sleep=sleeps.append)
    request = mock.Mock(return_value=_resp(429, {"Retry-After": "5"}))
    monkeypatch.setattr(http.session, "request", request)
    s = scraper_mod.MHCOScraper(username="", password="", http=http)
    monkeypatch.setattr(s, "article_urls",
                        lambda path, *a, **kw: ([f"/a/{i}" for i in range(50)], True))

    with db.connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type='article'").fetchone()[0]
        result = s.scrape_articles(conn)
        after = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type='article'").fetchone()[0]
    assert result.status == "blocked"
    assert before > 0 and after == before
    # Retry-After honored a few times on the first page, then the whole crawl
    # stops: none of the other 49 pages is requested (no hammering).
    assert sleeps and all(w == 5 for w in sleeps)
    assert request.call_count == http.retries + 1
    assert {c.args[1] for c in request.call_args_list} == {"https://mhco.org/a/0"}


def test_sections_filed_under_their_own_number_and_cross_refs_kept():
    from ingest.ors import parse_chapter_html, section_chunks
    html = ("<html><body><p>Chapter 90 — Residential Landlord and Tenant</p>"
            "<p>90.527 Renter's insurance</p><p>90.530 Pets in facilities</p>"
            "<p><b>90.527 Renter's insurance.</b> (1) Rules apply. See ORS "
            "90.530 for pets.</p>"
            "<p><b>90.530 Pets in facilities.</b> (1) A tenant may keep a pet.</p>"
            "</body></html>")
    sections = parse_chapter_html(html, "90")["sections"]
    chunks = [c for sec in sections for c in section_chunks(sec)]
    assert [c["section_id"] for c in chunks] == ["ORS 90.527", "ORS 90.530"]
    assert "See ORS 90.530 for pets." in chunks[0]["text"]  # cross-reference not split
    assert chunks[1]["text"].startswith("90.530 Pets in facilities")


def test_fts_query_drops_question_words():
    from app.retrieval import _fts_query
    q = _fts_query("how can I implement a new pet policy")
    for noise in ('"how"', '"can"', '"implement"', '"new"'):
        assert noise not in q
    assert '"pet"' in q and '"policy"' in q
    assert _fts_query("how do I") != ""      # all-stopword questions still search


def test_search_ranks_by_relevance_within_type(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config
    importlib.reload(config)
    from app import db, retrieval
    importlib.reload(db)
    db.init_db()
    with db.connect() as conn:
        # Unrelated filler so "pet" is a rare (meaningful) word, as in real data.
        for i in range(10):
            db.add_document(conn, type="article", title=f"Other {i}",
                            chunks=[{"text": f"Water billing and utilities {i}."}])
        db.add_document(conn, type="article", title="Fences",
                        chunks=[{"text": "Fence height rules for the park. "
                                          "Mention of a pet once."}])
        db.add_document(conn, type="article", title="Pets",
                        chunks=[{"text": "Pet policy: pets, pet deposits, pet "
                                          "rules, and assistance animals."}])
        results = retrieval.search(conn, "pet policy")
    assert results[0]["title"] == "Pets"
    assert results[0]["score"] > results[1]["score"]


# ---------- incremental articles ----------

def test_incremental_scrape_fetches_only_new_articles(authed, monkeypatch):
    from bs4 import BeautifulSoup
    from app import config, db
    from ingest import scraper as scraper_mod
    base = "https://mhco.org"
    old_url = f"{base}/news-and-resources/community-updates/old"
    new_url = f"{base}/news-and-resources/community-updates/new"
    with db.connect() as conn:
        db.add_document(conn, type="article", title="Old", source_url=old_url,
                        chunks=[{"text": "Old article text."}])

    listing_pages = []
    def fake_get(path):
        listing_pages.append(path)
        # Newest-first listing: page 0 has the new article; page 1 holds
        # only the stored one, so paging stops there and page 2 (which would
        # list an unseen older article) is never read.
        row = '<div class="views-row"><h2><a href="{}">t</a></h2></div>'
        pages = {"": row.format(new_url), "?page=1": row.format(old_url),
                 "?page=2": row.format(f"{base}/x/never-read")}
        return BeautifulSoup(pages[path[len(config.MHCO_UPDATES_PATH):]],
                             "html.parser")
    fetched = []
    def fake_fetch(url):
        fetched.append(url)
        return {"title": "New", "date": None, "author": None, "url": url,
                "content": "A brand new MHCO update about pet rules."}

    s = scraper_mod.MHCOScraper(base_url=base, username="", password="")
    monkeypatch.setattr(s, "_get", fake_get)
    monkeypatch.setattr(s, "fetch_article", fake_fetch)
    with db.connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type='article'").fetchone()[0]
        result = s.scrape_articles(conn)
        assert result.status == "updated" and "added 1 new" in result.detail
        titles = {r[0] for r in conn.execute(
            "SELECT title FROM documents WHERE type='article'")}
        after = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type='article'").fetchone()[0]
    assert fetched == [new_url]                 # the stored article isn't re-downloaded
    assert listing_pages == [config.MHCO_UPDATES_PATH,    # stopped at the first
                             config.MHCO_UPDATES_PATH + "?page=1"]  # all-known page
    assert {"Old", "New"} <= titles
    # Seed placeholders are cleared once real articles arrive; real ones stay.
    assert after >= 2 and after <= before + 1


# ---------- official statutes ----------

def _official_page(sections: int, chapter: str = "90") -> bytes:
    """Mimics oregonlegislature.gov chapter pages: Word-exported HTML in
    windows-1252, a table of contents, then one <p> per heading/paragraph."""
    toc = "".join(f"<p class=MsoNormal>{chapter}.{100 + i} Title {i}</p>"
                  for i in range(sections))
    body = "".join(
        f"<p class=MsoNormal><b>{chapter}.{100 + i} Title {i}.</b> (1) Rule text "
        f"for section {i}, see ORS {chapter}.{100 + (i + 1) % sections} for more.</p>"
        f"<p class=MsoNormal>(2) Landlord’s duty. Second paragraph {i}.</p>"
        f"<p class=MsoNormal>[1973 c.559 §{i}]</p>"
        for i in range(sections))
    html = ('<html><head><meta http-equiv=Content-Type content="text/html; '
            'charset=windows-1252"></head><body>'
            f"<p class=MsoNormal><b>Chapter {chapter} — Residential Landlord "
            f"and Tenant</b></p><p>2025 EDITION</p>{toc}{body}</body></html>")
    return html.encode("windows-1252")


def _ok(content: bytes):
    r = requests.Response()
    r.status_code = 200
    r._content = content
    return r


def test_official_chapter_parsing():
    from ingest.ors import parse_chapter_html, section_chunks
    parsed = parse_chapter_html(_official_page(30), "90")
    assert parsed["edition"] == "2025" and parsed["real_sections"] == 30
    chunks = [c for sec in parsed["sections"] for c in section_chunks(sec)]
    assert len(chunks) == 30
    sec = next(c for c in chunks if c["section_id"] == "ORS 90.105")
    assert sec["text"].startswith("90.105 Title 5. (1) Rule text")
    assert "Landlord’s duty. Second paragraph 5." in sec["text"]   # charset + nbsp
    assert "see ORS 90.106 for more" in sec["text"]   # cross-reference not split


def test_failed_chapter_keeps_its_stored_text(authed, monkeypatch):
    """ORS 90 comes from the official site; ORS 91's official page fails, so
    the stored ORS 91 text must be kept (mhco.org blocks GitHub, so there is
    no MHCO copy to fall back to)."""
    from app import config, db
    from ingest import ors
    monkeypatch.setitem(config.ORS_CHAPTERS["90"], "min_sections", 20)
    with db.connect() as conn:
        db.add_document(conn, type="law", title="ORS 91: Tenancy",
                        source_url="https://mhco.org/forms-oregon-laws/ors-91-tenancy",
                        chunks=[{"section_id": "ORS 91.010", "text": "91.010 Kept."}])

    class FakeHTTP:
        def get(self, url, **kw):
            if "ors090" in url:
                return _ok(_official_page(25))
            raise requests.ConnectionError("blocked")

    with db.connect() as conn:
        results = ors.update_laws(conn, FakeHTTP())
        docs = dict(conn.execute(
            "SELECT title, source_url FROM documents WHERE type='law'").fetchall())
        seed_laws = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type='law' AND is_seed=1").fetchone()[0]
    assert [r.status for r in results] == ["updated", "failed"]
    ors90 = "ORS Chapter 90 — Residential Landlord and Tenant (2025 Edition)"
    assert "oregonlegislature.gov" in docs[ors90]
    assert "ORS 91: Tenancy" in docs           # failed chapter kept, not wiped
    assert seed_laws == 0
