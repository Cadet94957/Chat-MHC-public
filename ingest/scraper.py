"""Scrapes mhco.org (Drupal) into the knowledge base.

Uses a plain requests session — no Selenium/Chrome required. Logs into the
members area with MHCO_USERNAME/MHCO_PASSWORD, then pulls:
  - community updates / articles: only ones not stored yet, unless a full
    refresh is requested
  - the associate members directory
  - (legacy, not run by the workflow) the MHCO forms index
ORS statutes come from the Legislature's own site instead; see ingest/ors.py.

Nothing stored is deleted until its replacement has been fetched. mhco.org
rate-limits aggressively, so requests go through PoliteSession: paced, honoring
Retry-After, and the first persistent refusal stops every further MHCO request
for the run (SiteBlocked) instead of hammering the site.
"""
import hashlib
import json
import logging
import math
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app import config, db
from ingest import snapshot
from ingest.polite import PoliteSession, SiteBlocked
from ingest.report import SourceResult

log = logging.getLogger("ingest.scraper")

CONTENT_SELECTORS = [
    "div.view-mode-full", ".field--name-body", ".field--type-text-with-summary",
    ".field__item", "article .content",
]
TITLE_SELECTORS = ["#block-salem-page-title h1", ".article-title", "h1.title", "h1"]
# Machine-readable publication dates (Drupal metatag / RDF), tried first.
DATE_ATTR_SELECTORS = [
    'meta[property="article:published_time"]', 'meta[name="dcterms.date"]',
    'meta[name="dcterms.created"]', '[property="schema:dateCreated"][content]',
    '[property="dc:date"][content]', "time[datetime]",
]
DATE_SELECTORS = [".field--name-field-publication-date", ".article-date", "time",
                  ".date", ".field--name-created", ".node__meta"]
AUTHOR_SELECTORS = [".field--name-field-author", ".author-name", ".byline"]
SKIP_TEXT = ["welcome back", "update your profile", "sign out", "contact mhco",
             "skip to main content", "manufactured housing communities of oregon"]

# Routine runs stop walking the article listing at the first page with nothing
# new; this caps how far they go if the listing order is ever unexpected.
INCREMENTAL_MAX_PAGES = 10
# A full refresh only prunes articles that vanished from the site when it read
# at least this share of the articles already stored.
FULL_REFRESH_MIN_SHARE = 0.8


