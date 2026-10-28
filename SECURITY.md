# Chat MHC — Security & Privacy Overview

**Last reviewed:** July 4, 2026 (full-codebase security audit; all findings below are fixed in this version)

This document describes how Chat MHC protects the data it handles. It is written
both as an internal record of the security review and as an assurance document
that can be shared with customers and members.

---

## 1. What Chat MHC is

Chat MHC is a private, login-protected web assistant for Oregon manufactured-home
community owners and managers. It answers questions grounded in MHCO guidance,
Oregon Revised Statutes (ORS 90/91), and MHCO forms, and serves MHCO form PDFs
for download.

Architecture, deliberately minimal:

| Layer | Technology | Notes |
|---|---|---|
| Web app | FastAPI (Python) — single process | No admin panel, no user-upload endpoints |
| Storage | SQLite — single file on the app host | No external database service |
| AI answers | Anthropic Claude API | The only third-party service the app talks to at runtime |
| Frontend | Dependency-free HTML/CSS/JS | **No CDN scripts, no analytics, no trackers, no third-party requests from the browser** |
| Hosting | Docker container (Render.com or any host) | TLS terminated by the hosting platform |

A small attack surface is the primary security property: there are ~10 API
routes, one process, one database file, and zero third-party code running in
the browser.

## 2. Authentication & sessions

- **Everything requires login.** Every API route — chat, chat history, forms
  list, form PDF downloads, statute browsing, status — rejects unauthenticated
  requests with `401`. Only the login page itself is public.
- **Credential checking is constant-time.** Credentials are compared as
  fixed-length SHA-256 digests via `hmac.compare_digest`, eliminating timing
  and length side-channels and handling any character set safely.
- **Brute-force protection.** Login is rate-limited (10 attempts per address
  per 5 minutes, with a global cap of 100 per 5 minutes that holds even if an
  attacker spoofs client addresses). Excess attempts receive `429` with a
  `Retry-After`.
- **Sessions are signed cookies** (`itsdangerous`, HMAC-signed, 14-day expiry
  enforced server-side) with `HttpOnly` (no JavaScript access), `SameSite=Lax`
  (blocks cross-site request forgery on state-changing requests), and `Secure`
  (HTTPS-only; on by default).
- **Fail-closed configuration.** The app refuses to start if the example
  placeholder secrets are still configured, and rejects all logins when no
  password is set. Weak/missing secrets produce loud startup warnings.

*Design note:* the app uses a single shared login by design (one operator
credential for a small trusted team). There are no per-user accounts, so all
chats are visible to anyone holding the credential. Rotating either the
password or `SESSION_SECRET` immediately invalidates access/sessions.

## 3. Transport security

- All production traffic is HTTPS (TLS terminated by the hosting platform).
- `Strict-Transport-Security` (HSTS, 1 year, includeSubDomains) is sent on all
  responses, so browsers refuse to downgrade to plain HTTP.
- Session cookies are marked `Secure` and are never sent over plain HTTP.

## 4. Application hardening

