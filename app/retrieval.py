"""BM25 retrieval over the FTS5 chunk index with domain-aware, recency-aware
ranking.

Laws outrank guidance, official updates outrank general articles, stale dated
content decays smoothly, and when two articles/updates cover the same topic the
older one is demoted and flagged as superseded by the newer — so the assistant
reflects the most current guidance. Query terms are expanded with landlord/
tenant domain synonyms so conversational questions hit statutory language.
"""
import json
import re
from datetime import datetime

# Boost applied on top of BM25 score, per content type.
TYPE_WEIGHTS = {"law": 3.0, "update": 2.0, "article": 1.0, "form": 1.5, "member": 0.8}

# Context packing. Statute passages run up to ~2,500 chars, so without limits
# a handful of them would fill the whole budget and leave no MHCO guidance:
# laws get at most this share of the budget while other content is waiting,
# and at most this many passages from any one section.
LAW_SHARE = 0.6
MAX_PASSAGES_PER_SECTION = 2
_SECTION_RE = re.compile(r"ORS \d+\.\d+[a-z]?")

# Half-life (years) for the recency decay on dated, non-law content.
RECENCY_HALF_LIFE_YEARS = 3.0
RECENCY_FLOOR = 0.5
# Same-topic supersession: older chunk score multiplier + grouping thresholds.
SUPERSEDE_PENALTY = 0.5
SUPERSEDE_MIN_SHARED = 4
SUPERSEDE_MIN_JACCARD = 0.5

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "are",
    "was", "will", "have", "has", "not", "any", "all", "may", "can", "must",
    "shall", "their", "they", "when", "what", "which", "who", "how", "into",
    "under", "over", "out", "about", "than", "then", "them", "these", "those",
    "such", "also", "but", "per", "within", "upon", "each", "other", "more",
    "most", "some", "only", "after", "before", "during", "mhco", "form",
    # Question words that otherwise match nearly every chunk.
    "new", "implement", "does", "should", "need", "my", "our", "do", "is",
    "it", "an", "of", "to", "in", "on", "or", "if", "be", "we", "me",
}

# Conversational term -> statutory/MHCO vocabulary.
SYNONYMS = {
    "evict": ["termination", "terminate", "eviction"],
    "eviction": ["termination", "terminate"],
    "kick": ["termination"],
    "raise": ["increase"],
    "rent": ["rent", "payment"],
    "late": ["nonpayment", "late"],
    "deposit": ["security deposit"],
    "sell": ["sale", "transfer"],
    "selling": ["sale", "transfer"],
    "screening": ["applicant", "application"],
    "abandon": ["abandonment", "abandoned"],
    "pet": ["pets", "animal", "animals", "assistance"],
    "pets": ["pet", "animal", "animals", "assistance"],
    "policy": ["rule", "rules"],
    "tree": ["hazard tree"],
    "rules": ["rule", "violation"],
}

_token_re = re.compile(r"[A-Za-z0-9][A-Za-z0-9.]*")


def _fts_query(query: str) -> str:
    """Build a safe OR query for FTS5 from free text, with synonym expansion.
    Stopwords are dropped unless the question is made only of them."""
    tokens = [t for t in _token_re.findall(query.lower()) if len(t) > 1]
    tokens = [t for t in tokens if t not in _STOPWORDS] or tokens
    terms: list[str] = []
    seen = set()
    for tok in tokens:
        candidates = [tok] + SYNONYMS.get(tok, [])
        for term in candidates:
            for word in term.split():
                if word not in seen and len(word) > 1:
                    seen.add(word)
                    terms.append(f'"{word}"')
    return " OR ".join(terms)


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str[:10])
    except ValueError:
        return None


def _recency_factor(doc_type: str, date_str: str | None) -> float:
    """Smooth recency multiplier: ~1.0 for new dated content, decaying by a
    half-life toward RECENCY_FLOOR. Laws and undated content are unaffected so a
    newer article consistently edges out a near-equal older one without dropping
    older content entirely."""
    if doc_type == "law":
        return 1.0
    dt = _parse_date(date_str)
    if dt is None:
        return 1.0
    age_years = max((datetime.now() - dt).days / 365.0, 0.0)
    decay = 0.5 ** (age_years / RECENCY_HALF_LIFE_YEARS)
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * decay


def _significant_terms(text: str) -> set[str]:
    return {w for w in _token_re.findall(text.lower())
            if len(w) > 3 and w not in _STOPWORDS}


