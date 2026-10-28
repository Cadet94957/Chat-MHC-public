"""Polite HTTP for the knowledge-base updaters.

mhco.org rate-limits aggressive clients (HTTP 429), and firing the next
request immediately after a 429 is what kept the weekly refresh blocked for
months. PoliteSession paces requests, honors Retry-After, and once a site keeps
refusing us it trips a circuit breaker: every further request in the run raises
SiteBlocked without touching the network.
"""
import email.utils
import logging
import random
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("ingest.polite")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

# The site is telling us to back off (or its gateway is overloaded): wait and
# retry, and if it persists stop talking to the site for the rest of the run.
SITE_LEVEL_STATUSES = {429, 502, 503, 504}
# Pause before the single retry of a page that answered with another 5xx.
PAGE_ERROR_RETRY_WAIT = 10.0


class SiteBlocked(Exception):
    """The site keeps refusing requests (rate limit or unreachable).

    Deliberately NOT a requests.RequestException, so per-page
    `except requests.RequestException` handlers can't swallow it and move on
    to the next URL — that pattern is what hammered the rate limiter before."""


def retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date)."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class PoliteSession:
    """A requests.Session wrapper with pacing, backoff and a circuit breaker.

    delay        mean seconds between consecutive requests (jittered)
    retries      extra attempts after a 429/5xx/connection failure
    backoff      first backoff wait when the server gives no Retry-After;
                 doubles on each retry
    max_wait     a single requested wait longer than this means "blocked"
    wait_budget  total seconds this session may spend backing off per run
    """

    def __init__(self, *, delay=5.0, retries=3, backoff=60.0, max_wait=300.0,
                 wait_budget=600.0, timeout=30, headers=None, sleep=time.sleep):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
        })
        if headers:
            self.session.headers.update(headers)
        self.delay = delay
        self.retries = retries
        self.backoff = backoff
        self.max_wait = max_wait
        self.wait_budget = wait_budget
        self.timeout = timeout
        self.waited = 0.0
        self.blocked = False
        self._sleep = sleep
        self._last: float | None = None

    def _pace(self) -> None:
        if self._last is None or self.delay <= 0:
            return
        target = random.uniform(self.delay * 0.75, self.delay * 1.5)
        elapsed = time.monotonic() - self._last
        if elapsed < target:
            self._sleep(target - elapsed)

    def _block(self, message: str) -> SiteBlocked:
        self.blocked = True
        log.error("%s — no more requests to this site this run", message)
        return SiteBlocked(message)

    def _back_off(self, url: str, attempt: int, reason: str,
                  retry_after: float | None = None) -> None:
        wait = retry_after if retry_after is not None \
            else self.backoff * (2 ** attempt)
        if wait > self.max_wait or self.waited + wait > self.wait_budget:
            raise self._block(
                f"{url}: {reason}; the site asked us to wait {wait:.0f}s "
                f"(already waited {self.waited:.0f}s this run)")
        log.warning("%s: %s; waiting %.0fs before retrying", url, reason, wait)
        self._sleep(wait)
        self.waited += wait

    def request(self, method: str, url: str, *, raise_for_status=True,
                **kwargs) -> requests.Response:
        if self.blocked:
            raise SiteBlocked(f"{url}: skipped — the site already refused "
                              "requests this run")
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(self.retries + 1):
            self._pace()
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._last = time.monotonic()
                if attempt == self.retries:
                    raise self._block(
                        f"{url}: unreachable ({type(exc).__name__})") from exc
                self._back_off(url, attempt, type(exc).__name__)
                continue
            self._last = time.monotonic()

            status = resp.status_code
            if status in SITE_LEVEL_STATUSES:
                if attempt == self.retries:
                    raise self._block(f"{url}: HTTP {status} after "
                                      f"{self.retries} retries")
                self._back_off(url, attempt, f"HTTP {status}",
                               retry_after_seconds(resp))
                continue
            if status >= 500 and attempt == 0 < self.retries:
                # A broken page, not necessarily a blocked site: retry once
                # after a short pause that doesn't spend the rate-limit budget
                # (so a few broken pages can't trip the circuit breaker), and if
                # it persists fall through to a page-level HTTPError below.
                log.warning("%s: HTTP %s; retrying once", url, status)
                self._sleep(PAGE_ERROR_RETRY_WAIT)
                continue
            if raise_for_status:
                resp.raise_for_status()
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)
