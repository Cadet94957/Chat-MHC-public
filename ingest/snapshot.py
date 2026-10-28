"""Knowledge-base snapshot: export the live DB + form PDFs into a committable
`kb/` directory, and restore from it on startup.

This is what lets the app run on hosts with an ephemeral filesystem (e.g.
Render's free tier): the scraped content is baked into the deploy instead of
relying on a persistent disk. The published snapshot deliberately excludes
chat history.
"""
import hashlib
import logging
import os
import shutil
import sqlite3

from app import config

log = logging.getLogger("ingest.snapshot")

SNAPSHOT_DB = config.KB_SNAPSHOT_DIR / "chatmhc.db"
SNAPSHOT_FORMS = config.KB_SNAPSHOT_DIR / "forms"

# Content types whose loss would gut the assistant's answers.
GUARDED_TYPES = ("article", "law")
# Refuse to publish if a guarded type falls below this share of the current
# snapshot (a sign the scrape was blocked or broke, not that MHCO deleted it).
MIN_RETAINED_FRACTION = 0.5


class SnapshotShrankError(RuntimeError):
    """The new export has far less content than the published snapshot."""


def _type_counts(db_path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute(
            "SELECT type, COUNT(*) FROM documents GROUP BY type").fetchall())
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def check_not_shrunk(new: dict[str, int], old: dict[str, int]) -> None:
    """Raise SnapshotShrankError if articles or laws vanished or shrank by more
    than half compared with the snapshot currently published."""
    problems = []
    for doc_type in GUARDED_TYPES:
        before, after = old.get(doc_type, 0), new.get(doc_type, 0)
        if before and after < before * MIN_RETAINED_FRACTION:
            problems.append(f"{doc_type}: {before} -> {after}")
    if problems:
        raise SnapshotShrankError(
            "Refusing to overwrite kb/ — content dropped sharply ("
            + "; ".join(problems) + "). The scrape was probably blocked; "
            "the existing snapshot was kept. If the drop is intended, re-run "
            "with --allow-shrink.")



def _digest(db_path) -> str:
    """Hash of the schema and every content row — equal digests mean the same
    published content, whatever SQLite's file-level bookkeeping says."""
    conn = sqlite3.connect(db_path)
    try:
        h = hashlib.sha256()
        for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT"
                                   " NULL ORDER BY type, name"):
            h.update(sql.encode("utf-8"))
        for table, order in (("documents", "id"), ("chunks", "id"),
                             ("forms", "id"), ("kb_meta", "key")):
            try:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
            except sqlite3.OperationalError:  # table not in an older snapshot
                continue
            for row in rows:
                h.update(repr((table, row)).encode("utf-8"))
        return h.hexdigest()
    finally:
        conn.close()


def export_snapshot(allow_shrink: bool = False) -> dict:
    """Copy the live DB (content only, no chats) and form PDFs into kb/.

    Refuses (SnapshotShrankError) when articles or laws dropped sharply
    compared with the snapshot already published, unless allow_shrink is set."""
    if not config.DB_PATH.exists():
        raise FileNotFoundError(
            f"No database at {config.DB_PATH}; run `python -m ingest scrape` first")

    if SNAPSHOT_DB.exists() and not allow_shrink:
        check_not_shrunk(_type_counts(config.DB_PATH), _type_counts(SNAPSHOT_DB))

    config.KB_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_DB.with_name(SNAPSHOT_DB.name + ".tmp")
    if tmp.exists():
        tmp.unlink()

    # VACUUM INTO writes a clean, defragmented single-file copy.
    src = sqlite3.connect(config.DB_PATH)
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()

    # Strip chat history from the published copy.
    snap = sqlite3.connect(tmp)
    try:
        snap.execute("DELETE FROM messages")
        snap.execute("DELETE FROM chats")
        snap.commit()
        snap.execute("VACUUM")
        stats = {
            "documents": snap.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "chunks": snap.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "forms": snap.execute("SELECT COUNT(*) FROM forms").fetchone()[0],
        }
    finally:
        snap.close()

    # Every VACUUM changes a header byte, so a byte-for-byte new file would be
    # committed (and redeployed) on every run. Keep the published file when its
    # content is identical.
    if SNAPSHOT_DB.exists() and _digest(SNAPSHOT_DB) == _digest(tmp):
        tmp.unlink()
        stats["unchanged"] = True
    else:
        os.replace(tmp, SNAPSHOT_DB)
        stats["unchanged"] = False

    # Copy cached form PDFs (forms.file_path values are relative to DATA_DIR).
    if SNAPSHOT_FORMS.exists():
        shutil.rmtree(SNAPSHOT_FORMS)
    src_forms = config.FORMS_DIR
    pdf_count = 0
    if src_forms.exists():
        SNAPSHOT_FORMS.mkdir(parents=True, exist_ok=True)
        for pdf in src_forms.glob("*.pdf"):
            shutil.copy2(pdf, SNAPSHOT_FORMS / pdf.name)
            pdf_count += 1
    stats["pdf_files"] = pdf_count
    log.info("snapshot exported to %s: %s", config.KB_SNAPSHOT_DIR, stats)
    return stats


def has_snapshot() -> bool:
    return SNAPSHOT_DB.exists()


def restore_snapshot() -> bool:
    """Seed the live data dir from kb/ when it's empty (host startup)."""
    if not SNAPSHOT_DB.exists():
        return False
    config.ensure_dirs()
    shutil.copy2(SNAPSHOT_DB, config.DB_PATH)
    if SNAPSHOT_FORMS.exists():
        for pdf in SNAPSHOT_FORMS.glob("*.pdf"):
            shutil.copy2(pdf, config.FORMS_DIR / pdf.name)
    log.info("restored knowledge base from snapshot %s", config.KB_SNAPSHOT_DIR)
    return True
