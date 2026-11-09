from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


class AdapterError(RuntimeError):
    """A store adapter could not produce a trustworthy result (site down, format changed...)."""


@dataclass(frozen=True)
class Offer:
    store: str
    product_id: str
    name: str
    category: str
    regular_price: Decimal
    sale_price: Decimal
    valid_from: date | None = None
    valid_to: date | None = None
    unit: str | None = None
    url: str | None = None
    card_required: bool = False
    # Extra category names / labels used only for classification (e.g. the men's-product filter).
    tags: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.store, self.product_id)

    @property
    def exact_discount_pct(self) -> Decimal:
        """Unrounded discount, for comparing with the threshold (29.995 % must not pass as 30 %)."""
        if self.regular_price <= 0 or self.sale_price >= self.regular_price:
            return Decimal("0")
        return (self.regular_price - self.sale_price) / self.regular_price * 100

    @property
    def discount_pct(self) -> Decimal:
        """Discount computed from prices (rounded for display); the store's own percentage is not trusted."""
        return self.exact_discount_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
