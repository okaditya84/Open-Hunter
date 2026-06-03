"""A polite, resilient HTTP client.

Design principles, in line with the project's honesty goal:
  * We identify ourselves with a real User-Agent (no browser impersonation
    for the sake of evading bot detection).
  * We respect robots.txt when configured to (the default).
  * We rate-limit per host and retry transient failures with backoff.
  * When a host genuinely blocks us, we report it as "blocked" rather than
    pretending we got data.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config
from .logging_util import get_logger

log = get_logger(__name__)


class BlockedError(Exception):
    """Raised when a host clearly blocks automated access (403/429/CAPTCHA)."""


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids the path and we're respecting robots."""


class HttpClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._lock = threading.Lock()
        self._last_request_at: Dict[str, float] = {}
        self._robots_cache: Dict[str, Optional[RobotFileParser]] = {}

    # ------------------------------------------------------------------ #
    # politeness helpers
    # ------------------------------------------------------------------ #
    def _throttle(self, host: str) -> None:
        """Ensure at least request_delay_seconds between hits to one host."""
        with self._lock:
            now = time.monotonic()
            last = self._last_request_at.get(host, 0.0)
            wait = self.config.request_delay_seconds - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at[host] = time.monotonic()

    def _robots_for(self, scheme: str, host: str) -> Optional[RobotFileParser]:
        key = f"{scheme}://{host}"
        if key in self._robots_cache:
            return self._robots_cache[key]

        rp = RobotFileParser()
        robots_url = f"{key}/robots.txt"
        try:
            resp = self.session.get(
                robots_url, timeout=self.config.http_timeout
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                # No robots.txt => everything is allowed.
                rp = None
        except requests.RequestException:
            rp = None  # If robots is unreachable, don't block ourselves.

        self._robots_cache[key] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return True
        rp = self._robots_for(parsed.scheme, parsed.netloc)
        if rp is None:
            return True
        return rp.can_fetch(self.config.user_agent, url)

    # ------------------------------------------------------------------ #
    # core request
    # ------------------------------------------------------------------ #
    @retry(
        retry=retry_if_exception_type(
            (requests.ConnectionError, requests.Timeout)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _raw_request(
        self, method: str, url: str, **kwargs
    ) -> requests.Response:
        timeout = kwargs.pop("timeout", self.config.http_timeout)
        return self.session.request(method, url, timeout=timeout, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        check_robots: bool = True,
        **kwargs,
    ) -> Optional[requests.Response]:
        """Perform a request, honouring politeness and reporting blocks.

        Returns the Response on success, or None on a non-fatal failure.
        Raises BlockedError / RobotsDisallowed for honest reporting upstream.
        """
        parsed = urlparse(url)
        host = parsed.netloc

        if check_robots and not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows: {url}")

        self._throttle(host)

        try:
            resp = self._raw_request(method, url, **kwargs)
        except requests.RequestException as exc:
            log.debug("request failed for %s: %s", url, exc)
            return None

        if resp.status_code in (401, 403, 429):
            raise BlockedError(
                f"host returned {resp.status_code} for {url} "
                f"(access blocked / rate-limited)"
            )

        # Detect obvious anti-bot interstitials so we can report honestly.
        if resp.status_code == 200 and _looks_like_challenge(resp):
            raise BlockedError(f"anti-bot challenge detected at {url}")

        return resp

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Optional[requests.Response]:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs) -> Optional[dict]:
        resp = self.get(url, **kwargs)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def close(self) -> None:
        self.session.close()


_CHALLENGE_MARKERS = (
    "captcha",
    "cf-challenge",
    "cf-browser-verification",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "px-captcha",
    "perimeterx",
)


def _looks_like_challenge(resp: requests.Response) -> bool:
    ctype = resp.headers.get("Content-Type", "")
    if "text/html" not in ctype:
        return False
    # Only inspect a small slice — challenges declare themselves early.
    body = resp.text[:4000].lower()
    return any(marker in body for marker in _CHALLENGE_MARKERS)
