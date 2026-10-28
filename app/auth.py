"""Shared-login auth with a signed session cookie, plus in-memory rate limiting."""
import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import config

COOKIE_NAME = "chatmhc_session"


class RateLimiter:
    """Sliding-window limiter with a per-key limit and an optional global cap.

    The global cap bounds total throughput even when an attacker rotates keys
    (e.g. spoofed X-Forwarded-For addresses). In-memory only — resets on
    restart, which is acceptable for brute-force/cost containment on a
    single-process deployment.
    """

    _GLOBAL = "\x00global"

    def __init__(self, max_events: int, window_seconds: float,
                 global_max: int | None = None):
        self.max_events = max_events
        self.window = window_seconds
        self.global_max = global_max
        self._events: dict[str, deque] = defaultdict(deque)

    def _prune(self, q: deque, now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._events) > 4096:  # bound memory under key churn
            stale = [k for k, q in self._events.items()
                     if not q or now - q[-1] > self.window]
            for k in stale:
                del self._events[k]
        q = self._events[key]
        self._prune(q, now)
        if len(q) >= self.max_events:
            return False
        if self.global_max is not None:
            g = self._events[self._GLOBAL]
            self._prune(g, now)
            if len(g) >= self.global_max:
                return False
            g.append(now)
        q.append(now)
        return True


# Login: 10 attempts / 5 min per address, 100 / 5 min overall.
login_limiter = RateLimiter(10, 300, global_max=100)
# Chat messages (each one triggers an LLM call): 30 / 5 min per address.
message_limiter = RateLimiter(30, 300, global_max=240)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _serializer() -> URLSafeTimedSerializer:
    if not config.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="chatmhc")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def verify_credentials(username: str, password: str) -> bool:
    if not config.CHATBOT_PASSWORD:
        return False  # fail closed when no password is configured
    # Compare fixed-length digests: constant-time regardless of input length
    # or encoding (compare_digest on raw str rejects non-ASCII).
    user_ok = hmac.compare_digest(_digest(username),
                                  _digest(config.CHATBOT_USERNAME))
    pass_ok = hmac.compare_digest(_digest(password),
                                  _digest(config.CHATBOT_PASSWORD))
    return user_ok and pass_ok


def make_session_cookie() -> str:
    return _serializer().dumps({"user": config.CHATBOT_USERNAME})


def require_session(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        data = _serializer().loads(token, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Invalid session")
    return data["user"]
