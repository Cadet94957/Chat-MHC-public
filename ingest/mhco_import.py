"""Import MHCO pages collected with the "Grab MHCO updates" bookmark button.

mhco.org blocks GitHub's servers, so the member collects pages in their own
logged-in browser (static/mhco-bookmarklet.js) and uploads the resulting
mhco-export-*.json.gz file to mhco_imports/. This turns those raw pages into
knowledge-base records with the same parsing the web scrape uses, under the
same never-lose-content rules:
  - articles are added, or refreshed when their text changed, one by one;
  - a stored article is removed only when a complete, logged-in "Everything"
    export no longer lists it, and only if that listing still holds most
    stored ones;
  - an export from a logged-out browser only adds articles (members-only ones
    may be missing or cut to a teaser);
  - an export older than one already imported is skipped, so a stale file
    can't undo newer content;
  - the members list is replaced only when plausible and changed.
"""
import gzip
import json
import logging
import math
import zlib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app import config, db
from ingest.report import SourceResult
from ingest.scraper import FULL_REFRESH_MIN_SHARE, MHCOScraper

log = logging.getLogger("ingest.mhco_import")

FORMAT = "chatmhc-mhco-export"
SUPPORTED_VERSIONS = {1}


class ExportError(ValueError):
    pass


def load_export(path: Path) -> dict:
    """Read and validate one bookmark export (.json or gzip-compressed)."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error) as exc:
            raise ExportError(f"not a readable .gz file ({exc})") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"not a bookmark export ({exc})") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ExportError("not a file saved by the Grab MHCO updates button")
    if data.get("version") not in SUPPORTED_VERSIONS:
        raise ExportError(f"unsupported export version {data.get('version')!r}")
    if not isinstance(data.get("articles") or [], list):
        raise ExportError("malformed export (articles is not a list)")
    if not isinstance(data.get("listing_urls") or [], list):
        raise ExportError("malformed export (listing_urls is not a list)")
    if not isinstance(data.get("created_at"), str):
        raise ExportError("malformed export (no created_at)")
    return data


def _canonical(url) -> str | None:
    """Map www./http variants of an mhco.org URL onto MHCO_BASE_URL so one
    article always has one URL; None for anything that isn't mhco.org."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "mhco.org" and not host.endswith(".mhco.org"):
        return None
    base = urlsplit(config.MHCO_BASE_URL)
    return urlunsplit((base.scheme, base.netloc, parts.path.rstrip("/") or "/",
                       parts.query, ""))


def _as_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _import_articles(conn, data: dict, scraper: MHCOScraper) -> SourceResult:
    name = "Articles (upload)"
    items = data.get("articles") or []
    logged_in = data.get("logged_in") is True
    existing = {r[0] for r in conn.execute(
        "SELECT source_url FROM documents WHERE type = 'article' AND is_seed = 0"
        " AND source_url IS NOT NULL")}
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    unreadable = _as_int(data.get("unreadable"))  # pages the browser couldn't load
    parsed = 0
    try:
        for item in items:
            url = _canonical(item.get("url")) if isinstance(item, dict) else None
            html = item.get("html") if isinstance(item, dict) else None
            art = (scraper.parse_article(BeautifulSoup(html, "html.parser"), url)
                   if url and isinstance(html, str) else None)
            if not art:
                unreadable += 1
                continue
            parsed += 1
            if not logged_in and url in existing:
                # Logged out, a members-only page may be just a teaser: never
                # let it overwrite the full stored text.
                counts["unchanged"] += 1
                continue
            counts[MHCOScraper.upsert_article(conn, "article", art)] += 1

        # Prune only on a complete, logged-in "Everything" listing that still
        # contains most stored articles, and only if its pages could be read —
        # never because a page failed to load.
        stale: set[str] = set()
        if data.get("mode") == "all" and data.get("listing_complete") \
                and not data.get("stopped_early") and logged_in and parsed:
            listed = {u for u in map(_canonical, data.get("listing_urls") or []) if u}
            if listed and len(listed & existing) >= \
                    math.ceil(len(existing) * FULL_REFRESH_MIN_SHARE):
                stale = existing - listed
        for url in stale:
            conn.execute("DELETE FROM documents WHERE type = 'article'"
                         " AND source_url = ?", (url,))
        if counts["added"]:
            db.delete_seed_documents(conn, "article")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if items and not parsed:
        return SourceResult(
            name, "failed",
            f"none of the {len(items)} article pages in the file could be read "
            "(MHCO's page layout may have changed); kept existing articles")
    detail = (f"{counts['added']} new, {counts['updated']} refreshed, "
              f"{counts['unchanged']} unchanged")
    if stale:
        detail += f", {len(stale)} removed from the site"
    if unreadable:
        detail += f"; {unreadable} page(s) had no readable text"
    if not logged_in:
        detail += ("; the browser wasn't logged in to mhco.org, so this file "
                   "only added new articles")
    changed = counts["added"] or counts["updated"] or stale
    return SourceResult(name, "updated" if changed else "unchanged", detail)


