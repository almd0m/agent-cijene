"""Price history in SQLite (step 5).

One row per price *period*: a product with unchanged prices only moves `last_seen` forward,
so the file stays small even with two runs per week. Answers two questions:

- is_new: the product was not on sale in the previous successful run of ITS store -> 🆕 in email/site
  (a store that failed in between does not make all of its products look new)
- suspicious: the "regular" price is now noticeably higher than the lowest regular price seen
  in the last 90 days, i.e. the store raised the old price to make the discount look bigger -> ⚠️

Limitation: Voli and Cosmetics only list products while they are discounted, so their
regular price is only known from earlier promotions of the same product.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from akcije.models import Offer

SUSPICIOUS_MARGIN = Decimal("1.05")  # regular price raised by more than 5 %
LOOKBACK_DAYS = 90
KEEP_DAYS = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS prices (
    store TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    regular_cents INTEGER NOT NULL,
    sale_cents INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prices_product ON prices (store, product_id, last_seen);
CREATE TABLE IF NOT EXISTS store_runs (
    store TEXT NOT NULL,
    day TEXT NOT NULL,
    offers INTEGER NOT NULL,
    PRIMARY KEY (store, day)
);
"""


@dataclass(frozen=True)
class Flags:
    is_new: bool = False
    suspicious: bool = False
    lowest_regular: Decimal | None = None


class PriceHistory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> PriceHistory:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def previous_run(self, today: date, store: str | None = None) -> date | None:
        """Last run before today. With `store`: the last run in which that store delivered data
        (falls back to any run for databases written before stores were tracked)."""
        if store is not None:
            row = self._db.execute("SELECT MAX(day) FROM store_runs WHERE store=? AND day < ?", (store, today.isoformat())).fetchone()
            if row and row[0]:
                return date.fromisoformat(row[0])
        row = self._db.execute("SELECT MAX(day) FROM runs WHERE day < ?", (today.isoformat(),)).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def last_count(self, store: str, today: date) -> int | None:
        """Number of offers the store delivered in its last successful run before today."""
        row = self._db.execute(
            "SELECT offers FROM store_runs WHERE store=? AND day < ? ORDER BY day DESC LIMIT 1", (store, today.isoformat()),
        ).fetchone()
        return int(row[0]) if row else None

    def flags(self, offers: Iterable[Offer], today: date) -> dict[tuple[str, str], Flags]:
        """Must be called BEFORE record() for the same day."""
        previous_by_store: dict[str, date | None] = {}
        since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        result: dict[tuple[str, str], Flags] = {}
        for offer in offers:
            if offer.store not in previous_by_store:
                previous_by_store[offer.store] = self.previous_run(today, offer.store)
            previous = previous_by_store[offer.store]
            is_new = False
            if previous is not None:
                seen = self._db.execute(
                    "SELECT 1 FROM prices WHERE store=? AND product_id=? AND first_seen<=? AND last_seen>=? LIMIT 1",
                    (offer.store, offer.product_id, previous.isoformat(), previous.isoformat()),
                ).fetchone()
                is_new = seen is None
            row = self._db.execute(
                "SELECT MIN(regular_cents) FROM prices WHERE store=? AND product_id=? AND last_seen>=? AND last_seen<?",
                (offer.store, offer.product_id, since, today.isoformat()),
            ).fetchone()
            lowest = Decimal(row[0]) / 100 if row and row[0] is not None else None
            suspicious = lowest is not None and offer.regular_price > lowest * SUSPICIOUS_MARGIN
            result[offer.key] = Flags(is_new=is_new, suspicious=suspicious, lowest_regular=lowest)
        return result

    def record(self, offers: Iterable[Offer], today: date, stores: Iterable[str] | None = None) -> None:
        """Records one run. `stores` = stores that delivered data (default: those present in offers);
        a store that failed must not be listed, so its products are not "new" next time."""
        offers = list(offers)
        day = today.isoformat()
        counts = {store: 0 for store in (stores if stores is not None else {o.store for o in offers})}
        for offer in offers:
            if offer.store in counts:
                counts[offer.store] += 1
        with self._db:
            for offer in offers:
                regular, sale = _cents(offer.regular_price), _cents(offer.sale_price)
                last = self._db.execute(
                    "SELECT rowid, regular_cents, sale_cents FROM prices WHERE store=? AND product_id=? "
                    "ORDER BY last_seen DESC, rowid DESC LIMIT 1",
                    (offer.store, offer.product_id),
                ).fetchone()
                if last and last[1] == regular and last[2] == sale:
                    self._db.execute("UPDATE prices SET last_seen=MAX(last_seen, ?), name=? WHERE rowid=?", (day, offer.name, last[0]))
                else:
                    self._db.execute(
                        "INSERT INTO prices (store, product_id, name, regular_cents, sale_cents, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (offer.store, offer.product_id, offer.name, regular, sale, day, day),
                    )
            self._db.execute("INSERT OR IGNORE INTO runs (day) VALUES (?)", (day,))
            for store, count in counts.items():
                self._db.execute("INSERT OR REPLACE INTO store_runs (store, day, offers) VALUES (?, ?, ?)", (store, day, count))
            cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
            self._db.execute("DELETE FROM prices WHERE last_seen < ?", (cutoff,))
            self._db.execute("DELETE FROM runs WHERE day < ?", (cutoff,))
            self._db.execute("DELETE FROM store_runs WHERE day < ?", (cutoff,))


def _cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value())
