"""Imports the JSON files produced by the previous ChatMHC scraper
(F:/ChatMHC_Production_Oregon/output/*.json) into the new knowledge base.

Recognized files (matched by name or by shape):
  mhco_articles.json / all_articles_*.json   {"articles": [...]}
  mhco_laws.json                             {"items": [{title, url, sections:[...]}]}
  mhco_forms.json                            {"items": [{title, description, url}]}
  mhco_updates.json                          {"items": [{title, date, content}]}
  mhco_members.json                          {"items": [{name, services, contact}]}
"""
import json
import logging
import re
from pathlib import Path

from app import db

log = logging.getLogger("ingest.importer")


def import_path(conn, path: Path) -> dict:
    counts: dict[str, int] = {}
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("skipping %s: %s", file.name, exc)
            continue
        kind, n = _import_file(conn, file.name, data)
        if kind:
            counts[kind] = counts.get(kind, 0) + n
            log.info("%s: imported %d %s items", file.name, n, kind)
    conn.commit()
    return counts


def _import_file(conn, name: str, data: dict) -> tuple[str | None, int]:
    name = name.lower()
    if "articles" in data or "articles" in name:
        return "article", _import_articles(conn, data.get("articles", []))
    items = data.get("items", [])
    if "law" in name or (items and "sections" in items[0]):
        return "law", _import_laws(conn, items)
    if "form" in name:
        return "form", _import_forms(conn, items)
    if "update" in name:
        return "update", _import_updates(conn, items)
    if "member" in name or (items and "services" in items[0]):
        return "member", _import_members(conn, items)
    return None, 0


def _import_articles(conn, articles: list) -> int:
    if not articles:
        return 0
    db.delete_seed_documents(conn)
    db.replace_documents_of_type(conn, "article")
    for a in articles:
        content = a.get("content", "")
        chunks = [{"text": p} for p in content.split("\n\n")] or [{"text": content}]
        if a.get("teaser"):
            chunks.insert(0, {"text": a["teaser"]})
        db.add_document(conn, type="article", title=a.get("title", "Untitled"),
                        source_url=a.get("url"), date=a.get("date"),
                        author=a.get("author"), chunks=chunks)
    return len(articles)


def _import_laws(conn, items: list) -> int:
    if not items:
        return 0
    db.replace_documents_of_type(conn, "law")
    n = 0
    for law in items:
        sections = [
            {"section_id": s.get("section_id"), "text": s.get("text", "")}
            for s in law.get("sections", [])
        ]
        db.add_document(conn, type="law", title=law.get("title", "Oregon law"),
                        source_url=law.get("url"), chunks=sections)
        n += len(sections)
    return n


def _import_forms(conn, items: list) -> int:
    if not items:
        return 0
    conn.execute("DELETE FROM forms")
    for f in items:
        title = f.get("title", "Untitled form")
        m = re.search(r"form\s*(\d+[A-Za-z]?)", title, re.I)
        conn.execute(
            "INSERT INTO forms (title, form_number, description, source_url)"
            " VALUES (?, ?, ?, ?)",
            (title, m.group(1).upper() if m else None,
             f.get("description"), f.get("url")),
        )
    return len(items)


def _import_updates(conn, items: list) -> int:
    if not items:
        return 0
    db.replace_documents_of_type(conn, "update")
    for u in items:
        content = u.get("content", "")
        db.add_document(conn, type="update", title=u.get("title", "Update"),
                        source_url=u.get("url"), date=u.get("date"),
                        chunks=[{"text": p} for p in content.split("\n\n")])
    return len(items)


def _import_members(conn, items: list) -> int:
    if not items:
        return 0
    db.replace_documents_of_type(conn, "member")
    for m in items:
        contact = m.get("contact", {})
        bits = [m.get("name", "Member")]
        if m.get("services"):
            bits.append("Services: " + ", ".join(m["services"]))
        for key in ("phone", "email", "address"):
            if contact.get(key):
                bits.append(f"{key.title()}: {contact[key]}")
        db.add_document(conn, type="member", title=m.get("name", "Member"),
                        chunks=[{"text": ". ".join(bits)}])
    return len(items)
