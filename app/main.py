import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from app import auth, config, db, llm, retrieval

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chatmhc")

@asynccontextmanager
async def lifespan(_: FastAPI):
    config.check_security_config()
    db.init_db()
    with db.connect() as conn:
        stats = db.content_stats(conn)
    if not stats["chunks"]:
        from ingest import snapshot
        if snapshot.restore_snapshot():
            db.init_db()  # ensure empty chats/messages tables exist
            log.info("Restored knowledge base from committed snapshot (kb/).")
        # A snapshot exported after a failed scrape can be empty of content;
        # fall back to seed data so the assistant still has grounding.
        with db.connect() as conn:
            if not db.content_stats(conn)["chunks"]:
                from ingest.seed import load_seed
                load_seed(conn)
                log.info("Knowledge base empty — loaded seed content. "
                         "Run the GitHub 'Update knowledge base' workflow to "
                         "load real MHCO data.")
    # The committed forms library (if present) is the source of truth for forms.
    from ingest.forms_library import import_forms_library
    with db.connect() as conn:
        n = import_forms_library(conn)
    if n:
        log.info("Loaded %d MHCO forms from forms_library/.", n)
    yield


app = FastAPI(title="Chat MHC", lifespan=lifespan)

# The frontend is dependency-free (no CDN scripts/styles), so the CSP can be
# strict: same-origin everything, no inline script, no framing.
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'")


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware (safe with SSE streaming) adding security headers
    to every response, plus no-store caching for API responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy",
                                   "strict-origin-when-cross-origin")
                headers.setdefault("Content-Security-Policy", _CSP)
                headers.setdefault("Permissions-Policy",
                                   "camera=(), microphone=(), geolocation=()")
                if config.COOKIE_SECURE:
                    headers.setdefault("Strict-Transport-Security",
                                       "max-age=31536000; includeSubDomains")
                if path.startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


class LoginBody(BaseModel):
    username: str = Field(max_length=config.MAX_CREDENTIAL_CHARS)
    password: str = Field(max_length=config.MAX_CREDENTIAL_CHARS)


class MessageBody(BaseModel):
    input: str = Field(max_length=config.MAX_MESSAGE_CHARS)


class ChatBody(BaseModel):
    title: str | None = Field(default=None, max_length=config.MAX_TITLE_CHARS)


# ---------- auth ----------

@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    if not auth.login_limiter.allow(auth.client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts — try again in a few minutes.",
            headers={"Retry-After": "300"},
        )
    if not auth.verify_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_session_cookie(),
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=config.COOKIE_SECURE,
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
def me(user: str = Depends(auth.require_session)):
    return {"user": user, "llm_configured": bool(config.ANTHROPIC_API_KEY),
            "model": config.MODEL_NAME}


# ---------- chats ----------

@app.get("/api/chats")
def get_chats(user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        return {"chats": db.list_chats(conn)}


@app.post("/api/chats")
def new_chat(body: ChatBody, user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        return db.create_chat(conn, body.title)


@app.get("/api/chats/{chat_id}/history")
def chat_history(chat_id: str, user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        return {"history": db.get_history(conn, chat_id)}


@app.post("/api/chats/{chat_id}/archive")
def archive(chat_id: str, user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        db.archive_chat(conn, chat_id)
    return {"ok": True}


@app.post("/api/chats/{chat_id}/messages")
def send_message(chat_id: str, body: MessageBody, request: Request,
                 user: str = Depends(auth.require_session)):
    question = body.input.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Empty message")
    if not auth.message_limiter.allow(auth.client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many messages — please wait a few minutes.",
            headers={"Retry-After": "300"},
        )

    with db.connect() as conn:
        if not db.chat_exists(conn, chat_id):
            raise HTTPException(status_code=404, detail="Chat not found")
        history = db.get_history(conn, chat_id, limit=8)
        chunks = retrieval.search(conn, question)
        forms = retrieval.matching_forms(conn, question)
        # First user message becomes the chat title.
        if not history:
            db.rename_chat(conn, chat_id, question[:60])

    sources = [
        {"label": c["section_id"] or c["title"], "type": c["type"],
         "title": c["title"], "date": c["date"], "url": c["url"],
         "is_current": c.get("is_current", True),
         "superseded_by": c.get("superseded_by")}
        for c in chunks
    ] + [
        {"label": f["title"], "type": "form", "title": f["title"],
         "form_id": f["id"], "downloadable": bool(f["file_path"]),
         "url": f["source_url"]}
        for f in forms
    ]

    def event_stream():
        yield f"event: sources\ndata: {json.dumps(sources)}\n\n"
        answer_parts = []
        try:
            for delta in llm.stream_answer(question, history, chunks, forms):
                answer_parts.append(delta)
                yield f"event: delta\ndata: {json.dumps(delta)}\n\n"
        except Exception:  # surface LLM/network failures to the UI
            # Log the details server-side; never stream internals to the client.
            log.exception("answer generation failed")
            err = ("\n\n_Sorry — the answer could not be generated. "
                   "Please try again in a moment._")
            answer_parts.append(err)
            yield f"event: delta\ndata: {json.dumps(err)}\n\n"
        answer = "".join(answer_parts)
        with db.connect() as conn:
            db.add_message(conn, chat_id, "user", question)
            db.add_message(conn, chat_id, "assistant", answer, sources)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- forms ----------

@app.get("/api/forms")
def get_forms(user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, form_number, description, source_url,"
            " file_path IS NOT NULL AS downloadable FROM forms ORDER BY title"
        ).fetchall()
    return {"forms": [dict(r) for r in rows]}


@app.get("/api/forms/{form_id}/download")
def download_form(form_id: int, user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM forms WHERE id = ?", (form_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Form not found")
    if not row["file_path"]:
        raise HTTPException(
            status_code=409,
            detail={"message": "PDF not cached yet — run the ingest pipeline,"
                               " or download from MHCO directly.",
                    "url": row["source_url"]},
        )
    base = config.DATA_DIR.resolve()
    path = (base / row["file_path"]).resolve()
    if not path.is_relative_to(base):  # containment: file_path must stay in DATA_DIR
        raise HTTPException(status_code=404, detail="Form not found")
    if not path.exists():
        raise HTTPException(status_code=410, detail="Cached PDF missing on disk")
    # Keep the download filename to header-safe characters.
    safe_title = re.sub(r"[^\w .,()&-]", "-", row["title"]).strip() or "form"
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{safe_title}.pdf")


# ---------- content browsing ----------

@app.get("/api/laws")
def get_laws(q: str = "", user: str = Depends(auth.require_session)):
    q = q[:config.MAX_TITLE_CHARS]  # cap free-text search input
    with db.connect() as conn:
        if q:
            laws = retrieval.search(conn, q, k=20, max_chars=60000,
                                    law_share=1.0, doc_types=("law",))
            return {"laws": laws}
        rows = conn.execute(
            """SELECT c.section_id, c.text, d.title, d.source_url AS url
               FROM chunks c JOIN documents d ON d.id = c.document_id
               WHERE d.type = 'law' ORDER BY d.title, c.id LIMIT 2000"""
        ).fetchall()
        return {"laws": [dict(r) for r in rows]}


@app.get("/api/status")
def status(user: str = Depends(auth.require_session)):
    with db.connect() as conn:
        stats = db.content_stats(conn)
        seed = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE is_seed = 1"
        ).fetchone()["n"]
    return {**stats, "seed_documents": seed,
            "llm_configured": bool(config.ANTHROPIC_API_KEY)}


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