- **Strict Content-Security-Policy** on every response:
  `default-src 'self'` — no inline scripts, no external script/style/font/image
  sources, no framing (`frame-ancestors 'none'`). Because the frontend has zero
  third-party dependencies, the policy has no exceptions.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` (clickjacking), `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy` (camera, microphone,
  geolocation disabled), and `Cache-Control: no-store` on all API responses so
  chat content is never written to shared/proxy caches.
- **Output encoding (XSS).** Assistant answers are rendered by a small
  markdown renderer that HTML-escapes all content **including quotes** before
  any formatting is applied; markdown links only ever become anchors for
  `http(s)` URLs (so `javascript:`/`data:` schemes are inert), and all links
  open with `rel="noopener noreferrer"`. All other dynamic UI text is inserted
  via `textContent`, never HTML.
- **Injection safety.** Every database query uses bound parameters (no string
  SQL). Full-text search queries are rebuilt from a strict token whitelist
  (`[A-Za-z0-9.]`), so FTS5 operators and quotes in user input cannot reach the
  query engine.
- **Input limits.** Message length (4,000 chars), chat titles (200), and
  credentials (256) are enforced server-side, bounding LLM cost abuse and
  memory use. Chat messages are additionally rate-limited (30 per address per
  5 minutes) to cap AI-API spend from a stolen credential.
- **File download hardening.** Form PDFs are served only from the app's data
  directory — the resolved path is verified to stay inside it (no path
  traversal even if the database were tampered with) — and download filenames
  are restricted to header-safe characters.
- **No information leaks.** Internal error details (stack traces, upstream API
  errors) are logged server-side only; clients receive a generic message.
- **CSRF.** All state-changing endpoints require a JSON body and are protected
  by `SameSite=Lax` cookies; no cross-origin resource sharing (CORS) is enabled.

## 5. Privacy & data handling

**What the app stores** (in its SQLite database, on the app host only):

| Data | Contents | Retention |
|---|---|---|
| Knowledge base | Public statutes + MHCO member content (articles, forms) | Refreshed by the ingest pipeline |
| Chats & messages | Questions typed by users, generated answers, cited sources | Until archived/removed; on Render's free tier the disk is ephemeral, so chat history is wiped on every restart |
| Session cookie | Signed username only — no tokens for other systems, no tracking IDs | 14 days |

**What the app does *not* do:**

- No user accounts, emails, phone numbers, or payment data are collected.
- No analytics, advertising, fingerprinting, or third-party trackers — the
  browser talks only to the app itself.
- No chat data in the codebase or deploy artifacts: the published knowledge-base
  snapshot strips all chats/messages before it is committed (enforced by an
  automated test).

**AI processing.** When a question is asked, the question, recent conversation
turns, and matching knowledge-base excerpts are sent to Anthropic's Claude API
over TLS to generate the answer. Under Anthropic's commercial API terms, API
inputs/outputs are not used to train models. No other third party receives any
user data. See https://www.anthropic.com/legal/commercial-terms and
https://privacy.anthropic.com for Anthropic's data handling and retention.

**Content licensing.** MHCO forms and member content are members-only. The
application keeps them behind the login. They are not included in this public
copy of the source; any repository that holds them must be private.

## 6. Supply chain & operations

- **Dependencies:** eight small, mainstream Python packages, with explicit
  minimum versions for security-patched releases (including `starlette` ≥ 0.40
  and `h11` ≥ 0.16 for known CVE fixes). No JavaScript dependencies at all.
- **Continuous security checks:** CI runs the full test suite — including
  dedicated security regression tests for headers, cookie flags, rate
  limiting, input limits, path traversal, header injection, and FTS injection —
  plus `pip-audit` scanning all installed dependencies against known-CVE
  databases on every push and pull request.
- **Container hardening:** the Docker image runs as an unprivileged user
  (UID 10001), with the data directory as the only writable path.
- **Automation hygiene:** the knowledge-base refresh workflow passes inputs via
  environment variables (no shell interpolation of user-controlled values) and
  scraper credentials live only in GitHub Actions secrets.
- **Secrets:** all secrets (AI API key, login credentials, session secret,
  scraper credentials) are supplied via environment variables; `.env` is
  git-ignored, and this repository's history contains no committed secrets
  (verified during the audit).

## 7. Audit summary — July 2026

A full review of the codebase, configuration, deployment files, git history,
and CI pipeline was performed. Findings and resolutions:

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | High | Markdown renderer allowed attribute injection via quotes in link URLs (XSS in assistant messages) | Quotes now HTML-escaped; link URL grammar tightened |
| 2 | High | Source/form URLs from the knowledge base assigned to links without scheme validation (`javascript:` risk) | Only `http(s)` URLs may become links |
| 3 | High | Session cookie lacked the `Secure` flag; no HSTS | `Secure` on by default; HSTS added |
| 4 | High | No brute-force protection on login; no rate limit on the AI endpoint | Sliding-window rate limits with global caps |
| 5 | High | Non-ASCII login input caused a server error (and a length side-channel existed) | Digest-based constant-time comparison |
| 6 | Medium | No security headers (CSP, frame, sniff, referrer, cache) | Strict headers on every response |
| 7 | Medium | Internal exception text streamed to the browser | Generic client message; details logged server-side |
| 8 | Medium | No input length limits | Server-side limits on all user input |
| 9 | Medium | CI workflow interpolated a user input into a shell command | Input passed via environment variable |
| 10 | Medium | Container ran as root | Unprivileged user |
| 11 | Medium | Placeholder secrets accepted silently | Startup refuses placeholders; warns on weak/missing values |
| 12 | Medium | Form download lacked path containment and filename sanitization | Both enforced |
| 13 | Low | Message posts to nonexistent chats crashed mid-stream | Validated with `404` |
| 14 | Low | Unused `python-multipart` dependency; unpinned patched transitive deps | Removed / floors added; `pip-audit` in CI |
| 15 | Low | Running the test suite overwrote the deployable `kb/` snapshot (data-integrity risk) | Snapshot tests now use an isolated directory |
| 16 | Low | Startup restored an empty knowledge-base snapshot without falling back to seed content, leaving the assistant ungrounded | Seed fallback added when the restored snapshot has no content |

Also verified during the audit: no secrets in git history, no SQL injection
(all queries parameterized), FTS query construction safe, CSRF posture sound,
and the published knowledge-base snapshot contains no chat data. (The audit
covered the private source repository; this public copy was published with
fresh history.)

## 8. Known limitations & operator recommendations

These are properties of the current design that operators should be aware of:

- **Shared login:** one credential for all users means no per-user audit trail
  and no per-user revocation. Rotate `CHATBOT_PASSWORD` when someone leaves the
  team; rotate `SESSION_SECRET` to force all sessions to re-authenticate.
- **Logout is client-side:** logging out deletes the cookie but does not revoke
  it server-side (it remains valid until its 14-day expiry on a device that
  kept a copy). Rotating `SESSION_SECRET` revokes everything immediately.
- **Encryption at rest** relies on the hosting platform's disk encryption; the
  SQLite file itself is not application-encrypted. Prefer hosts with encrypted
  disks (Render's disks are encrypted at rest).
- **Chat retention:** "archiving" a chat hides it but does not delete its
  messages. On ephemeral hosting (Render free tier) history is wiped on
  restart; on persistent hosting, periodically delete old rows if retention
  matters to you.
- **Never commit MHCO content to a public repository** — form PDFs and
  member articles are licensed. Keep any copy that holds them private.
- If credentials from the *predecessor* project (pre-dating this repository)
  were ever committed elsewhere, ensure they have been rotated.

## 9. Reporting a vulnerability

If you believe you have found a security issue, please contact the repository
owner privately (GitHub → repository → *Security* → *Report a vulnerability*,
or the contact address provided with your deployment) rather than opening a
public issue. Reports are triaged promptly and fixes are deployed via the
standard CI pipeline.
