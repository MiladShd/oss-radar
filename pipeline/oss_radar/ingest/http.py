"""A polite, resilient HTTP client shared by all connectors.

Design notes distilled from live API verification:
* Every source is best-effort fair-use with little/no documented rate limit, so we
  enforce a small per-host minimum interval and retry 429/5xx with exponential backoff.
* A 404 is a *normal* signal (unknown package / unresolved dependency graph), so the
  helpers return ``None`` on 404 rather than raising — callers degrade gracefully.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import requests
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)

USER_AGENT = "oss-radar/0.1 (+https://github.com/MiladShd/oss-radar)"

# Per-host courtesy floor between requests (seconds). pypistats is a tiny single service.
_HOST_MIN_INTERVAL = {
    "pypistats.org": 1.0,
    "api.github.com": 1.2,  # search endpoint has a strict secondary rate limit
    "packages.ecosyste.ms": 0.2,
    "repos.ecosyste.ms": 0.2,
}
_DEFAULT_MIN_INTERVAL = 0.1


class RateLimited(Exception):
    """Raised on HTTP 429/5xx so tenacity retries with backoff."""


class HttpClient:
    def __init__(self, timeout: int = 30, extra_headers: dict[str, str] | None = None):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if extra_headers:
            self._session.headers.update(extra_headers)
        self._last_call: dict[str, float] = defaultdict(float)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        floor = _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL)
        # per-host lock so concurrent workers genuinely respect the floor (no bursts)
        with self._locks[host]:
            wait = floor - (time.monotonic() - self._last_call[host])
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    @retry(
        retry=retry_if_exception_type((RateLimited, requests.exceptions.ConnectionError,
                                       requests.exceptions.Timeout)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response | None:
        self._throttle(url)
        kwargs.setdefault("timeout", self.timeout)
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code == 404:
            return None
        # 403 from GitHub search = secondary rate limit; back off and retry.
        if resp.status_code in (429, 403) or resp.status_code >= 500:
            raise RateLimited(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        return resp

    def get_json(self, url: str, params: dict | None = None,
                 headers: dict | None = None) -> Any | None:
        try:
            resp = self._request("GET", url, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001 — connectors decide how to degrade
            log.warning("http.get_failed", url=url, error=str(exc))
            return None
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("http.bad_json", url=url, status=resp.status_code)
            return None

    def post_json(self, url: str, json_body: dict, headers: dict | None = None) -> Any | None:
        try:
            resp = self._request("POST", url, json=json_body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("http.post_failed", url=url, error=str(exc))
            return None
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def get_text(self, url: str, headers: dict | None = None) -> str | None:
        """Plain-text GET (e.g. a raw requirements.txt). Returns None on any failure."""
        try:
            resp = self._request("GET", url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("http.get_failed", url=url, error=str(exc))
            return None
        return resp.text if resp is not None else None

    def get_status(self, url: str, params: dict | None = None) -> int | None:
        """Like get_json but returns the HTTP status (used where 202-vs-200 matters)."""
        try:
            self._throttle(url)
            resp = self._session.get(url, params=params, timeout=self.timeout)
            return resp.status_code
        except Exception:  # noqa: BLE001
            return None
