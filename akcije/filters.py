from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from akcije.models import Offer


def is_active(offer: Offer, today: date) -> bool:
    """Not yet expired. Offers starting in the next few days are kept so they can be planned for."""
    return offer.valid_to is None or offer.valid_to >= today


def select_offers(offers: Iterable[Offer], min_discount_pct: Decimal, today: date) -> list[Offer]:
    if min_discount_pct < 0 or min_discount_pct > 100:
        raise ValueError(f"min_discount_pct mora biti između 0 i 100, a dobijeno je {min_discount_pct}")
    return [o for o in offers if o.exact_discount_pct >= min_discount_pct and is_active(o, today)]
