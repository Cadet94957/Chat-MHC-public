"""Oregon Revised Statutes from the Legislature's official website.

Each chapter page (e.g. ors090.html) is a Word-exported HTML document:
  - a header with the edition ("2025 EDITION") and chapter title
    ("Chapter 90 — Residential Landlord and Tenant");
  - a table of contents: one short line per section ("90.100 Definitions");
  - the body: "90.100 Definitions. As used in this chapter ..." followed by
    subsection paragraphs "(1) ...", "Note: ..." paragraphs, and one-line
    stubs for repealed or renumbered sections ("90.105 [1973 c.559 §3; ...]").

parse_chapter_html() is a pure function so it can be tested on fixtures.
update_laws() replaces a chapter only when the parse looks complete AND the
text actually changed, so an unchanged chapter never produces a new commit.
"""
import hashlib
import json
import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app import config, db
from ingest.polite import PoliteSession, SiteBlocked
from ingest.report import SourceResult

log = logging.getLogger("ingest.ors")

# Long sections (ORS 90.100 is ~30k characters) are split into passages of
# about this size so one section can't crowd everything else out of the
# assistant's context.
CHUNK_CHARS = 2500

# Where the old scraper read its ORS copies from on mhco.org. A chapter's legacy
# document is removed once the official text for that chapter loads.
LEGACY_MHCO_LAW_PATHS = {
    "90": "/forms-oregon-laws/ors-90-property-rights-and-transactions",
    "91": "/forms-oregon-laws/ors-91-tenancy",
}

_WS = re.compile(r"\s+")
_EDITION = re.compile(r"\b((?:19|20)\d\d)\s+EDITION\b", re.I)
_DIVIDER = re.compile(r"^[\s_\-–—=*.]*$")
_SUBSECTION = re.compile(r"\s+(?=\(\d+\)\s)")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.replace("\xa0", " ")).strip()


def _blocks(soup: BeautifulSoup) -> list[str]:
    """Paragraph texts in document order, whitespace-normalized."""
    paras = [t for t in (_norm(p.get_text()) for p in soup.find_all("p")) if t]
    if len(paras) >= 20:
        return paras
    # Not paragraph-structured: fall back to visual lines.
    return [t for t in (_norm(x) for x in soup.get_text("\n").split("\n")) if t]


def _key(number: str) -> tuple[int, str]:
    """Sort key for a section number: "90.505" -> (505, ""), "90.1a" -> (1, "a")."""
    m = re.match(r"\d+\.(\d+)([a-z]?)", number)
    return (int(m.group(1)), m.group(2)) if m else (0, "")


def _is_noise(block: str) -> bool:
    """Divider rules between sections. Everything else is kept: ALL-CAPS lines
    are often part of statutory notice text, so they can't be dropped as
    series headings."""
    return bool(_DIVIDER.match(block))


def _is_series_heading(block: str) -> bool:
    """A heading that introduces the next group of sections, e.g.
    "MANUFACTURED DWELLING PARKS AND MARINAS" or "(General Provisions)"."""
    if len(block) > 80:
        return False
    if re.fullmatch(r"\([A-Z][^()]*\)", block):
        return True
    return (block.upper() == block and any(c.isalpha() for c in block)
            and not any(c.isdigit() or c in ":_$" for c in block))


def _heading(body: str) -> str:
    """The section's catchline: text up to the first sentence end."""
    m = re.match(r"(.{1,200}?\.)(?:\s|$)", body)
    return (m.group(1) if m else body[:120]).strip()


