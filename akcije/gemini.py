"""Minimal Gemini REST client (free tier) that returns structured JSON.

Used for two things only: reading Idea catalog text (step 4) and deciding unclear
men's/not-men's products (step 3). Every answer is validated by the caller.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import requests

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# "-latest" alias always points to the current model, so the code does not break when Google retires a version.
# Flash-Lite: tested 8.11.2026 on 3 Idea pages it took 4 s vs 236 s for "gemini-flash-latest" (a thinking model)
# with practically the same result; wrong pairs are filtered by our own validation anyway.
DEFAULT_MODEL = "gemini-flash-lite-latest"
# Free tier allows roughly 10 requests per minute; stay below that.
MIN_INTERVAL_SECONDS = 7.0
MAX_ATTEMPTS = 4


class GeminiError(RuntimeError):
    pass


class Gemini:
    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        session: requests.Session | None = None,
        min_interval_seconds: float = MIN_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not api_key:
            raise GeminiError("GEMINI_API_KEY nije postavljen.")
        self._key = api_key
        self.model = model or DEFAULT_MODEL
        self._http = session or requests.Session()
        self._interval = min_interval_seconds
        self._sleep = sleep
        self._timeout = timeout_seconds
        self._last_call: float | None = None
        self.calls = 0

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
        }
        url = API_URL.format(model=self.model)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_turn()
            try:
                response = self._http.post(url, headers={"x-goog-api-key": self._key}, json=body, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise GeminiError(f"Gemini nije dostupan: {exc.__class__.__name__}") from exc
                self._sleep(10.0 * attempt)
                continue
            finally:
                self._last_call = time.monotonic()
                self.calls += 1

            if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                wait = _retry_after(response) or 20.0 * attempt
                log.warning("Gemini %s, ponovni pokušaj za %.0f s", response.status_code, wait)
                self._sleep(wait)
                continue
            if response.status_code != 200:
                # Error bodies describe the problem (quota, bad model name) and never echo the key.
                raise GeminiError(f"Gemini je vratio {response.status_code}: {response.text[:300]}")
            return _parse(response)
        raise GeminiError("Gemini: previše neuspjelih pokušaja.")

    def _wait_turn(self) -> None:
        if self._last_call is None:
            return
        wait = self._interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)


def _parse(response) -> Any:
    try:
        data = response.json()
        candidate = data["candidates"][0]
        text = "".join(part.get("text", "") for part in candidate["content"]["parts"])
        return json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        reason = ""
        try:
            reason = str(response.json()["candidates"][0].get("finishReason", ""))
        except Exception:
            pass
        raise GeminiError(f"Gemini odgovor nije ispravan JSON {reason}".strip()) from exc


def _retry_after(response) -> float | None:
    value = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    try:
        return min(float(value), 120.0) if value else None
    except ValueError:
        return None
