"""Local development cache (only with --dry-run).

Stores the OUTPUT of each store adapter (list[Offer] after Gemini parsing), one JSON file per
store in .cache/, so repeated local runs neither download the sites again nor spend Gemini quota.
The men's-filter Gemini verdicts are cached the same way in .cache/muski-filter.json.

A broken, foreign or expired cache file is never fatal: it is ignored (with a warning when
broken) and fresh data is fetched instead. Never used by GitHub Actions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from akcije.models import Offer

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = ".cache"
MAX_AGE = timedelta(hours=12)
VERSION = 1
VERDICTS_FILE = "muski-filter.json"

_DECIMAL_FIELDS = ("regular_price", "sale_price")
_DATE_FIELDS = ("valid_from", "valid_to")


class CacheFormatError(ValueError):
    pass


class OfferCache:
    def __init__(self, directory: str | Path, now: datetime, max_age: timedelta = MAX_AGE) -> None:
        if now.tzinfo is None:
            raise ValueError("now mora imati vremensku zonu")
        self.directory = Path(directory)
        self.now = now
        self.max_age = max_age

    def path(self, store: str) -> Path:
        return self.directory / f"{slug(store)}.json"

    def load(self, store: str) -> tuple[list[Offer], timedelta] | None:
        """Cached offers and their age, or None when missing, expired or broken."""
        path = self.path(store)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != VERSION or data.get("store") != store:
                raise CacheFormatError("nepoznata verzija ili druga prodavnica")
            age = self._age(data.get("created_at"))
            if age is None:
                return None
            raw_offers = data.get("offers")
            if not isinstance(raw_offers, list):
                raise CacheFormatError("nema liste ponuda")
            offers = [offer_from_json(item) for item in raw_offers]
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
            log.warning("%s: keš %s je oštećen (%s), ignorišem ga i skidam sveže podatke.", store, path, exc)
            return None
        return offers, age

    def save(self, store: str, offers: list[Offer]) -> None:
        data = {
            "version": VERSION,
            "store": store,
            "created_at": self.now.isoformat(),
            "offers": [offer_to_json(o) for o in offers],
        }
        _write_atomic(self.path(store), data)

    def _age(self, created_at) -> timedelta | None:
        """Age of a cache entry; None when expired. Raises CacheFormatError for invalid timestamps."""
        if not isinstance(created_at, str):
            raise CacheFormatError("nema vremena kreiranja")
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            raise CacheFormatError("vrijeme kreiranja bez vremenske zone")
        age = self.now - created
        if age < timedelta(0):
            raise CacheFormatError("vrijeme kreiranja je u budućnosti")
        return age if age <= self.max_age else None


class VerdictCache:
    """Gemini answers of the men's filter, keyed by (store, product_id, name), each with its own timestamp.

    With refresh=True nothing is read (every product is asked again), but answers are still saved.
    """

    def __init__(self, directory: str | Path, now: datetime, refresh: bool = False, max_age: timedelta = MAX_AGE) -> None:
        self.path = Path(directory) / VERDICTS_FILE
        self.now = now
        self.max_age = max_age
        self._entries: dict[str, dict] = {} if refresh else self._read()
        self._dirty = False

    def get(self, offer: Offer) -> bool | None:
        entry = self._entries.get(_verdict_key(offer))
        return entry["mens_only"] if entry else None

    def put(self, offer: Offer, mens_only: bool) -> None:
        self._entries[_verdict_key(offer)] = {"mens_only": bool(mens_only), "decided_at": self.now.isoformat()}
        self._dirty = True

    def save(self) -> None:
        if self._dirty:
            _write_atomic(self.path, {"version": VERSION, "verdicts": self._entries})
            self._dirty = False

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("verdicts"), dict):
                raise CacheFormatError("nepoznat format")
            fresh = {}
            for key, entry in data["verdicts"].items():
                if not isinstance(entry, dict) or not isinstance(entry.get("mens_only"), bool):
                    raise CacheFormatError(f"neispravan unos {key!r}")
                decided = datetime.fromisoformat(entry["decided_at"])
                if decided.tzinfo is not None and timedelta(0) <= self.now - decided <= self.max_age:
                    fresh[key] = entry
            return fresh
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("Keš muškog filtera %s je oštećen (%s), ignorišem ga.", self.path, exc)
            return {}


def offer_to_json(offer: Offer) -> dict:
    data = asdict(offer)
    for name in _DECIMAL_FIELDS:
        data[name] = str(data[name])  # str keeps every digit, float would not
    for name in _DATE_FIELDS:
        data[name] = data[name].isoformat() if data[name] else None
    data["tags"] = list(data["tags"])
    return data


def offer_from_json(data) -> Offer:
    if not isinstance(data, dict):
        raise CacheFormatError("ponuda nije objekat")
    values = dict(data)
    for name in _DECIMAL_FIELDS:
        if not isinstance(values.get(name), str):
            raise CacheFormatError(f"{name} mora biti string")
        value = Decimal(values[name])
        if not value.is_finite():
            raise CacheFormatError(f"{name} nije broj")
        values[name] = value
    for name in _DATE_FIELDS:
        raw = values.get(name)
        values[name] = date.fromisoformat(raw) if raw is not None else None
    values["tags"] = tuple(values.get("tags") or ())
    return Offer(**values)


def slug(store: str) -> str:
    text = store.replace("đ", "dj").replace("Đ", "Dj")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "prodavnica"


def format_age(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _verdict_key(offer: Offer) -> str:
    return json.dumps([offer.store, offer.product_id, offer.name], ensure_ascii=False)


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