def parse_chapter_html(html: bytes | str, chapter: str) -> dict:
    """Parse one ORS chapter page into sections.

    Returns {"chapter", "title", "edition", "sections", "toc_entries",
    "rejected_headings"}; each section is {"number", "heading", "paragraphs"}.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    blocks = _blocks(soup)

    head_text = " ".join(blocks[:60])
    m = _EDITION.search(head_text)
    edition = m.group(1) if m else None
    title = None
    for b in blocks[:60]:
        m = re.search(rf"Chapter\s+0*{re.escape(chapter)}\b\s*[—–-]+\s*(.+)", b, re.I)
        if m:
            title = _EDITION.split(m.group(1))[0].strip(" -–—")
            if not title or len(title) > 120:
                title = None
            break

    # A section line is its number followed by a capitalized catchline
    # ("90.100 Definitions") or a bracketed history note (repealed stubs);
    # "90.100 (12) defines ..." or "90.100 to 90.465 apply" are cross-references.
    head_re = re.compile(
        rf"^({re.escape(chapter)}\.\d{{3}}[a-z]?)(?=[\s\[])\s*([A-Z\[“\"'].*)$")
    heads = [(i, m.group(1)) for i, b in enumerate(blocks)
             if (m := head_re.match(b))]
    result = {"chapter": chapter, "title": title, "edition": edition,
              "sections": [], "toc_entries": 0, "rejected_headings": 0,
              "real_sections": 0, "missing_from_body": []}
    if not heads:
        return result

    # The table of contents lists every section before the body repeats them:
    # the body starts at the second appearance of the first section number.
    first = heads[0][1]
    repeats = [i for i, n in heads if n == first]
    body_start = repeats[1] if len(repeats) > 1 else heads[0][0]
    toc_numbers = [n for i, n in heads if i < body_start]
    result["toc_entries"] = len(toc_numbers)

    raw: list[dict] = []
    current = None
    pending: list[str] = []  # series headings not yet known to be body text
    for b in blocks[body_start:]:
        m = head_re.match(b)
        # Sections run in ascending order; a number that doesn't advance is a
        # cross-reference that happens to start a paragraph, not a new section.
        if m and (current is None or _key(m.group(1)) > _key(current["number"])):
            # Headings right before a section introduce the next series; they
            # belong to neither section (and would keep a repealed stub alive).
            pending = []
            current = {"number": m.group(1), "body": m.group(2), "paragraphs": [b]}
            raw.append(current)
            continue
        if m:
            result["rejected_headings"] += 1
        if current is None or _is_noise(b):
            continue
        if _is_series_heading(b):
            pending.append(b)
            continue
        # Body text follows, so those heading-like lines were part of the
        # section after all (e.g. ALL-CAPS lines of a statutory notice).
        current["paragraphs"].extend(pending)
        pending = []
        current["paragraphs"].append(b)
    if current is not None:
        current["paragraphs"].extend(pending)

    for s in raw:
        # Repealed/renumbered stubs are just a bracketed history note.
        if len(s["paragraphs"]) == 1 and s["body"].startswith("["):
            continue
        result["sections"].append({"number": s["number"],
                                   "heading": _heading(s["body"]),
                                   "paragraphs": s["paragraphs"]})
    # Sections with a real catchline ("90.100 Definitions. ...") — a contents
    # line mistaken for a section has none, so this is what min_sections checks.
    result["real_sections"] = sum(1 for s in result["sections"]
                                  if s["heading"].endswith("."))
    parsed_numbers = {s["number"] for s in raw}
    result["missing_from_body"] = sorted(set(toc_numbers) - parsed_numbers, key=_key)
    return result


def section_chunks(section: dict, limit: int = CHUNK_CHARS) -> list[dict]:
    """Split one section into passages of about `limit` characters, breaking
    only between paragraphs (or between numbered subsections of an overlong
    paragraph). Continuation passages restate the section number and heading
    so each one is self-explanatory when cited."""
    sid = f"ORS {section['number']}"
    paras: list[str] = []
    for p in section["paragraphs"]:
        paras.extend(_SUBSECTION.split(p) if len(p) > limit * 2 else [p])

    groups: list[list[str]] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        if buf and size + len(p) + 2 > limit:
            groups.append(buf)
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 2
    if buf:
        groups.append(buf)

    chunks = [{"section_id": sid, "text": "\n\n".join(groups[0])}]
    prefix = f"{section['number']} {section['heading']} (continued)"
    for i, g in enumerate(groups[1:], 2):
        # Label continuations by the subsections they hold — "ORS 90.425(3)–(5)"
        # — so citations of different parts of one section are distinguishable.
        subs = [m.group(1) for p in g if (m := re.match(r"\((\d+)\)\s", p))]
        if subs:
            label = f"{sid}({subs[0]})" + (f"–({subs[-1]})" if subs[-1] != subs[0] else "")
        else:
            label = f"{sid} (part {i})"
        chunks.append({"section_id": label,
                       "text": prefix + "\n\n" + "\n\n".join(g)})
    return chunks


def _content_hash(title: str, edition: str | None, chunks: list[dict]) -> str:
    payload = json.dumps([title, edition, [[c["section_id"], c["text"]] for c in chunks]],
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def store_chapter(conn, chapter: str, spec: dict, parsed: dict) -> SourceResult:
    """Replace one chapter's law document, unless nothing (text, title or
    edition) changed."""
    name = f"Laws: ORS {chapter}"
    url = spec["url"]
    chunks = [c for s in parsed["sections"] for c in section_chunks(s)]
    edition = parsed["edition"]
    title = f"ORS Chapter {chapter} — {parsed['title'] or spec['title']}"
    if edition:
        title += f" ({edition} Edition)"
    content_hash = _content_hash(title, edition, chunks)
    n = len(parsed["sections"])
    summary = f"{n} sections, {edition + ' edition' if edition else 'edition unknown'}"

    legacy = []
    if chapter in LEGACY_MHCO_LAW_PATHS:
        legacy.append(urljoin(config.MHCO_BASE_URL.rstrip("/") + "/",
                              LEGACY_MHCO_LAW_PATHS[chapter]))
    marks = ",".join("?" * len(legacy)) or "NULL"
    has_legacy = conn.execute(
        f"SELECT 1 FROM documents WHERE type = 'law' AND source_url IN ({marks})",
        legacy).fetchone() is not None
    row = conn.execute(
        "SELECT metadata FROM documents WHERE type = 'law' AND source_url = ?"
        " AND is_seed = 0", (url,)).fetchone()
    if row and not has_legacy and \
            json.loads(row[0] or "{}").get("content_hash") == content_hash:
        return SourceResult(name, "unchanged", summary)

    try:
        # Seed placeholders share the official URL, so this removes them too.
        conn.execute(
            f"DELETE FROM documents WHERE type = 'law'"
            f" AND (source_url = ? OR source_url IN ({marks}))", [url, *legacy])
        db.add_document(
            conn, type="law", title=title, source_url=url,
            metadata={"chapter": chapter, "edition": edition,
                      "source": "oregonlegislature.gov",
                      "content_hash": content_hash},
            chunks=chunks)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info("ORS %s: stored %d sections as %d passages", chapter, n, len(chunks))
    return SourceResult(name, "updated", f"{summary} ({len(chunks)} passages)")


def update_laws(conn, http: PoliteSession | None = None) -> list[SourceResult]:
    """Fetch, validate and store every chapter in config.ORS_CHAPTERS. One
    chapter failing never affects the others or the existing stored text."""
    http = http or PoliteSession(delay=2.0, backoff=30.0, wait_budget=300.0)
    results = []
    for chapter, spec in config.ORS_CHAPTERS.items():
        name = f"Laws: ORS {chapter}"
        try:
            resp = http.get(spec["url"])
        except SiteBlocked as exc:
            results.append(SourceResult(name, "blocked", str(exc)))
            continue
        except requests.RequestException as exc:
            results.append(SourceResult(name, "failed", f"download failed: {exc}"))
            continue
        parsed = parse_chapter_html(resp.content, chapter)
        n = len(parsed["sections"])
        missing = parsed["missing_from_body"]
        log.info("ORS %s: %d sections parsed, %d with a catchline (edition %s, "
                 "%d contents lines skipped, %d cross-reference lines kept as "
                 "text)%s", chapter, n, parsed["real_sections"],
                 parsed["edition"], parsed["toc_entries"],
                 parsed["rejected_headings"],
                 f"; first {parsed['sections'][0]['number']}, last "
                 f"{parsed['sections'][-1]['number']}" if n else "")
        if missing:
            log.warning("ORS %s: %d contents entries have no section in the "
                        "body: %s", chapter, len(missing), ", ".join(missing[:20]))
        if parsed["real_sections"] < spec["min_sections"]:
            results.append(SourceResult(
                name, "failed",
                f"only {parsed['real_sections']} sections recognized (expected "
                f"at least {spec['min_sections']}); kept the existing text"))
            continue
        # Contents entries missing from the body mean sections were merged —
        # the page layout has drifted. A few can be legitimate; many cannot.
        if len(missing) > max(3, parsed["toc_entries"] // 20):
            results.append(SourceResult(
                name, "failed",
                f"{len(missing)} sections listed in the chapter's contents were "
                "not found in its text; kept the existing text"))
            continue
        results.append(store_chapter(conn, chapter, spec, parsed))
    return results
