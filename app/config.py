import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
FORMS_DIR = DATA_DIR / "forms"
DB_PATH = DATA_DIR / "chatmhc.db"
STATIC_DIR = BASE_DIR / "static"
# Committed knowledge-base snapshot (baked into the deploy for hosts with an
# ephemeral filesystem, e.g. Render's free tier). See ingest/snapshot.py.
KB_SNAPSHOT_DIR = BASE_DIR / "kb"
# Manually-curated MHCO form PDFs committed to the repo. The forms library is
# members-only and protected by an anti-bot wall that blocks automated login,
# so forms are managed by dropping PDFs here rather than scraping. Source of
# truth for the Forms tab. See ingest/forms_library.py.
FORMS_LIBRARY_DIR = BASE_DIR / "forms_library"
# Files saved by the "Grab MHCO updates" bookmark button and uploaded to the
# repo. mhco.org blocks GitHub's servers, so MHCO articles and members arrive
# this way; the workflow imports and then removes them. See
# ingest/mhco_import.py and static/mhco-bookmarklet.js.
MHCO_IMPORTS_DIR = BASE_DIR / "mhco_imports"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "claude-sonnet-5-5")

CHATBOT_USERNAME = os.getenv("CHATBOT_USERNAME", "admin")
CHATBOT_PASSWORD = os.getenv("CHATBOT_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days
# Mark the session cookie Secure (HTTPS-only). Leave on in production; set
# COOKIE_SECURE=false only for local development over plain http.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() not in (
    "0", "false", "no", "off")

# Length limits on user-supplied input (DoS / LLM-cost containment).
MAX_MESSAGE_CHARS = 4000
MAX_TITLE_CHARS = 200
MAX_CREDENTIAL_CHARS = 256

MHCO_BASE_URL = os.getenv("MHCO_BASE_URL", "https://mhco.org")
MHCO_USERNAME = os.getenv("MHCO_USERNAME", "")
MHCO_PASSWORD = os.getenv("MHCO_PASSWORD", "")

# Pages scraped by `python -m ingest scrape`
MHCO_LOGIN_PATH = "/user/login"
MHCO_UPDATES_PATH = "/news-and-resources/community-updates"
MHCO_FORMS_PATH = "/forms-oregon-laws/mhco-forms"
MHCO_MEMBERS_PATH = "/membership/associate-members"

# Oregon Revised Statutes, fetched from the Legislature's official site (public,
# no login). `min_sections` is a sanity floor: a parse yielding fewer sections
# than this is treated as a failure and the existing laws are kept.
ORS_CHAPTERS = {
    "90": {"url": "https://www.oregonlegislature.gov/bills_laws/ors/ors090.html",
           "title": "Residential Landlord and Tenant", "min_sections": 120},
    "91": {"url": "https://www.oregonlegislature.gov/bills_laws/ors/ors091.html",
           "title": "Tenancy", "min_sections": 10},
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FORMS_DIR.mkdir(parents=True, exist_ok=True)


# Values shipped in .env.example — refusing to boot with them beats silently
# running with guessable credentials.
_PLACEHOLDER_SECRETS = {"change-me", "change-me-to-a-long-random-string"}


def check_security_config() -> None:
    """Validate secrets at startup: hard-fail on .env.example placeholders,
    warn on weak/missing values. Called from the app lifespan."""
    global SESSION_SECRET
    log = logging.getLogger("chatmhc.config")
    if SESSION_SECRET.strip().lower() in _PLACEHOLDER_SECRETS \
            or CHATBOT_PASSWORD.strip().lower() in _PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "SESSION_SECRET / CHATBOT_PASSWORD still contain the .env.example "
            "placeholder values. Set real secrets (e.g. `openssl rand -base64 48` "
            "for SESSION_SECRET) before starting the app.")
    if not SESSION_SECRET:
        SESSION_SECRET = secrets.token_urlsafe(48)
        log.warning(
            "SESSION_SECRET is not set — generated an ephemeral secret. "
            "Sessions will not survive a restart; set SESSION_SECRET in "
            "production.")
    elif len(SESSION_SECRET) < 32:
        log.warning("SESSION_SECRET is under 32 characters — use a long random "
                    "value in production.")
    if not CHATBOT_PASSWORD:
        log.warning("CHATBOT_PASSWORD is not set — all logins will be rejected "
                    "until it is configured.")
    if not COOKIE_SECURE:
        log.warning("COOKIE_SECURE=false — session cookies may be sent over "
                    "plain HTTP. Use only for local development.")