def _import_members(conn, data: dict) -> SourceResult | None:
    page = data.get("members")
    if not isinstance(page, dict) or not isinstance(page.get("html"), str):
        return None
    rows = MHCOScraper._member_entries(BeautifulSoup(page["html"], "html.parser"))
    if not rows:
        return SourceResult("Members (upload)", "failed",
                            "the members page in the file had no recognizable "
                            "list; kept existing members")
    url = _canonical(page.get("url")) or \
        config.MHCO_BASE_URL.rstrip("/") + config.MHCO_MEMBERS_PATH
    return MHCOScraper.store_members(conn, rows, url, name="Members (upload)")


LATEST_EXPORT_KEY = "latest_bookmark_export"


def import_export(conn, data: dict) -> list[SourceResult]:
    """Import one export, unless it's older than one already imported (a
    stale file would otherwise undo newer articles, edits and members)."""
    created = data["created_at"]
    row = conn.execute("SELECT value FROM kb_meta WHERE key = ?",
                       (LATEST_EXPORT_KEY,)).fetchone()
    if row and row[0] and created < row[0]:
        return [SourceResult(
            "Upload", "skipped",
            f"this file was saved {created[:16].replace('T', ' ')} UTC, before "
            f"one already imported ({row[0][:16].replace('T', ' ')} UTC), so it "
            "was skipped. Click Grab MHCO updates again and upload the new file.")]
    scraper = MHCOScraper(username="", password="")  # parsing only, no network
    results = [_import_articles(conn, data, scraper)]
    members = _import_members(conn, data)
    if members:
        results.append(members)
    conn.execute("INSERT INTO kb_meta (key, value) VALUES (?, ?) ON CONFLICT(key)"
                 " DO UPDATE SET value = excluded.value", (LATEST_EXPORT_KEY, created))
    conn.commit()
    return results


def import_directory(conn, directory: Path, remove: bool = False) -> list[SourceResult]:
    """Import every export in `directory` (oldest first, by file name).

    With `remove`, each export file is deleted once processed — including one
    that couldn't be used, so it isn't retried every run (it stays in git
    history and the run summary names it). One bad file never stops the
    others."""
    files = sorted({p for pattern in ("*.json", "*.gz") for p in directory.glob(pattern)
                    if p.is_file()})
    results: list[SourceResult] = []
    for path in files:
        try:
            file_results = import_export(conn, load_export(path))
        except Exception as exc:  # noqa: BLE001 - report, keep going
            conn.rollback()
            log.warning("upload %s could not be imported: %s", path.name, exc)
            results.append(SourceResult(f"Upload {path.name}", "failed",
                                        f"couldn't be imported: {exc}"))
        else:
            for r in file_results:
                results.append(SourceResult(f"{r.name}: {path.name}", r.status, r.detail))
        if remove:
            path.unlink()
            log.info("removed processed upload %s", path.name)
    return results
