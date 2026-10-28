"""Claude-backed answer generation grounded in retrieved MHCO content."""
from typing import Generator

import anthropic

from app import config

SYSTEM_PROMPT = """You are the Chat MHC assistant, an expert on Oregon \
manufactured-home / mobile home park management. You serve park owners, \
operators, and community managers, answering from MHCO (Manufactured Housing \
Communities of Oregon) guidance, Oregon Revised Statutes (primarily ORS \
chapters 90 and 91), MHCO forms, and community updates.

How to answer:
- Lead with the answer. Open with one or two plain sentences that directly \
answer the question, then give practical numbered steps the reader can act on.
- Base every statement on the MHCO reference material supplied with the \
question. Never fill gaps from general knowledge; when the material doesn't \
cover part of the question, say so in one short line at the end (e.g. "MHCO's \
materials here don't spell out X — confirm with MHCO.") rather than a \
separate section. If the material doesn't address the question at all, say \
that briefly and suggest contacting MHCO.
- Refer to sources by name — the ORS section, the MHCO article, or the form. \
Never mention "the context", "retrieved" or "provided" material; the reader \
doesn't see it and just wants the answer.
- Cite statutes by section (e.g., ORS 90.600) and name the specific MHCO form \
(with its number) whenever one applies to the task.
- Include concrete timeframes, notice periods, and dollar amounts only when \
they appear in the material.
- Keep it short: short paragraphs and numbered steps, no long preamble or \
recap.
- Currency: sources may span several years and each is labeled with its year \
and a [CURRENT] or [OLDER — may be superseded] marker. When two sources give \
conflicting specifics (numbers, timeframes, dollar amounts, procedures), follow \
the most recent source and the current ORS statute over older article \
commentary. When current guidance differs from older guidance, answer with \
the current rule and briefly note that it changed — e.g. "As of 2024, the cap \
is X (this changed from earlier guidance)." Never present a value you can see \
is marked superseded as if it were current.
- You provide general regulatory information, not legal advice. Add a \
one-line note recommending legal counsel only when the question involves an \
active dispute, eviction, or fair-housing risk.
- If a relevant MHCO form appears in the material, end with a "Relevant \
forms:" line listing it."""


def _format_context(chunks: list[dict], forms: list[dict]) -> str:
    parts = []
    laws = [c for c in chunks if c["type"] == "law"]
    guidance = [c for c in chunks if c["type"] in ("article", "update")]
    if laws:
        parts.append("LEGAL REQUIREMENTS:\n" + "\n\n".join(
            f"[{c['section_id'] or c['title']}] {c['text']}" for c in laws))
    if guidance:
        # Newest-first so the model sees current guidance before older.
        guidance = sorted(
            guidance, key=lambda c: (c.get("date") or ""), reverse=True)
        lines = []
        for c in guidance:
            marker = "[OLDER — may be superseded]" if c.get("superseded_by") \
                or c.get("is_current") is False else "[CURRENT]"
            dated = f" — {c['date']}" if c.get("date") else ""
            lines.append(f"{marker} [{c['title']}{dated}] {c['text']}")
        parts.append("MHCO GUIDANCE (newest first):\n" + "\n\n".join(lines))
    others = [c for c in chunks if c["type"] not in ("law", "article", "update")]
    if others:
        parts.append("OTHER:\n" + "\n\n".join(
            f"[{c['title']}] {c['text']}" for c in others))
    if forms:
        parts.append("AVAILABLE MHCO FORMS:\n" + "\n".join(
            f"- {f['title']}" + (f": {f['description']}" if f.get("description") else "")
            for f in forms))
    return "\n\n".join(parts) if parts else "(no matching MHCO material found)"


def build_user_message(question: str, chunks: list[dict], forms: list[dict]) -> str:
    return (
        "MHCO REFERENCE MATERIAL:\n"
        f"{_format_context(chunks, forms)}\n\n"
        f"QUESTION: {question}"
    )


def _fallback_answer(chunks: list[dict], forms: list[dict]) -> str:
    """Used when no ANTHROPIC_API_KEY is configured: return raw retrieval results."""
    lines = ["**AI answers are not configured yet** (no `ANTHROPIC_API_KEY` set). "
             "Here is what the knowledge base found for your question:\n"]
    if not chunks and not forms:
        lines.append("_No matching content. Run the ingest pipeline to load MHCO data._")
    for c in chunks[:5]:
        label = c["section_id"] or c["title"]
        lines.append(f"- **{label}** ({c['type']}): {c['text'][:300].strip()}…")
    if forms:
        lines.append("\n**Relevant forms:** " + ", ".join(f["title"] for f in forms))
    return "\n".join(lines)


def stream_answer(question: str, history: list[dict], chunks: list[dict],
                  forms: list[dict]) -> Generator[str, None, None]:
    """Yield answer text deltas. History is prior turns as [{role, content}]."""
    if not config.ANTHROPIC_API_KEY:
        yield _fallback_answer(chunks, forms)
        return

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-8:] if m["role"] in ("user", "assistant") and m["content"]
    ]
    messages.append({"role": "user",
                     "content": build_user_message(question, chunks, forms)})

    with client.messages.stream(
        model=config.MODEL_NAME,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text
        final = stream.get_final_message()

    # A declined or truncated answer would otherwise end silently mid-reply.
    if final.stop_reason == "refusal":
        yield ("\n\n_The assistant couldn't answer this question as asked. "
               "Try rephrasing it, or contact MHCO directly._")
    elif final.stop_reason == "max_tokens":
        yield "\n\n_This answer was cut off because it ran too long. " \
              "Try asking a narrower question._"