class MHCOScraper:
    def __init__(self, base_url=None, username=None, password=None, delay=5.0,
                 http=None):
        self.base = (base_url or config.MHCO_BASE_URL).rstrip("/")
        self.username = username or config.MHCO_USERNAME
        self.password = password or config.MHCO_PASSWORD
        self.logged_in = False
        self.http = http or PoliteSession(delay=delay,
                                          headers={"Referer": self.base + "/"})
        self.session = self.http.session
        self._date_debugged = False

    @property
    def blocked(self) -> bool:
        """True once mhco.org has refused us persistently this run."""
        return self.http.blocked

    def _get(self, path_or_url: str) -> BeautifulSoup:
        """Paced fetch. Raises requests.HTTPError for a page-level error (e.g.
        404) and SiteBlocked when the site keeps refusing us."""
        url = urljoin(self.base + "/", path_or_url)
        resp = self.http.get(url)
        return BeautifulSoup(resp.text, "html.parser")

    @staticmethod
    def _debug_page(soup: BeautifulSoup, label: str, n: int = 20) -> None:
        """Log page structure so failed selectors can be diagnosed from CI logs."""
        title = soup.title.get_text(strip=True) if soup.title else "(no title)"
        h1 = soup.select_one("h1")
        log.info("DEBUG %s: title=%r h1=%r", label, title,
                 h1.get_text(strip=True) if h1 else None)
        links = [a for a in soup.select("a[href]")
                 if a.get_text(strip=True) and not a["href"].startswith("#")]
        log.info("DEBUG %s: %d links; first %d:", label, len(links), n)
        for a in links[:n]:
            log.info("DEBUG   %-60s -> %s", a.get_text(strip=True)[:60], a["href"][:90])
        classes = {}
        for el in soup.select("[class]"):
            for c in el.get("class", []):
                classes[c] = classes.get(c, 0) + 1
        common = sorted(classes.items(), key=lambda t: -t[1])[:15]
        log.info("DEBUG %s: common classes: %s", label, common)

    # ---------- login ----------

    @staticmethod
    def _find_login_form(soup: BeautifulSoup):
        """Locate the Drupal login form by id, then by any form with a password
        field. Returns the BeautifulSoup form element or None."""
        form = soup.find("form", id="user-login-form")
        if form:
            return form
        for f in soup.find_all("form"):
            if f.find("input", attrs={"name": "pass"}) or \
                    f.find("input", attrs={"type": "password"}):
                return f
        return None

    @staticmethod
    def _build_login_payload(form, username: str, password: str) -> dict:
        """Build the POST body from every named input in the form (carrying
        Drupal's form_build_id / form_id / op), then set the credentials."""
        payload: dict[str, str] = {}
        for inp in form.find_all(("input", "button")):
            name = inp.get("name")
            if not name or name in ("name", "pass"):
                continue
            # Skip the unfilled honeypot 'url' trap if present; keep tokens.
            payload[name] = inp.get("value", "")
        payload.setdefault("form_id", "user_login_form")
        payload.setdefault("op", "Log in")
        payload["name"] = username
        payload["pass"] = password
        return payload

    @staticmethod
    def _drupal_settings(soup: BeautifulSoup) -> dict:
        """Parse the embedded drupalSettings JSON (where Antibot stashes its
        per-session key and the form's real action)."""
        tag = soup.find("script", attrs={
            "type": "application/json",
            "data-drupal-selector": "drupal-settings-json"})
        if not tag or not tag.string:
            return {}
        try:
            return json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _login_succeeded(resp_url: str, page: BeautifulSoup) -> bool:
        """Success = redirected to a user profile, a logout link is present, or
        the login form (password field) is gone. The Antibot interstitial
        (/antibot) is an explicit failure, not success."""
        if resp_url.rstrip("/").endswith("/antibot"):
            return False
        if re.search(r"/user/\d+", resp_url):
            return True
        if page.select_one('a[href*="/user/logout"], a[href*="logout"]'):
            return True
        still_on_login_form = bool(page.find("input", attrs={"name": "pass"})
                                   or page.find("input", attrs={"type": "password"}))
        return not still_on_login_form

    def login(self) -> bool:
        username = (self.username or "").strip()
        password = (self.password or "").strip()
        if not username or not password:
            log.warning("MHCO credentials not set; only public pages will load")
            return False

        soup = self._get(config.MHCO_LOGIN_PATH)
        form = self._find_login_form(soup)
        if not form:
            log.error("MHCO login: no login form found on %s — page title=%r",
                      config.MHCO_LOGIN_PATH,
                      soup.title.get_text(strip=True) if soup.title else None)
            self._debug_page(soup, "login-page")
            self.logged_in = False
            return False

        payload = self._build_login_payload(form, username, password)

        # Antibot module (confirmed structure on mhco.org):
        #   drupalSettings.antibot.forms[form_id] = {"id": ..., "key": "<KEY>"}
        #   <form action="/antibot" data-action="/user/login" class="... antibot">
        # The site's JS copies that key into the empty `antibot_key` field and
        # restores the real action from data-action. We replicate both.
        action = form.get("data-action") or form.get("action") or config.MHCO_LOGIN_PATH
        settings = self._drupal_settings(soup)
        antibot = settings.get("antibot") or {}
        form_html_id = form.get("id")
        markup_key = (payload.get("antibot_key") or "").strip()

        def _entry_field(entry, field):
            if isinstance(entry, dict):
                return entry.get(field)
            if isinstance(entry, str) and field == "action":
                return entry
            return None

        if antibot:
            forms_map = antibot.get("forms")
            forms_map = forms_map if isinstance(forms_map, dict) else {}
            entry = forms_map.get(form_html_id)
            # Resolve the key: markup (usually empty) -> this form's entry ->
            # any form entry -> top-level antibot.key.
            key = markup_key or _entry_field(entry, "key") or antibot.get("key")
            if not key:
                for v in forms_map.values():
                    key = _entry_field(v, "key")
                    if key:
                        break
            if key:
                payload["antibot_key"] = key
            # Resolve the real action: data-action (preferred) already in
            # `action`; otherwise an entry action; never /antibot.
            if action.rstrip("/").endswith("/antibot"):
                action = (_entry_field(entry, "action") or config.MHCO_LOGIN_PATH)
            log.info("Antibot: action=%s, antibot_key=%s", action,
                     "resolved" if payload.get("antibot_key") else "EMPTY")
        elif action.rstrip("/").endswith("/antibot"):
            action = config.MHCO_LOGIN_PATH

        post_url = urljoin(self.base + "/", action)
        resp = self.http.post(
            post_url, data=payload, raise_for_status=False,
            headers={"Referer": urljoin(self.base + "/", config.MHCO_LOGIN_PATH)})
        page = BeautifulSoup(resp.text, "html.parser")
        self.logged_in = resp.ok and self._login_succeeded(resp.url, page)

        if self.logged_in:
            log.info("MHCO login succeeded (url=%s)", resp.url)
        else:
            err = page.select_one(
                ".messages--error, .messages.error, .alert-danger,"
                ' [role="alert"], [role="contentinfo"]')
            on_antibot = resp.url.rstrip("/").endswith("/antibot")
            log.error(
                "MHCO login FAILED — status=%s url=%s%s\n"
                "  posted to=%s, antibot_key=%s, site message: %s",
                resp.status_code, resp.url,
                " (bounced to Antibot wall)" if on_antibot else "",
                action,
                "present" if payload.get("antibot_key") else "EMPTY",
                err.get_text(" ", strip=True)[:300] if err else "(none found)")
            # One-time decisive diagnostics: the full antibot data + form markup
            # tell us whether a usable key exists at all (vs. requiring a browser).
            log.error("DIAG full drupalSettings.antibot = %s",
                      json.dumps(antibot)[:2000])
            log.error("DIAG login form markup (trimmed):\n%s",
                      form.decode()[:2000])
        return self.logged_in


    # ---------- articles / community updates ----------

    def article_urls(self, listing_path: str, max_pages: int = 100,
                     known=frozenset()) -> tuple[list[str], bool]:
        """Article URLs from the paginated listing (newest first), and whether
        the walk reached the true end of the listing (an empty page) rather
        than stopping early on an error, the page cap, or — with `known` (URLs
        already stored) — the first page that has nothing new, which on a
        routine run is after one or two listing pages."""
        prefix = urljoin(self.base + "/", listing_path.strip("/")) + "/"
        urls: list[str] = []
        for page in range(max_pages):
            path = f"{listing_path}?page={page}" if page else listing_path
            try:
                soup = self._get(path)
            except requests.RequestException as exc:
                log.warning("listing page %s failed: %s", page, exc)
                return urls, False
            links = soup.select(
                "div.views-field-title h2.field-content a, .article-title a,"
                " .views-row h2 a, .views-row .field-content a")
            in_site = []
            for a in links:
                href = a.get("href", "")
                if not href:
                    continue
                url = urljoin(self.base, href).split("#", 1)[0]
                if url.startswith(self.base) and url not in in_site:
                    in_site.append(url)  # skips external links in listing rows
            # Articles live under the listing's own path; other in-site links
            # in the rows (tags, attachments) aren't articles. Fall back to any
            # in-site link only if the site's URL scheme ever changes.
            articles = [u for u in in_site if u.startswith(prefix)]
            if in_site and not articles:
                log.warning("listing page %d: no links under %s; using all %d "
                            "in-site links", page + 1, prefix, len(in_site))
                articles = in_site
            new = [u for u in articles if u not in urls]
            if not new:
                return urls, True
            urls.extend(new)
            unseen = [u for u in new if u not in known]
            log.info("listing page %d: %d articles, %d not stored yet (total %d)",
                     page + 1, len(new), len(unseen), len(urls))
            if known and not unseen:
                return urls, False
        if known:
            log.warning("stopped after %d listing pages; older new articles "
                        "(if any) will be picked up next run", max_pages)
        return urls, False

    def _extract_date(self, soup: BeautifulSoup, url: str) -> str | None:
        for sel in DATE_ATTR_SELECTORS:
            el = soup.select_one(sel)
            if el:
                date = _parse_date(el.get("content") or el.get("datetime") or "")
                if date:
                    return date
        for sel in DATE_SELECTORS:
            el = soup.select_one(sel)
            if el:
                date = _parse_date(el.get("datetime")
                                   or el.get_text(" ", strip=True))
                if date:
                    return date
        if not self._date_debugged:
            # Once per run: show what date-like markup the page has, so the
            # selectors can be fixed from the CI log.
            self._date_debugged = True
            cands = soup.select('[class*="date"], [class*="Date"], time, '
                                '[datetime], meta[property*="time"], '
                                'meta[name*="date"], [property*="date"], '
                                '.node__meta, .submitted')[:12]
            log.info("DEBUG no publication date found on %s; %d candidates:",
                     url, len(cands))
            for el in cands:
                log.info("DEBUG   <%s class=%s %s> %r", el.name, el.get("class"),
                         {k: v for k, v in el.attrs.items()
                          if k in ("datetime", "content", "property", "name")},
                         el.get_text(" ", strip=True)[:80])
        return None

    def fetch_article(self, url: str) -> dict | None:
        try:
            soup = self._get(url)
        except requests.RequestException as exc:
            log.warning("article fetch failed %s: %s", url, exc)
            return None
        return self.parse_article(soup, url)

    def parse_article(self, soup: BeautifulSoup, url: str) -> dict | None:
        """Article fields from a page, or None if it has no readable body.
        Shared by the web scrape and the bookmark-button import."""
        def first(selectors):
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    return el
            return None

        title_el = first(TITLE_SELECTORS)
        title = title_el.get_text(strip=True) if title_el else url.rsplit("/", 1)[-1]
        author_el = first(AUTHOR_SELECTORS)
        author = author_el.get_text(strip=True) if author_el else None

        content_el = first(CONTENT_SELECTORS)
        if not content_el:
            return None
        paras = []
        for el in content_el.find_all(["p", "h2", "h3", "h4", "li"]):
            text = el.get_text(" ", strip=True)
            if text and not any(s in text.lower() for s in SKIP_TEXT):
                paras.append(text)
        content = "\n\n".join(paras)
        if len(content) < 100:
            return None
        return {"title": title, "date": self._extract_date(soup, url),
                "author": author, "content": content, "url": url}

    @staticmethod
    def _store_article(conn, doc_type: str, art: dict) -> None:
        db.add_document(
            conn, type=doc_type, title=art["title"], source_url=art["url"],
            date=art["date"], author=art["author"],
            chunks=[{"text": p} for p in art["content"].split("\n\n")],
        )

    @classmethod
    def upsert_article(cls, conn, doc_type: str, art: dict) -> str:
        """Store `art`, replacing a stored copy of the same URL only when it
        differs. Returns "added", "updated" or "unchanged". Doesn't commit."""
        rows = conn.execute(
            "SELECT id, title, date, author FROM documents"
            " WHERE type = ? AND source_url = ?", (doc_type, art["url"])).fetchall()
        if rows:
            doc_id, title, date, author = rows[0][0], rows[0][1], rows[0][2], rows[0][3]
            stored = [r[0] for r in conn.execute(
                "SELECT text FROM chunks WHERE document_id = ? ORDER BY id", (doc_id,))]
            fresh = [p.strip() for p in art["content"].split("\n\n")
                     if len(p.strip()) >= 30]  # what add_document keeps
            if len(rows) == 1 and (title, date, author, stored) == \
                    (art["title"], art["date"], art["author"], fresh):
                return "unchanged"
            conn.execute("DELETE FROM documents WHERE type = ? AND source_url = ?",
                         (doc_type, art["url"]))
        cls._store_article(conn, doc_type, art)
        return "updated" if rows else "added"

    def scrape_articles(self, conn, doc_type="article", listing_path=None,
                        limit=None, full=False) -> SourceResult:
        """Routine mode (default): add only articles not stored yet; never
        deletes. `full=True` re-reads every article (slow — hundreds of
        requests) and replaces them in one transaction at the end."""
        listing_path = listing_path or config.MHCO_UPDATES_PATH
        existing = {r[0] for r in conn.execute(
            "SELECT source_url FROM documents WHERE type = ? AND is_seed = 0"
            " AND source_url IS NOT NULL", (doc_type,))}
        incremental = bool(existing) and not full
        if incremental:
            urls, _ = self.article_urls(listing_path, INCREMENTAL_MAX_PAGES,
                                        known=existing)
        else:
            urls, complete = self.article_urls(listing_path)
        if not urls:
            return SourceResult(
                "Articles", "failed",
                "the article listing returned no links (page layout changed "
                f"or access denied); kept the existing {len(existing)} articles")
        todo = [u for u in urls if u not in existing] if incremental else urls
        if limit:
            todo = todo[:limit]
        if incremental:
            return self._add_new_articles(conn, doc_type, todo, existing)
        return self._refresh_all_articles(
            conn, doc_type, todo, existing,
            listed=set(urls) if complete and not limit else None)

    def _add_new_articles(self, conn, doc_type, todo, existing) -> SourceResult:
        n_existing = len(existing)
        if not todo:
            return SourceResult("Articles", "unchanged",
                                f"no new articles ({n_existing} stored)")
        added = unreadable = 0
        layout_ok = False
        try:
            for url in todo:
                art = self.fetch_article(url)
                if not art:
                    unreadable += 1
                    continue
                self._store_article(conn, doc_type, art)
                conn.commit()  # keep each article even if a later one is blocked
                added += 1
                log.info("added article: %s", art["title"])
            if not added and existing:
                # Every new link failed. Tell "the page layout changed" apart
                # from "these few posts have no readable body" (members-only,
                # PDF-only, deleted) by re-reading articles we already have.
                layout_ok = any(self.fetch_article(u) is not None
                                for u in sorted(existing)[:3])
        except SiteBlocked as exc:
            return SourceResult(
                "Articles", "blocked",
                f"added {added} of {len(todo)} new articles before mhco.org "
                f"refused requests ({exc}); the rest will be fetched next run")
        if not added:
            if existing and layout_ok:
                return SourceResult(
                    "Articles", "unchanged",
                    f"{unreadable} listed article(s) had no readable text "
                    f"(members-only, PDF-only or removed?); {n_existing} stored")
            return SourceResult(
                "Articles", "failed",
                f"found {len(todo)} new articles but none could be read (page "
                f"layout may have changed); kept the existing {n_existing}")
        self._drop_seed(conn, doc_type)
        detail = f"added {added} new article(s); {n_existing + added} stored"
        if unreadable:
            detail += f"; {unreadable} could not be read"
        return SourceResult("Articles", "updated", detail)

    def _refresh_all_articles(self, conn, doc_type, todo, existing,
                              listed) -> SourceResult:
        """Re-read every listed article and swap them in one transaction.

        `listed` is the full set of URLs on the listing when the walk reached
        its true end (else None). Only stored articles absent from a complete
        listing are pruned — never one that is listed but failed to load —
        and only when the listing still contains most of what's stored."""
        fetched = []
        try:
            for i, url in enumerate(todo, 1):
                art = self.fetch_article(url)
                if art:
                    fetched.append(art)
                if i % 10 == 0:
                    log.info("read %d/%d articles", i, len(todo))
        except SiteBlocked as exc:
            return SourceResult(
                "Articles", "blocked",
                f"full refresh stopped after reading {len(fetched)} of "
                f"{len(todo)} articles ({exc}); kept the existing "
                f"{len(existing)} articles unchanged")
        if not fetched:
            return SourceResult(
                "Articles", "failed",
                f"none of {len(todo)} articles could be read; kept the "
                f"existing {len(existing)}")
        floor = math.ceil(len(existing) * FULL_REFRESH_MIN_SHARE)
        stale: set[str] = set()
        if listed is not None and len(listed & existing) >= floor:
            stale = existing - listed
        fetched_urls = {a["url"] for a in fetched}
        try:
            for art in fetched:
                conn.execute("DELETE FROM documents WHERE type = ? AND source_url = ?",
                             (doc_type, art["url"]))
                self._store_article(conn, doc_type, art)
            for url in stale:
                conn.execute("DELETE FROM documents WHERE type = ? AND source_url = ?",
                             (doc_type, url))
            db.delete_seed_documents(conn, doc_type)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        detail = (f"re-read {len(fetched)} of {len(todo)} listed articles; "
                  f"{len(fetched_urls - existing)} new, {len(stale)} removed "
                  "from the site")
        unread = len(todo) - len(fetched)
        if unread:
            detail += f"; {unread} unreadable (stored copies kept)"
        if listed is None:
            detail += "; listing not fully read, so nothing was pruned"
        return SourceResult("Articles", "updated", detail)

    @staticmethod
    def _drop_seed(conn, doc_type: str) -> None:
        db.delete_seed_documents(conn, doc_type)
        conn.commit()

    # ---------- page discovery ----------

    def _candidate_paths(self, keyword: str, configured: str):
        """Yield the configured path first; only if that doesn't pan out,
        fetch the homepage and yield nav links mentioning `keyword`."""
        yield configured
        try:
            home = self._get("/")
        except requests.RequestException as exc:
            log.warning("nav discovery failed: %s", exc)
            return
        seen = {urljoin(self.base + "/", configured)}
        for a in home.select("a[href]"):
            text = a.get_text(" ", strip=True).lower()
            href = a["href"]
            if keyword in text or keyword in href.lower():
                url = urljoin(self.base, href)
                if url.startswith(self.base) and url not in seen:
                    seen.add(url)
                    yield url

    # ---------- forms (legacy) ----------
    # The workflow doesn't run this: MHCO forms are generated per park as
    # expiring downloads, so they're managed by hand in forms_library/.

    def _collect_form_links(self, soup: BeautifulSoup) -> list[tuple[str, str, str]]:
        """Return (title, description, url) candidates found on a forms page."""
        results, seen = [], set()
        # Preferred: Drupal views rows with a heading link.
        for row in soup.select(".views-row"):
            a = row.select_one("h2 a, h3 a, .field-content a, a[href]")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            url = urljoin(self.base, a.get("href", ""))
            if not title or url in seen or not url.startswith(self.base):
                continue
            desc_el = row.select_one("p, .views-field-body, .field--name-body")
            desc = desc_el.get_text(" ", strip=True)[:300] if desc_el else None
            seen.add(url)
            results.append((title, desc, url))
        # Fallback: any link that looks like a form.
        if not results:
            for a in soup.select("a[href]"):
                title = a.get_text(" ", strip=True)
                href = a.get("href", "")
                if len(title) < 4:
                    continue
                if not re.search(r"\bform\b", title, re.I) \
                        and "/form" not in href.lower() \
                        and not href.lower().endswith(".pdf"):
                    continue
                url = urljoin(self.base, href)
                if url in seen or not url.startswith(self.base):
                    continue
                seen.add(url)
                results.append((title, None, url))
        return results

    def scrape_forms(self, conn, download_pdfs=True) -> SourceResult:
        if not self.logged_in:
            log.warning("not logged in — the MHCO forms library is members-only "
                        "and will likely be inaccessible")
        forms: list[tuple[str, str, str]] = []
        for path in self._candidate_paths("form", config.MHCO_FORMS_PATH):
            try:
                soup = self._get(path)
            except requests.RequestException as exc:
                log.warning("forms candidate %s failed: %s", path, exc)
                continue
            # Walk listing pagination if present.
            page_forms = self._collect_form_links(soup)
            page_num = 1
            while page_forms and soup.select_one(
                    'a[rel="next"], li.pager__item--next a, a[title="Go to next page"]'):
                try:
                    soup = self._get(f"{path}?page={page_num}")
                except requests.RequestException:
                    break
                more = self._collect_form_links(soup)
                if not more:
                    break
                page_forms.extend(f for f in more if f not in page_forms)
                page_num += 1
            if page_forms:
                # Ignore generic nav matches; require several form-like entries.
                titled = [f for f in page_forms if re.search(r"\bform\b", f[0], re.I)
                          or f[2].lower().endswith(".pdf")]
                if len(titled) >= 3:
                    log.info("forms found via %s: %d entries", path, len(titled))
                    forms = titled
                    break
            log.warning("no form entries recognized at %s — page structure dump:",
                        path)
            self._debug_page(soup, f"forms[{path}]")
        if not forms:
            return SourceResult("Forms", "failed",
                                "forms scrape found nothing usable; existing forms kept")

        rows = []
        for i, (title, desc, url) in enumerate(forms):
            number = None
            m = re.search(r"form\s*#?\s*(\d+[A-Za-z]?)", title, re.I)
            if m:
                number = m.group(1).upper()
            file_path = (self._cache_form_pdf(url, number or str(i))
                         if download_pdfs else None)
            rows.append((title[:200], number, desc, url, file_path))
        try:
            conn.execute("DELETE FROM forms")
            conn.executemany(
                "INSERT INTO forms (title, form_number, description, source_url,"
                " file_path) VALUES (?, ?, ?, ?, ?)", rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        cached = sum(1 for r in rows if r[4])
        return SourceResult("Forms", "updated",
                            f"{len(rows)} forms ({cached} with cached PDFs)")

    def _cache_form_pdf(self, page_url: str, key: str) -> str | None:
        """Find the PDF link on a form page and store it under data/forms/."""
        try:
            if page_url.lower().endswith(".pdf"):
                pdf_url = page_url
            else:
                soup = self._get(page_url)
                link = soup.select_one(
                    'a[href$=".pdf"], a.file--application-pdf,'
                    ' div.field--name-field-form-pdf a, div.field__item a[href*=".pdf"]')
                if not link:
                    return None
                pdf_url = urljoin(self.base, link["href"])
            resp = self.http.get(pdf_url, timeout=60)
            if not resp.content[:5] == b"%PDF-":
                return None
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
            rel = f"forms/MHCO_Form_{safe}.pdf"
            (config.DATA_DIR / rel).write_bytes(resp.content)
            return rel
        except requests.RequestException as exc:
            log.warning("pdf cache failed for %s: %s", page_url, exc)
            return None

    # ---------- associate members ----------

    @staticmethod
    def _member_entries(soup: BeautifulSoup) -> list[tuple[str, str]]:
        for sel in (".views-row", ".member", "article",
                    "table tbody tr", ".node", ".field__item > div"):
            entries = []
            for row in soup.select(sel):
                text = row.get_text(" ", strip=True)
                if len(text) < 40 or len(text) > 4000:
                    continue
                name_el = row.select_one("h2, h3, h4, strong, b, a, td")
                name = (name_el.get_text(strip=True) if name_el else text)[:80]
                entries.append((name, text[:2000]))
            if len(entries) >= 3:
                return entries
        return []

    def scrape_members(self, conn) -> SourceResult:
        rows_found: list[tuple[str, str]] = []
        page_url = None
        for path in self._candidate_paths("member", config.MHCO_MEMBERS_PATH):
            try:
                soup = self._get(path)
            except requests.RequestException as exc:
                log.warning("members candidate %s failed: %s", path, exc)
                continue
            rows_found = self._member_entries(soup)
            if rows_found:
                page_url = path
                break
            log.warning("no member entries recognized at %s — page structure dump:",
                        path)
            self._debug_page(soup, f"members[{path}]")
        if not rows_found:
            return SourceResult("Members", "failed",
                                "no member listing recognized; kept existing members")

        return self.store_members(conn, rows_found, urljoin(self.base, page_url))

    @staticmethod
    def store_members(conn, rows_found: list[tuple[str, str]], source_url: str,
                      name: str = "Members") -> SourceResult:
        """Replace the members directory with `rows_found` if it's plausible
        (not much shorter than what's stored) and actually changed."""
        existing = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type = 'member' AND is_seed = 0"
        ).fetchone()[0]
        # Same threshold the snapshot guard applies to articles and laws.
        if existing and len(rows_found) < max(
                3, math.ceil(existing * snapshot.MIN_RETAINED_FRACTION)):
            return SourceResult(
                name, "failed",
                f"found only {len(rows_found)} members (had {existing}); "
                "kept the existing list")
        batch_hash = hashlib.sha256(
            json.dumps(rows_found, ensure_ascii=False).encode("utf-8")).hexdigest()
        old = conn.execute(
            "SELECT metadata FROM documents WHERE type = 'member' AND is_seed = 0"
            " LIMIT 1").fetchone()
        if old and json.loads(old[0] or "{}").get("batch_hash") == batch_hash:
            return SourceResult(name, "unchanged", f"{len(rows_found)} members")

        try:
            db.replace_documents_of_type(conn, "member")
            for member, text in rows_found:
                db.add_document(conn, type="member", title=member,
                                source_url=source_url,
                                metadata={"batch_hash": batch_hash},
                                chunks=[{"text": text}])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info("members indexed: %d (via %s)", len(rows_found), source_url)
        return SourceResult(name, "updated",
                            f"{len(rows_found)} members (was {existing})")

_MDY = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_MONTH_DAY_YEAR = re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),?\s+(\d{4})\b")


def _parse_date(raw: str) -> str | None:
    """Find a date anywhere in `raw` (ISO, m/d/Y, or "June 3, 2026") and
    return it as YYYY-MM-DD."""
    raw = (raw or "").strip()
    candidates = []
    if m := _ISO.search(raw):
        candidates.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if m := _MDY.search(raw):
        candidates.append((int(m.group(3)), int(m.group(1)), int(m.group(2))))
    if m := _MONTH_DAY_YEAR.search(raw):
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt)
                candidates.append((d.year, d.month, d.day))
                break
            except ValueError:
                continue
    for y, mo, d in candidates:
        try:
            if 1990 <= y <= 2100:
                return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