def _apply_supersession(chunks: list[dict]) -> None:
    """Among dated article/update chunks, find same-topic pairs and demote the
    older one, tagging it `superseded_by` the newer. Mutates chunks in place,
    adding `is_current` (bool) and `superseded_by` (dict|None). Conservative:
    only demotes on strong term overlap, never drops, never touches laws."""
    for c in chunks:
        c.setdefault("is_current", True)
        c.setdefault("superseded_by", None)

    candidates = [
        c for c in chunks
        if c["type"] in ("article", "update") and _parse_date(c.get("date"))
    ]
    terms = {id(c): _significant_terms(c["text"]) for c in candidates}
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            ta, tb = terms[id(a)], terms[id(b)]
            if not ta or not tb:
                continue
            shared = ta & tb
            union = ta | tb
            if len(shared) < SUPERSEDE_MIN_SHARED:
                continue
            if len(shared) / len(union) < SUPERSEDE_MIN_JACCARD:
                continue
            older, newer = sorted(
                (a, b), key=lambda c: _parse_date(c["date"]))
            if _parse_date(older["date"]) == _parse_date(newer["date"]):
                continue
            older["is_current"] = False
            older["score"] = round(older["score"] * SUPERSEDE_PENALTY, 4)
            older["superseded_by"] = {
                "title": newer["title"], "date": newer["date"]}


def _section_key(chunk: dict) -> str:
    m = _SECTION_RE.match(chunk.get("section_id") or "")
    return m.group(0) if m else chunk["title"]


def _pack(scored: list[dict], k: int, max_chars: int, law_cap: float) -> list[dict]:
    """Take the best chunks within `k` slots and `max_chars`, capping law text
    at `law_cap` chars and passages per section; laws held back by the cap
    fill whatever room the other content leaves."""
    out, total, law_total = [], 0, 0
    per_section: dict[str, int] = {}
    held: list[dict] = []
    for chunk in scored:
        if len(out) >= k:
            break
        size = len(chunk["text"])
        if total + size > max_chars:
            continue
        if chunk["type"] == "law":
            key = _section_key(chunk)
            if per_section.get(key, 0) >= MAX_PASSAGES_PER_SECTION:
                continue
            if law_total + size > law_cap:
                held.append(chunk)
                continue
            per_section[key] = per_section.get(key, 0) + 1
            law_total += size
        out.append(chunk)
        total += size
    for chunk in held:
        if len(out) >= k:
            break
        size = len(chunk["text"])
        key = _section_key(chunk)
        if total + size > max_chars or \
                per_section.get(key, 0) >= MAX_PASSAGES_PER_SECTION:
            continue
        per_section[key] = per_section.get(key, 0) + 1
        out.append(chunk)
        total += size
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def search(conn, query: str, k: int = 8, max_chars: int = 12000,
           law_share: float = LAW_SHARE, doc_types: tuple[str, ...] = ()) -> list[dict]:
    """Return top chunks: [{text, section_id, title, type, date, url, score}].

    `law_share` caps the share of `max_chars` that statute text may take while
    other matching content is available (1.0 = no cap). `doc_types` limits the
    search to those document types."""
    fts = _fts_query(query)
    if not fts:
        return []
    type_filter = ""
    if doc_types:
        type_filter = f"AND d.type IN ({','.join('?' * len(doc_types))})"
    rows = conn.execute(
        f"""
        SELECT c.text, c.section_id, d.title, d.type, d.date, d.source_url,
               d.metadata, bm25(chunks_fts) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN documents d ON d.id = c.document_id
        WHERE chunks_fts MATCH ? {type_filter}
        ORDER BY rank
        LIMIT ?
        """,
        (fts, *doc_types, k * 5),
    ).fetchall()

    scored = []
    # bm25() is negative, lower = better. Flip it and scale by the best match
    # so relevance (0, 1] actually varies across results.
    best = max((-r["rank"] for r in rows), default=0.0)
    for r in rows:
        base = (-r["rank"] / best) if best > 0 else 1.0
        score = (base
                 * TYPE_WEIGHTS.get(r["type"], 1.0)
                 * _recency_factor(r["type"], r["date"]))
        scored.append({
            "text": r["text"],
            "section_id": r["section_id"],
            "title": r["title"],
            "type": r["type"],
            "date": r["date"],
            "url": r["source_url"],
            "metadata": json.loads(r["metadata"] or "{}"),
            "score": round(score, 4),
        })

    # Demote/flag older chunks superseded by newer same-topic ones, then sort.
    _apply_supersession(scored)
    scored.sort(key=lambda c: c["score"], reverse=True)

    has_other = any(c["type"] != "law" for c in scored)
    law_cap = max_chars * law_share if has_other else max_chars
    return _pack(scored, k, max_chars, law_cap)


def matching_forms(conn, query: str, limit: int = 3) -> list[dict]:
    """Forms whose title/description overlap the query terms."""
    words = {w for w in _token_re.findall(query.lower()) if len(w) > 3}
    for w in list(words):
        for syn in SYNONYMS.get(w, []):
            words.update(syn.split())
    if not words:
        return []
    rows = conn.execute(
        "SELECT id, title, form_number, description, source_url, file_path FROM forms"
    ).fetchall()
    scored = []
    for r in rows:
        haystack = f"{r['title']} {r['description'] or ''}".lower()
        hits = sum(1 for w in words if w in haystack)
        if hits:
            scored.append((hits, dict(r)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [f for _, f in scored[:limit]]
