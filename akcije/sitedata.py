"""Writes site/akcije.json, the only data source of the static web page (site/index.html).

When a store fails, its offers from the previous akcije.json are carried over (only those that
have not expired), marked with `stale_since`, so one broken store does not empty the page.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from akcije.categories import GROUPS, group_of
from akcije.history import Flags
from akcije.models import Offer

log = logging.getLogger(__name__)


@dataclass
class CarriedOver:
    rows: list[dict] = field(default_factory=list)
    stale_stores: dict[str, date] = field(default_factory=dict)  # store -> date the shown data is from


def build_site_data(
    offers: Sequence[Offer],
    errors: Sequence[str],
    min_discount_pct: Decimal,
    generated_at: datetime,
    flags: Mapping[tuple[str, str], Flags] | None = None,
    carried: CarriedOver | None = None,
) -> dict:
    flags = flags or {}
    rows = []
    for offer in sorted(offers, key=lambda o: (-o.discount_pct, o.name.lower())):
        flag = flags.get(offer.key, Flags())
        rows.append({
            "store": offer.store,
            "product_id": offer.product_id,  # with store: stable key for the shopping list
            "name": offer.name,
            "group": group_of(offer),
            "category": offer.category,
            "regular_price": _num(offer.regular_price),
            "sale_price": _num(offer.sale_price),
            "discount_pct": _num(offer.discount_pct, "0.1"),
            "valid_to": offer.valid_to.isoformat() if offer.valid_to else None,
            "unit": offer.unit,
            "url": offer.url if offer.url and offer.url.startswith("https://") else None,
            "card_required": offer.card_required,
            "is_new": flag.is_new,
            "suspicious": flag.suspicious,
        })
    stale_stores: dict[str, str] = {}
    if carried and carried.rows:
        rows = sorted(rows + carried.rows, key=lambda r: (-r["discount_pct"], r["name"].lower()))
        stale_stores = {store: day.isoformat() for store, day in sorted(carried.stale_stores.items())}
    return {
        "generated_at": generated_at.isoformat(timespec="minutes"),
        "min_discount": _num(min_discount_pct, "0.1"),
        "groups": list(GROUPS),  # display order of the groups
        "errors": list(errors),
        "stale_stores": stale_stores,
        "offers": rows,
    }


def load_previous(path: str | Path) -> dict | None:
    """The previous akcije.json, or None when it is missing or broken (never fatal)."""
    target = Path(path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
            raise ValueError("nema liste ponuda")
        return data
    except (OSError, ValueError) as exc:
        log.warning("Prethodni %s nije čitljiv (%s); stari podaci se ne mogu zadržati.", target, exc)
        return None


def carry_over(previous: dict | None, failed_stores: Iterable[str], today: date, min_discount_pct: Decimal) -> CarriedOver:
    """Offers of failed stores from the previous akcije.json that are still valid and still meet the threshold."""
    result = CarriedOver()
    failed = set(failed_stores)
    if not previous or not failed:
        return result
    fallback_day = _iso_day(previous.get("generated_at"))
    for row in previous.get("offers", []):
        if not isinstance(row, dict) or row.get("store") not in failed or not _valid_row(row):
            continue
        valid_to = _iso_day(row.get("valid_to"))
        if row.get("valid_to") is not None and (valid_to is None or valid_to < today):
            continue
        if Decimal(str(row["discount_pct"])) < min_discount_pct:
            continue
        since = _iso_day(row.get("stale_since")) or fallback_day
        if since is None:
            continue
        carried = dict(row, is_new=False, stale_since=since.isoformat())
        result.rows.append(carried)
        store = row["store"]
        result.stale_stores[store] = min(since, result.stale_stores.get(store, since))
    return result


def write_site_data(path: str | Path, data: dict) -> Path:
    """Atomic write: a crash mid-way leaves the previous file untouched, never a half-written one."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _valid_row(row: dict) -> bool:
    try:
        return (
            isinstance(row.get("name"), str) and bool(row["name"])
            and all(Decimal(str(row[k])).is_finite() for k in ("regular_price", "sale_price", "discount_pct"))
        )
    except (KeyError, InvalidOperation, ValueError):
        return False


def _iso_day(value) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _num(value: Decimal, step: str = "0.01") -> float:
    return float(value.quantize(Decimal(step), rounding=ROUND_HALF_UP))
