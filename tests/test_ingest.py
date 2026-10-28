"""Knowledge-base updater tests: official ORS parsing, polite HTTP / circuit
breaker, incremental MHCO scraping, and the never-lose-content guards.
All offline — HTTP is faked."""
import json
import sqlite3

import pytest
import requests

from app import config, db


# ---------- fixtures & fakes ----------

@pytest.fixture()
def kb(tmp_path, monkeypatch):
    """A fresh, empty knowledge-base DB in a temp data dir."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "FORMS_DIR", tmp_path / "data" / "forms")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "chatmhc.db")
    db.init_db()
    conn = db.connect()
    yield conn
    conn.close()


def _response(status=200, body=b"", headers=None, url="https://example.test/"):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body if isinstance(body, bytes) else body.encode("utf-8")
    resp.headers.update(headers or {})
    resp.url = url
    resp.encoding = "utf-8"
    return resp


class FakeHTTP:
    """Stands in for PoliteSession: serves canned pages, records requests, and
    can simulate the site blocking us at a given URL."""

    def __init__(self, pages, block_at=()):
        from ingest.polite import SiteBlocked
        self._blocked_exc = SiteBlocked
        self.pages = pages
        self.block_at = set(block_at)
        self.requested = []
        self.blocked = False
        self.session = requests.Session()

    def get(self, url, **kwargs):
        if self.blocked:
            raise self._blocked_exc(f"{url}: skipped")
        self.requested.append(url)
        if url in self.block_at:
            self.blocked = True
            raise self._blocked_exc(f"{url}: HTTP 429 after 3 retries")
        if url not in self.pages:
            resp = _response(404, b"not found", url=url)
            resp.raise_for_status()
        return _response(200, self.pages[url], url=url)

    post = get


# ---------- official ORS parsing ----------

def _ors_fixture() -> bytes:
    subsections = "".join(
        f"<p class=MsoNormal>({i}) “Term {i}” means a defined thing used in this "
        f"chapter, described at length so the section runs long enough to be "
        f"split into several passages for retrieval purposes number {i}.</p>\n"
        for i in range(1, 31))
    html = f"""<html><head>
<meta http-equiv=Content-Type content="text/html; charset=windows-1252">
<title>ORS Chapter 90</title></head><body>
<p class=MsoNormal align=center>Chapter 90 — Residential Landlord and Tenant</p>
<p class=MsoNormal align=center>2025 EDITION</p>
<p class=MsoNormal>RESIDENTIAL LANDLORD AND TENANT</p>
<p class=MsoNormal>GENERAL PROVISIONS</p>
<p class=MsoNormal>90.100\xa0\xa0\xa0\xa0 Definitions</p>
<p class=MsoNormal>90.110\xa0\xa0\xa0\xa0 Exclusions from application of this chapter</p>
<p class=MsoNormal>90.115\xa0\xa0\xa0\xa0 Territorial application</p>
<p class=MsoNormal>_______________</p>
<p class=MsoNormal>GENERAL PROVISIONS</p>
<p class=MsoNormal><b>\xa0\xa0\xa0\xa0\xa0 90.100 Definitions.</b> As used in this
chapter, unless the context otherwise requires:</p>
{subsections}
<p class=MsoNormal>90.105 [1973 c.559 \xa73; repealed by 1995 c.559 \xa761]</p>
<p class=MsoNormal><b>\xa0\xa0\xa0\xa0\xa0 90.110 Exclusions from application of this
chapter.</b> This chapter does not apply to the following, unless created to
avoid its application (see ORS 90.100 \xa7 2):</p>
<p class=MsoNormal>(1) Residence at a public or private institution.</p>
<p class=MsoNormal>Note: 90.110 was amended by section 4, chapter 1, Oregon Laws 2024.</p>
<p class=MsoNormal><b>\xa0\xa0\xa0\xa0\xa0 90.115 Territorial application.</b> This
chapter applies to dwelling units located within this state.</p>
<p class=MsoNormal>90.100 (12) defines the term used in this section.</p>
<p class=MsoNormal>90.110 Exclusions also apply to dwellings covered here.</p>
<p class=MsoNormal>NOTICE TO TENANT: READ THIS CAREFULLY</p>
<p class=MsoNormal>_______________</p>
</body></html>"""
    return html.encode("cp1252")


def _ors91_fixture() -> bytes:
    body = "".join(
        f"<p class=MsoNormal><b>91.{100 + i:03d} Rule {i}.</b> Text of tenancy "
        f"rule number {i}, long enough to be a real section of law.</p>\n"
        for i in range(12))
    html = ("<html><body><p>Chapter 91 — Tenancy</p><p>2025 EDITION</p>"
            + "".join(f"<p>91.{100 + i:03d} Rule {i}</p>" for i in range(12))
            + body + "</body></html>")
    return html.encode("utf-8")


def test_parse_chapter_skips_toc_stubs_and_keeps_text():
    from ingest.ors import parse_chapter_html

    parsed = parse_chapter_html(_ors_fixture(), "90")
    assert parsed["edition"] == "2025"
    assert parsed["title"] == "Residential Landlord and Tenant"
    assert parsed["toc_entries"] == 3
    numbers = [s["number"] for s in parsed["sections"]]
    assert numbers == ["90.100", "90.110", "90.115"]  # 90.105 stub dropped

    by_num = {s["number"]: s for s in parsed["sections"]}
    assert by_num["90.100"]["heading"] == "Definitions."
    s110 = " ".join(by_num["90.110"]["paragraphs"])
    assert "§ 2" in s110                      # windows-1252 decoded
    assert "Note: 90.110 was amended" in s110  # notes stay with their section
    s115 = by_num["90.115"]["paragraphs"]
    # A paragraph starting with an earlier section number is a cross-reference,
    # kept as text; ALL-CAPS notice text is kept; divider rules are dropped.
    assert "90.100 (12) defines the term used in this section." in s115
    assert "90.110 Exclusions also apply to dwellings covered here." in s115
    assert "NOTICE TO TENANT: READ THIS CAREFULLY" in s115
    assert not any(set(p) <= set("_") for p in s115)
    assert parsed["rejected_headings"] == 1


def test_long_sections_split_into_labelled_passages():
    import re
    from ingest.ors import CHUNK_CHARS, parse_chapter_html, section_chunks

    parsed = parse_chapter_html(_ors_fixture(), "90")
    chunks = section_chunks(parsed["sections"][0])
    assert len(chunks) > 1
    assert chunks[0]["section_id"] == "ORS 90.100"
    assert chunks[0]["text"].startswith("90.100 Definitions.")
    labels = [c["section_id"] for c in chunks[1:]]
    # Continuations are labelled by the subsections they hold, e.g.
    # "ORS 90.100(12)–(22)", so citations of different parts differ.
    assert all(re.fullmatch(r"ORS 90\.100\(\d+\)(–\(\d+\))?", lab) for lab in labels)
    assert len(set(labels)) == len(labels)
    for c in chunks[1:]:
        assert c["text"].startswith("90.100 Definitions. (continued)")
    prefix = len("90.100 Definitions. (continued)\n\n")
    assert all(len(c["text"]) <= CHUNK_CHARS + prefix for c in chunks)
    # Nothing lost: every subsection appears exactly once across the passages.
    joined = "\n".join(c["text"] for c in chunks)
    for i in range(1, 31):
        assert joined.count(f"({i}) “Term {i}”") == 1


def _series_fixture() -> bytes:
    """Repealed stub followed by series headings (as Word exports them), and
    an ALL-CAPS line inside a statutory notice."""
    secs = "".join(f"<p><b>90.{510 + i} Rule {i}.</b> Body of rule {i} for parks.</p>"
                   for i in range(4))
    html = f"""<html><body><p>Chapter 90 — Residential Landlord and Tenant</p>
