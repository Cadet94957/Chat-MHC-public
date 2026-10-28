"""SQLite storage: content corpus (with FTS5 index), forms, chats."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,              -- article | law | update | form | member
    title TEXT NOT NULL,
    source_url TEXT,
    date TEXT,                        -- ISO date when known
    author TEXT,
    metadata TEXT DEFAULT '{}',
    is_seed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section_id TEXT,                  -- e.g. "ORS 90.600"
    text TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS forms (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    form_number TEXT,
    description TEXT,
    source_url TEXT,
    file_path TEXT                    -- local cached PDF, relative to DATA_DIR
);

-- Small key/value facts about the knowledge base itself, e.g. the newest
-- bookmark export imported so far (see ingest/mhco_import.py).
CREATE TABLE IF NOT EXISTS kb_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL,
    archived INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- content ----------

def add_document(conn, *, type, title, source_url=None, date=None, author=None,
                 metadata=None, is_seed=False, chunks=()):
    cur = conn.execute(
        "INSERT INTO documents (type, title, source_url, date, author, metadata, is_seed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (type, title, source_url, date, author, json.dumps(metadata or {}), int(is_seed)),
    )
    doc_id = cur.lastrowid
    for chunk in chunks:
        if isinstance(chunk, str):
            chunk = {"text": chunk}
        text = chunk.get("text", "").strip()
        if len(text) < 30:
            continue
        conn.execute(
            "INSERT INTO chunks (document_id, section_id, text) VALUES (?, ?, ?)",
            (doc_id, chunk.get("section_id"), text),
        )
    return doc_id


def replace_documents_of_type(conn, doc_type: str) -> None:
    """Remove existing docs of a type before re-ingesting (seed docs always go first)."""
    conn.execute("DELETE FROM documents WHERE type = ?", (doc_type,))


def delete_seed_documents(conn, doc_type: str | None = None) -> None:
    """Remove seed placeholders — all of them, or only those of one type."""
    if doc_type is None:
        conn.execute("DELETE FROM documents WHERE is_seed = 1")
    else:
        conn.execute("DELETE FROM documents WHERE is_seed = 1 AND type = ?",
                     (doc_type,))


def content_stats(conn) -> dict:
    rows = conn.execute(
        "SELECT type, COUNT(*) AS docs FROM documents GROUP BY type"
    ).fetchall()
    chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    forms = conn.execute("SELECT COUNT(*) AS n FROM forms").fetchone()["n"]
    return {
        "documents": {r["type"]: r["docs"] for r in rows},
        "chunks": chunks,
        "forms": forms,
    }


# ---------- chats ----------

def create_chat(conn, title=None) -> dict:
    chat_id = str(uuid.uuid4())
    title = title or f"Chat {datetime.now().strftime('%b %d, %H:%M')}"
    conn.execute(
        "INSERT INTO chats (id, title, created_at) VALUES (?, ?, ?)",
        (chat_id, title, now()),
    )
    return {"id": chat_id, "title": title}


def list_chats(conn) -> list:
    rows = conn.execute(
        "SELECT id, title, created_at FROM chats WHERE archived = 0"
        " ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def chat_exists(conn, chat_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM chats WHERE id = ?", (chat_id,)
    ).fetchone() is not None


def archive_chat(conn, chat_id: str) -> None:
    conn.execute("UPDATE chats SET archived = 1 WHERE id = ?", (chat_id,))


def rename_chat(conn, chat_id: str, title: str) -> None:
    conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))


def get_history(conn, chat_id: str, limit: int | None = None) -> list:
    sql = ("SELECT role, content, sources, created_at FROM messages"
           " WHERE chat_id = ? ORDER BY id")
    rows = conn.execute(sql, (chat_id,)).fetchall()
    history = [
        {"role": r["role"], "content": r["content"],
         "sources": json.loads(r["sources"]), "created_at": r["created_at"]}
        for r in rows
    ]
    return history[-limit:] if limit else history


def add_message(conn, chat_id: str, role: str, content: str, sources=None) -> None:
    conn.execute(
        "INSERT INTO messages (chat_id, role, content, sources, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (chat_id, role, content, json.dumps(sources or []), now()),
    )
