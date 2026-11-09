from __future__ import annotations

import logging
import time
from typing import Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = "akcije-agent/0.1 (licni projekat, 2x sedmicno)"


class HttpClient(Protocol):
    def get_text(self, url: str) -> str: ...


class PoliteClient:
    """GET-only client with retries and a fixed pause between requests, to keep load on store sites minimal."""

    def __init__(self, delay_seconds: float = 1.5, timeout_seconds: float = 20.0) -> None:
        self._delay = delay_seconds
        self._timeout = timeout_seconds
        self._last_request_at: float | None = None
        retry = Retry(
            total=3,
            backoff_factor=2.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "sr-Latn-ME,sr;q=0.9"})
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    def get_text(self, url: str) -> str:
        if self._last_request_at is not None:
            wait = self._delay - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        log.debug("GET %s", url)
        try:
            response = self._session.get(url, timeout=self._timeout)
        finally:
            self._last_request_at = time.monotonic()
        response.raise_for_status()
        return response.text