<p>2025 EDITION</p><p>90.505 Definitions</p><p>90.510 Rule 0</p>
<p><b>90.505 Definitions.</b> As used in this series.</p>
<p>90.507 [1991 c.844 §4; repealed by 1995 c.559 §58]</p>
<p>MANUFACTURED DWELLING PARKS AND MARINAS</p>
<p>(General Provisions)</p>
{secs}
<p>NOTICE OF SALE</p>
<p>The landlord must give the tenant this notice in writing.</p>
</body></html>"""
    return html.encode("utf-8")


def test_series_headings_dont_stick_to_sections_or_keep_stubs_alive():
    from ingest.ors import parse_chapter_html

    parsed = parse_chapter_html(_series_fixture(), "90")
    by_num = {s["number"]: s["paragraphs"] for s in parsed["sections"]}
    assert "90.507" not in by_num  # stub dropped despite the headings after it
    assert not any("MANUFACTURED DWELLING PARKS" in p or p == "(General Provisions)"
                   for paras in by_num.values() for p in paras)
    # ALL-CAPS text followed by more body text stays in its section, in order.
    assert by_num["90.513"][-2:] == [
        "NOTICE OF SALE", "The landlord must give the tenant this notice in writing."]
    assert parsed["missing_from_body"] == []


def _laws_setup(monkeypatch, ch91_min=10):
    monkeypatch.setattr(config, "ORS_CHAPTERS", {
        "90": {"url": "https://ors.test/ors090.html",
               "title": "Residential Landlord and Tenant", "min_sections": 3},
        "91": {"url": "https://ors.test/ors091.html", "title": "Tenancy",
               "min_sections": ch91_min},
    })
    return FakeHTTP({"https://ors.test/ors090.html": _ors_fixture(),
                     "https://ors.test/ors091.html": _ors91_fixture()})


def test_update_laws_replaces_legacy_and_skips_unchanged(kb, monkeypatch):
    from ingest.ors import LEGACY_MHCO_LAW_PATHS, update_laws

    http = _laws_setup(monkeypatch)
    legacy90 = "https://mhco.org" + LEGACY_MHCO_LAW_PATHS["90"]
    db.add_document(kb, type="law", title="ORS 90: (MHCO copy)", source_url=legacy90,
                    chunks=[{"section_id": "ORS 90.100", "text": "x" * 40}])
    db.add_document(kb, type="law", title="seed", is_seed=True,
                    source_url="https://ors.test/ors090.html",
                    chunks=[{"section_id": "ORS 90.630", "text": "y" * 40}])
    kb.commit()

    results = update_laws(kb, http)
    assert [r.status for r in results] == ["updated", "updated"]
    docs = kb.execute("SELECT title, source_url, metadata FROM documents"
                      " WHERE type = 'law' ORDER BY title").fetchall()
    assert [d[0] for d in docs] == [
        "ORS Chapter 90 — Residential Landlord and Tenant (2025 Edition)",
        "ORS Chapter 91 — Tenancy (2025 Edition)"]
    assert json.loads(docs[0][2])["edition"] == "2025"
    ids_before = kb.execute("SELECT id FROM documents ORDER BY id").fetchall()

    # Same text again: nothing is rewritten (so no snapshot churn / commit).
    results = update_laws(kb, _laws_setup(monkeypatch))
    assert [r.status for r in results] == ["unchanged", "unchanged"]
    assert kb.execute("SELECT id FROM documents ORDER BY id").fetchall() == ids_before


def test_update_laws_keeps_existing_text_when_parse_looks_incomplete(kb, monkeypatch):
    from ingest.ors import LEGACY_MHCO_LAW_PATHS, update_laws

    http = _laws_setup(monkeypatch, ch91_min=500)
    legacy91 = "https://mhco.org" + LEGACY_MHCO_LAW_PATHS["91"]
    db.add_document(kb, type="law", title="ORS 91: Tenancy", source_url=legacy91,
                    chunks=[{"section_id": "ORS 91.010", "text": "z" * 40}])
    kb.commit()
    results = update_laws(kb, http)
    assert [r.status for r in results] == ["updated", "failed"]
    assert "kept the existing text" in results[1].detail
    assert kb.execute("SELECT COUNT(*) FROM documents WHERE source_url = ?",
                      (legacy91,)).fetchone()[0] == 1


def test_update_laws_refuses_a_page_whose_layout_drifted(kb, monkeypatch):
    from ingest.ors import parse_chapter_html, update_laws

    html = _ors_fixture()
    # (a) The page stops after its table of contents; (b) the first body
    # catchline is formatted differently, so the body start isn't found and
    # every real heading looks like a cross-reference.
    toc_only = html[:html.index(b"_______________")] + b"</body></html>"
    drifted = html.replace(b"90.100 Definitions.</b>", b"90.100. Definitions.</b>")
    for page in (toc_only, drifted):
        assert parse_chapter_html(page, "90")["real_sections"] == 0
        http = _laws_setup(monkeypatch)
        http.pages["https://ors.test/ors090.html"] = page
        monkeypatch.setitem(config.ORS_CHAPTERS["90"], "min_sections", 2)
        db.add_document(kb, type="law", title="good", source_url="https://ors.test/ors090.html",
                        chunks=[{"section_id": "ORS 90.100", "text": "v" * 40}])
        kb.commit()
        results = update_laws(kb, http)
        assert results[0].status == "failed"
        assert kb.execute("SELECT title FROM documents WHERE source_url = ?",
                          ("https://ors.test/ors090.html",)).fetchone()[0] == "good"
        kb.execute("DELETE FROM documents")
        kb.commit()


def test_update_laws_picks_up_a_new_edition_with_unchanged_text(kb, monkeypatch):
    from ingest.ors import update_laws

    assert [r.status for r in update_laws(kb, _laws_setup(monkeypatch))] == \
        ["updated", "updated"]
    http = _laws_setup(monkeypatch)
    for url, page in http.pages.items():
        http.pages[url] = page.replace(b"2025 EDITION", b"2027 EDITION")
    assert [r.status for r in update_laws(kb, http)] == ["updated", "updated"]
    titles = [r[0] for r in kb.execute("SELECT title FROM documents ORDER BY title")]
    assert all(t.endswith("(2027 Edition)") for t in titles)


def test_update_laws_blocked_site_keeps_everything(kb, monkeypatch):
    from ingest.ors import update_laws

    http = _laws_setup(monkeypatch)
    http.block_at = {"https://ors.test/ors090.html"}
    db.add_document(kb, type="law", title="existing", source_url="https://x.test",
                    chunks=[{"section_id": "ORS 90.100", "text": "w" * 40}])
    kb.commit()
    results = update_laws(kb, http)
    assert [r.status for r in results] == ["blocked", "blocked"]
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


# ---------- polite HTTP / circuit breaker ----------

def _polite(monkeypatch, responses, **kwargs):
    from ingest.polite import PoliteSession

    sleeps = []
    http = PoliteSession(delay=0, sleep=sleeps.append, **kwargs)
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(http.session, "request", fake_request)
    return http, sleeps, calls


def test_polite_honors_retry_after_then_succeeds(monkeypatch):
    http, sleeps, calls = _polite(monkeypatch, [
        _response(429, headers={"Retry-After": "7"}), _response(200, "ok")])
    assert http.get("https://site.test/a").text == "ok"
    assert sleeps == [7.0] and len(calls) == 2 and not http.blocked


def test_polite_trips_breaker_and_stops_all_requests(monkeypatch):
    from ingest.polite import SiteBlocked

    http, sleeps, calls = _polite(monkeypatch, [
        _response(429, headers={"Retry-After": "3600"})])
    with pytest.raises(SiteBlocked):
        http.get("https://site.test/login")
    assert http.blocked and sleeps == []  # didn't sit through an hour-long wait
    # Every later request fails fast without touching the network.
    with pytest.raises(SiteBlocked):
        http.get("https://site.test/other")
    assert calls == ["https://site.test/login"]
    # And it's not swallowed by per-page RequestException handlers.
    assert not issubclass(SiteBlocked, requests.RequestException)


def test_polite_backoff_without_retry_after_and_budget(monkeypatch):
    from ingest.polite import SiteBlocked

    http, sleeps, calls = _polite(
        monkeypatch, [requests.ConnectionError("down")] * 4,
        backoff=60.0, wait_budget=600.0)
    with pytest.raises(SiteBlocked):
        http.get("https://site.test/")
    assert sleeps == [60.0, 120.0, 240.0] and len(calls) == 4

    http, sleeps, calls = _polite(
        monkeypatch, [_response(503)] * 4, backoff=60.0, wait_budget=100.0)
    with pytest.raises(SiteBlocked):
        http.get("https://site.test/")
    assert sleeps == [60.0] and len(calls) == 2  # 60+120 would exceed the budget


def test_polite_page_errors_are_not_site_blocks(monkeypatch):
    http, _, _ = _polite(monkeypatch, [_response(404)])
    with pytest.raises(requests.HTTPError):
        http.get("https://site.test/missing")
    assert not http.blocked
    http, _, _ = _polite(monkeypatch, [_response(403)])
    resp = http.post("https://site.test/login", raise_for_status=False)
    assert resp.status_code == 403 and not http.blocked


def test_polite_broken_page_retries_once_without_spending_budget(monkeypatch):
    from ingest.polite import PAGE_ERROR_RETRY_WAIT

    http, sleeps, calls = _polite(monkeypatch, [_response(500), _response(500)],
                                  wait_budget=5.0)
    with pytest.raises(requests.HTTPError):
        http.get("https://site.test/broken")
    assert sleeps == [PAGE_ERROR_RETRY_WAIT] and len(calls) == 2
    assert not http.blocked and http.waited == 0


def test_retry_after_http_date():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    from ingest.polite import retry_after_seconds

    when = datetime.now(timezone.utc) + timedelta(seconds=90)
    secs = retry_after_seconds(_response(429, headers={"Retry-After": format_datetime(when)}))
    assert 80 <= secs <= 91
    assert retry_after_seconds(_response(429)) is None


# ---------- incremental MHCO articles ----------

BASE = "https://mhco.org"
LISTING = BASE + "/news-and-resources/community-updates"


def _listing(*slugs):
    rows = "".join(f'<div class="views-row"><h2><a href="/news-and-resources/'
                   f'community-updates/{s}">{s}</a></h2></div>' for s in slugs)
    return f"<html><body>{rows}</body></html>"


def _article(slug, date="2026-05-04T10:00:00-07:00"):
    meta = f'<meta property="article:published_time" content="{date}">' if date else ""
    return (f"<html><head>{meta}</head><body><h1>Article {slug}</h1>"
            f'<div class="field--name-body"><p>{"Guidance text. " * 20}</p>'
            f"<p>More detail about {slug} for park owners and managers.</p>"
            f"</div></body></html>")


def _url(slug):
    return f"{LISTING}/{slug}"


def _seed_articles(conn, *slugs):
    for s in slugs:
        db.add_document(conn, type="article", title=s, source_url=_url(s),
                        chunks=[{"text": f"Existing article {s} " * 5}])
    conn.commit()


def _scraper(pages, block_at=()):
    from ingest.scraper import MHCOScraper
    http = FakeHTTP(pages, block_at)
    return MHCOScraper(base_url=BASE, username="", password="", http=http), http


def test_incremental_fetches_only_new_articles_and_deletes_nothing(kb):
    _seed_articles(kb, "old1", "old2", "old3")
    scraper, http = _scraper({
        LISTING: _listing("new1", "new2", "old1"),
        LISTING + "?page=1": _listing("old2", "old3"),
        _url("new1"): _article("new1"),
        _url("new2"): _article("new2", date=None),
    })
    result = scraper.scrape_articles(kb)
    assert result.status == "updated" and "added 2" in result.detail
    # Two listing pages (stops at the first page with nothing new) + 2 articles.
    assert http.requested == [LISTING, LISTING + "?page=1", _url("new1"), _url("new2")]
    rows = dict(kb.execute("SELECT source_url, date FROM documents"
                           " WHERE type = 'article'").fetchall())
    assert set(rows) == {_url(s) for s in ("old1", "old2", "old3", "new1", "new2")}
    assert rows[_url("new1")] == "2026-05-04"


def test_incremental_nothing_new_is_one_request(kb):
    _seed_articles(kb, "old1", "old2")
    scraper, http = _scraper({LISTING: _listing("old1", "old2")})
    result = scraper.scrape_articles(kb)
    assert result.status == "unchanged" and http.requested == [LISTING]


def test_blocked_mid_run_keeps_progress_and_existing(kb):
    _seed_articles(kb, "old1")
    scraper, http = _scraper({
        LISTING: _listing("new1", "new2", "old1"),
        _url("new1"): _article("new1"),
    }, block_at={_url("new2")})
    result = scraper.scrape_articles(kb)
    assert result.status == "blocked" and "added 1 of 2" in result.detail
    urls = {r[0] for r in kb.execute("SELECT source_url FROM documents")}
    assert urls == {_url("old1"), _url("new1")}
    assert scraper.blocked


def test_empty_listing_is_a_failure_not_a_wipe(kb):
    _seed_articles(kb, "old1", "old2")
    scraper, _ = _scraper({LISTING: "<html><body>Access denied</body></html>"})
    result = scraper.scrape_articles(kb)
    assert result.status == "failed"
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_full_refresh_prunes_only_after_a_complete_crawl(kb):
    _seed_articles(kb, "a", "b", "c", "d", "e")
    # The complete listing (ends on an empty page) no longer has "e": prune it.
    pages = {LISTING: _listing("a", "b", "c", "d"), LISTING + "?page=1": _listing()}
    pages.update({_url(s): _article(s) for s in "abcd"})
    scraper, _ = _scraper(pages)
    result = scraper.scrape_articles(kb, full=True)
    assert result.status == "updated" and "1 removed" in result.detail
    assert {r[0] for r in kb.execute("SELECT source_url FROM documents")} == \
        {_url(s) for s in "abcd"}

    # A complete listing that has lost most stored articles proves nothing.
    pages = {LISTING: _listing("a", "b"), LISTING + "?page=1": _listing()}
    pages.update({_url(s): _article(s) for s in "ab"})
    scraper, _ = _scraper(pages)
    result = scraper.scrape_articles(kb, full=True)
    assert result.status == "updated" and "0 removed" in result.detail
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 4


def test_full_refresh_never_prunes_listed_but_unreadable_articles(kb):
    slugs = [f"a{i}" for i in range(10)]
    _seed_articles(kb, *slugs)
    # a8/a9 are still listed but their pages fail (403) this run.
    pages = {LISTING: _listing(*slugs), LISTING + "?page=1": _listing()}
    pages.update({_url(s): _article(s) for s in slugs[:8]})
    scraper, _ = _scraper(pages)
    result = scraper.scrape_articles(kb, full=True)
    assert result.status == "updated" and "0 removed" in result.detail
    assert "2 unreadable (stored copies kept)" in result.detail
    assert {r[0] for r in kb.execute("SELECT source_url FROM documents")} == \
        {_url(s) for s in slugs}

    # A listing walk cut short by an error never prunes either.
    pages = {LISTING: _listing(*slugs[:5])}  # ?page=1 -> 404
    pages.update({_url(s): _article(s) for s in slugs[:5]})
    scraper, _ = _scraper(pages)
    result = scraper.scrape_articles(kb, full=True)
    assert "nothing was pruned" in result.detail
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 10


def test_unreadable_new_article_is_not_a_false_alarm(kb):
    _seed_articles(kb, "old1", "old2")
    pages = {LISTING: _listing("pdf-only", "old1"), _url("old1"): _article("old1")}
    scraper, _ = _scraper(pages)  # pdf-only -> 404 (unreadable)
    result = scraper.scrape_articles(kb)
    assert result.status == "unchanged" and "no readable text" in result.detail

    # If stored articles can't be read either, the layout changed: that IS a failure.
    scraper, _ = _scraper({LISTING: _listing("pdf-only", "old1")})
    assert scraper.scrape_articles(kb).status == "failed"


def test_listing_ignores_non_article_links(kb):
    _seed_articles(kb, "old1")
    rows = (_listing("new1", "old1").replace(
        "</body>", '<div class="views-row"><div class="field-content">'
        '<a href="/taxonomy/term/7">Rent</a></div></div></body>'))
    scraper, http = _scraper({LISTING: rows, _url("new1"): _article("new1")})
    assert scraper.scrape_articles(kb).status == "updated"
    assert BASE + "/taxonomy/term/7" not in http.requested


def test_full_refresh_blocked_changes_nothing(kb):
    _seed_articles(kb, "a", "b")
    scraper, _ = _scraper({LISTING: _listing("a", "b"), _url("a"): _article("a")},
                          block_at={_url("b")})
    before = kb.execute("SELECT id, source_url FROM documents ORDER BY id").fetchall()
    result = scraper.scrape_articles(kb, full=True)
    assert result.status == "blocked"
    assert kb.execute("SELECT id, source_url FROM documents ORDER BY id").fetchall() == before


# ---------- members ----------

MEMBERS = BASE + "/membership/associate-members"


def _members_page(n):
    rows = "".join(f'<div class="views-row"><h3>Vendor {i}</h3><p>Vendor {i} '
                   f"provides services to manufactured home parks.</p></div>"
                   for i in range(n))
    return f"<html><body>{rows}</body></html>"


def test_members_replace_unchanged_and_shrink_guard(kb):
    scraper, http = _scraper({MEMBERS: _members_page(10)})
    assert scraper.scrape_members(kb).status == "updated"
    assert http.requested == [MEMBERS]  # no homepage detour when the path works
    ids = kb.execute("SELECT id FROM documents ORDER BY id").fetchall()

    scraper, _ = _scraper({MEMBERS: _members_page(10)})
    assert scraper.scrape_members(kb).status == "unchanged"
    assert kb.execute("SELECT id FROM documents ORDER BY id").fetchall() == ids

    scraper, _ = _scraper({MEMBERS: _members_page(4)})  # less than half: suspicious
    result = scraper.scrape_members(kb)
    assert result.status == "failed"
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 10


def test_members_guard_is_as_strict_as_the_snapshot_guard(kb):
    for i in range(33):
        db.add_document(kb, type="member", title=f"m{i}",
                        chunks=[{"text": f"Existing member {i} description text"}])
    kb.commit()
    scraper, _ = _scraper({MEMBERS: _members_page(16)})  # 16 < 33 * 0.5
    assert scraper.scrape_members(kb).status == "failed"
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 33


# ---------- dates ----------

@pytest.mark.parametrize("raw, expected", [
    ("2026-05-04T10:00:00-07:00", "2026-05-04"),
    ("Fri, 06/12/2026 - 10:00", "2026-06-12"),
    ("Posted June 3, 2026 by MHCO", "2026-06-03"),
    ("Jan. 5, 2025", "2025-01-05"),
    ("13/45/2026", None),
    ("", None),
])
def test_parse_date_variants(raw, expected):
    from ingest.scraper import _parse_date
    assert _parse_date(raw) == expected


# ---------- snapshot guard ----------

def _snapshot_paths(monkeypatch, tmp_path):
    from ingest import snapshot
    snap_dir = tmp_path / "kb"
    monkeypatch.setattr(config, "KB_SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(snapshot, "SNAPSHOT_DB", snap_dir / "chatmhc.db")
    monkeypatch.setattr(snapshot, "SNAPSHOT_FORMS", snap_dir / "forms")
    return snapshot


def test_snapshot_refuses_to_publish_a_big_shrink(kb, monkeypatch, tmp_path):
    snapshot = _snapshot_paths(monkeypatch, tmp_path)
    _seed_articles(kb, *[f"a{i}" for i in range(10)])
    snapshot.export_snapshot()
    published = snapshot.SNAPSHOT_DB.read_bytes()

    kb.execute("DELETE FROM documents WHERE source_url NOT IN (?, ?)",
               (_url("a0"), _url("a1")))
    kb.commit()
    with pytest.raises(snapshot.SnapshotShrankError, match="article: 10 -> 2"):
        snapshot.export_snapshot()
    assert snapshot.SNAPSHOT_DB.read_bytes() == published  # left untouched

    snapshot.export_snapshot(allow_shrink=True)
    snap = sqlite3.connect(snapshot.SNAPSHOT_DB)
    assert snap.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    snap.close()


def test_snapshot_export_leaves_unchanged_content_byte_identical(kb, monkeypatch, tmp_path):
    snapshot = _snapshot_paths(monkeypatch, tmp_path)
    _seed_articles(kb, "a0", "a1")
    assert snapshot.export_snapshot()["unchanged"] is False
    published = snapshot.SNAPSHOT_DB.read_bytes()
    # Same content again (a VACUUM would still change the file's header).
    assert snapshot.export_snapshot()["unchanged"] is True
    assert snapshot.SNAPSHOT_DB.read_bytes() == published
    _seed_articles(kb, "a2")
    assert snapshot.export_snapshot()["unchanged"] is False
    assert snapshot.SNAPSHOT_DB.read_bytes() != published
    assert not snapshot.SNAPSHOT_DB.with_name("chatmhc.db.tmp").exists()


def test_restore_snapshot_cli_refuses_to_overwrite_existing_content(kb, monkeypatch, tmp_path):
    import sys
    from ingest.__main__ import main

    snapshot = _snapshot_paths(monkeypatch, tmp_path)
    _seed_articles(kb, "a0", "a1")
    snapshot.export_snapshot()
    chat = db.create_chat(kb)
    db.add_message(kb, chat["id"], "user", "keep me")
    kb.commit()

    monkeypatch.setattr(sys, "argv", ["ingest", "restore-snapshot"])
    assert main() == 1
    assert kb.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    monkeypatch.setattr(sys, "argv", ["ingest", "restore-snapshot", "--force"])
    assert main() == 0


# ---------- CLI orchestration ----------

def test_scrape_cli_blocked_everywhere_keeps_content(kb, monkeypatch, tmp_path):
    import argparse
    import ingest.ors
    import ingest.scraper
    from ingest.__main__ import run_scrape
    from ingest.polite import SiteBlocked
    from ingest.report import SourceResult

    _seed_articles(kb, "old1", "old2")
    before = kb.execute("SELECT id FROM documents ORDER BY id").fetchall()

    monkeypatch.setattr(ingest.ors, "update_laws", lambda conn: [
        SourceResult("Laws: ORS 90", "unchanged", "fine")])

    class BlockedScraper:
        blocked = True

        def __init__(self, *a, **k):
            pass

        def login(self):
            raise SiteBlocked("https://mhco.org/user/login: HTTP 429")

    monkeypatch.setattr(ingest.scraper, "MHCOScraper", BlockedScraper)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    args = argparse.Namespace(only="articles,laws,members", full=False,
                              limit=None, no_pdfs=False)
    # mhco.org being blocked is reported, not a failed run (MHCO content comes
    # in through the bookmark button); only the laws decide the exit status.
    assert run_scrape(args) == 0
    text = summary.read_text()
    assert "Articles | ⛔ blocked" in text and "Members | ⛔ blocked" in text
    assert "kept as it was" in text
    assert kb.execute("SELECT id FROM documents ORDER BY id").fetchall() == before


def test_scrape_cli_login_page_error_still_scrapes_public_pages(kb, monkeypatch):
    import argparse
    import ingest.ors
    import ingest.scraper
    from ingest.__main__ import run_scrape
    from ingest.report import SourceResult

    monkeypatch.setattr(ingest.ors, "update_laws", lambda conn: [
        SourceResult("Laws: ORS 90", "updated", "fine")])
    calls = []

    class LoginBrokenScraper:
        blocked = False

        def __init__(self, *a, **k):
            pass

        def login(self):
            raise requests.HTTPError("403 Client Error: Forbidden")

        def scrape_articles(self, conn, limit=None, full=False):
            calls.append("articles")
            return SourceResult("Articles", "unchanged", "no new articles")

        def scrape_members(self, conn):
            calls.append("members")
            return SourceResult("Members", "unchanged", "33 members")

    monkeypatch.setattr(ingest.scraper, "MHCOScraper", LoginBrokenScraper)
    args = argparse.Namespace(only="articles,laws,members", full=False,
                              limit=None, no_pdfs=False)
    assert run_scrape(args) == 0
    assert calls == ["articles", "members"]


# ---------- retrieval packing with long statute passages ----------

def test_law_passages_leave_room_for_guidance(kb):
    from app import retrieval

    passages = [{"section_id": "ORS 90.425" + ("" if i == 0 else f"({i})"),
                 "text": f"Abandoned manufactured home disposition part {i}. "
                         + "Landlord storage sale notice. " * 75}
                for i in range(5)]
    passages.append({"section_id": "ORS 90.675",
                     "text": "Abandoned manufactured home in facility notice. " * 40})
    db.add_document(kb, type="law", title="ORS Chapter 90", chunks=passages)
    for i in range(3):
        db.add_document(kb, type="article", title=f"Guide {i}",
                        chunks=[{"text": f"Practical steps when a tenant leaves an "
                                         f"abandoned manufactured home, tip {i}. " * 3}])
    kb.commit()

    chunks = retrieval.search(kb, "abandoned manufactured home")
    laws = [c for c in chunks if c["type"] == "law"]
    assert sum(1 for c in laws if c["section_id"].startswith("ORS 90.425")) <= 2
    assert sum(1 for c in chunks if c["type"] == "article") == 3
    assert sum(len(c["text"]) for c in chunks) <= 12000
    assert len({c["score"] for c in laws}) > 1  # BM25 relevance actually counts

    # The laws browser search sees laws only, without the share cap.
    only_laws = retrieval.search(kb, "abandoned manufactured home", k=20,
                                 max_chars=60000, law_share=1.0, doc_types=("law",))
    assert only_laws and all(c["type"] == "law" for c in only_laws)


def test_scrape_cli_fails_when_laws_cannot_refresh(kb, monkeypatch):
    import argparse
    import ingest.ors
    from ingest.__main__ import run_scrape
    from ingest.report import SourceResult

    monkeypatch.setattr(ingest.ors, "update_laws", lambda conn: [
        SourceResult("Laws: ORS 90", "failed", "only 3 sections recognized")])
    args = argparse.Namespace(only="laws", full=False, limit=None, no_pdfs=False)
    assert run_scrape(args) == 1


# ---------- bookmark-button uploads ----------

def _export(articles=(), members=None, mode="quick", listing=None, **extra):
    data = {"format": "chatmhc-mhco-export", "version": 1,
            "created_at": "2026-10-03T12:00:00.000Z", "origin": BASE,
            "mode": mode, "logged_in": True, "listing_complete": mode == "all",
            "stopped_early": False, "unreadable": 0,
            "listing_urls": listing if listing is not None
            else [a["url"] for a in articles],
            "articles": list(articles), "members": members}
    data.update(extra)
    return data


def _write_export(directory, name, data):
    import gzip
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(gzip.compress(json.dumps(data).encode("utf-8")))
    return path


def _page(slug, **kw):
    return {"url": _url(slug), "html": _article(slug, **kw)}


def test_upload_adds_refreshes_and_backfills_dates(kb, tmp_path):
    from ingest.mhco_import import import_directory

    _seed_articles(kb, "old1")  # stored without a date, like the 235 restored ones
    uploads = tmp_path / "mhco_imports"
    (uploads / "README.md").parent.mkdir(parents=True)
    (uploads / "README.md").write_text("instructions")
    data = _export([_page("new1"), _page("old1"),
                    {"url": "https://www.mhco.org/news-and-resources/community-updates/new2",
                     "html": _article("new2")}],
                   members={"url": MEMBERS, "html": _members_page(5)})
    path = _write_export(uploads, "mhco-export-2026-10-03.json.gz", data)

    results = import_directory(kb, uploads, remove=True)
    by_name = {r.name.split(":")[0]: r for r in results}
    arts = by_name["Articles (upload)"]
    assert arts.status == "updated" and "2 new, 1 refreshed" in arts.detail
    assert by_name["Members (upload)"].status == "updated"
    rows = dict(kb.execute("SELECT source_url, date FROM documents WHERE type = 'article'"))
    # www. URLs are stored under the canonical address; old1 got its date.
    assert set(rows) == {_url("old1"), _url("new1"), _url("new2")}
    assert rows[_url("old1")] == "2026-05-04"
    assert not path.exists() and (uploads / "README.md").exists()

    # The same pages again change nothing (no needless snapshot commit).
    ids = kb.execute("SELECT id FROM documents ORDER BY id").fetchall()
    _write_export(uploads, "mhco-export-2026-10-04.json.gz", data)
    results = import_directory(kb, uploads, remove=True)
    assert [r.status for r in results] == ["unchanged", "unchanged"]
    assert kb.execute("SELECT id FROM documents ORDER BY id").fetchall() == ids


def test_upload_prunes_only_on_a_complete_everything_listing(kb, tmp_path):
    from ingest.mhco_import import import_directory

    slugs = [f"a{i}" for i in range(10)]
    _seed_articles(kb, *slugs)
    uploads = tmp_path / "up"
    # Newest-only upload never prunes, even though it lists few articles.
    _write_export(uploads, "1.json.gz", _export([_page("a0")]))
    import_directory(kb, uploads, remove=True)
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 10

    # Complete "Everything" listing without a9 (page for a8 unreadable): only
    # a9 is removed.
    pages = [_page(s) for s in slugs[:8]] + [{"url": _url("a8"), "html": "<html></html>"}]
    _write_export(uploads, "2.json.gz", _export(pages, mode="all",
                                                listing=[_url(s) for s in slugs[:9]]))
    results = import_directory(kb, uploads, remove=True)
    assert "1 removed" in results[0].detail
    urls = {r[0] for r in kb.execute("SELECT source_url FROM documents")}
    assert urls == {_url(s) for s in slugs[:9]}

    # An "Everything" upload that stopped early prunes nothing.
    _write_export(uploads, "3.json.gz", _export([_page("a0")], mode="all",
                                                stopped_early=True))
    import_directory(kb, uploads, remove=True)
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 9


def test_upload_rejects_wrong_files_and_short_member_lists(kb, tmp_path):
    from ingest.mhco_import import import_directory

    for i in range(10):
        db.add_document(kb, type="member", title=f"m{i}",
                        chunks=[{"text": f"Existing member {i} description text"}])
    kb.commit()
    uploads = tmp_path / "up"
    uploads.mkdir()
    (uploads / "notes.json").write_text('{"hello": 1}')
    _write_export(uploads, "short.json.gz",
                  _export([], members={"url": MEMBERS, "html": _members_page(3)}))
    results = {r.name: r for r in import_directory(kb, uploads, remove=True)}
    assert results["Upload notes.json"].status == "failed"
    assert results["Members (upload): short.json.gz"].status == "failed"
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 10
    assert not list(uploads.iterdir())  # processed files don't linger


def test_import_mhco_cli(kb, tmp_path, monkeypatch):
    import sys
    from ingest.__main__ import main

    uploads = tmp_path / "up"
    monkeypatch.setattr(config, "MHCO_IMPORTS_DIR", uploads)
    monkeypatch.setattr(sys, "argv", ["ingest", "import-mhco", "--remove-imported"])
    assert main() == 0  # no folder: nothing to do
    _write_export(uploads, "good.json.gz", _export([_page("n1")]))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main() == 0
    assert "Uploaded MHCO content" in summary.read_text()
    (uploads / "bad.json").write_text("not json")
    assert main() == 1


def test_upload_older_than_one_already_imported_is_skipped(kb, tmp_path):
    from ingest.mhco_import import import_directory

    uploads = tmp_path / "up"
    _write_export(uploads, "b.json.gz", _export([_page("new1")],
                                                created_at="2026-10-05T10:00:00.000Z"))
    import_directory(kb, uploads, remove=True)
    edited = _page("new1")
    edited["html"] = edited["html"].replace("Guidance text.", "OLD text.")
    _write_export(uploads, "a.json.gz", _export([edited, _page("new2")], mode="all",
                                                created_at="2026-09-01T10:00:00.000Z"))
    results = import_directory(kb, uploads, remove=True)
    assert [r.status for r in results] == ["skipped"]
    assert "was skipped" in results[0].detail
    assert {r[0] for r in kb.execute("SELECT source_url FROM documents")} == {_url("new1")}
    text = " ".join(r[0] for r in kb.execute("SELECT text FROM chunks"))
    assert "OLD text" not in text and not list(uploads.iterdir())


def test_logged_out_upload_only_adds(kb, tmp_path):
    from ingest.mhco_import import import_directory

    slugs = [f"a{i}" for i in range(5)]
    _seed_articles(kb, *slugs)
    teaser = {"url": _url("a0"), "html": _article("a0").replace(
        "Guidance text.", "Members-only teaser.")}
    # Everything, logged out: listing lacks members-only a4; a0 is a teaser.
    _write_export(tmp_path / "up", "x.json.gz",
                  _export([teaser, _page("new1")], mode="all", logged_in=False,
                          listing=[_url(s) for s in slugs[:4]] + [_url("new1")]))
    result = import_directory(kb, tmp_path / "up", remove=True)[0]
    assert result.status == "updated" and "only added new articles" in result.detail
    urls = {r[0] for r in kb.execute("SELECT source_url FROM documents")}
    assert urls == {_url(s) for s in slugs} | {_url("new1")}  # a4 not pruned
    text = " ".join(r[0] for r in kb.execute("SELECT text FROM chunks"))
    assert "teaser" not in text  # stored a0 not overwritten


def test_unreadable_everything_upload_prunes_nothing(kb, tmp_path):
    from ingest.mhco_import import import_directory

    slugs = [f"a{i}" for i in range(5)]
    _seed_articles(kb, *slugs)
    pages = [{"url": _url(s), "html": "<html><body>new layout</body></html>"}
             for s in slugs[:4]]
    _write_export(tmp_path / "up", "x.json.gz",
                  _export(pages, mode="all", listing=[_url(s) for s in slugs[:4]]))
    result = import_directory(kb, tmp_path / "up", remove=True)[0]
    assert result.status == "failed"
    assert kb.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 5


def test_damaged_upload_doesnt_block_the_others(kb, tmp_path):
    from ingest.mhco_import import import_directory

    uploads = tmp_path / "up"
    good = _write_export(uploads, "b-good.json.gz", _export([_page("n1")]))
    raw = good.read_bytes()
    (uploads / "a-damaged.json.gz").write_bytes(raw[:10] + b"\x00" * 40 + raw[50:])
    _write_export(uploads, "c-odd.json.gz",
                  _export([_page("n2")], unreadable="lots",
                          created_at="2026-10-04T00:00:00.000Z"))
    results = {r.name: r.status for r in import_directory(kb, uploads, remove=True)}
    assert results["Upload a-damaged.json.gz"] == "failed"
    assert results["Articles (upload): b-good.json.gz"] == "updated"
    assert results["Articles (upload): c-odd.json.gz"] == "updated"
    assert not list(uploads.iterdir())


def test_snapshot_publishes_upload_bookkeeping(kb, monkeypatch, tmp_path):
    snapshot = _snapshot_paths(monkeypatch, tmp_path)
    _seed_articles(kb, "a0")
    snapshot.export_snapshot()
    kb.execute("INSERT INTO kb_meta (key, value) VALUES ('latest_bookmark_export', 'x')")
    kb.commit()
    assert snapshot.export_snapshot()["unchanged"] is False
