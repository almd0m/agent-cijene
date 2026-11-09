from __future__ import annotations

import json
from decimal import Decimal
from html import escape
from pathlib import Path

import pytest

from akcije.models import Offer

FIXTURES = Path(__file__).parent / "fixtures"


def voli_card(code: int, name: str, regular: str, special: str, category_id: int = 44,
              category_name: str | None = "Cerealije", date_to: str = "2026-11-30T22:59:00.000000Z") -> str:
    """Renders a card with the same structure as the real voli.me markup."""
    product = {
        "id": code, "name": name, "regular_price": regular, "special_price": special,
        "special_price_date_from": "2026-11-01T23:00:01.000000Z", "special_price_date_to": date_to,
        "article_code": code, "category_id": category_id, "selling_quantity_unit": "kom",
    }
    if category_name is not None:
        product["category"] = {"id": category_id, "name": {"en": "x", "me": category_name}}
    return (
        f'<div data-article-code="{code}" class="product-card "><div class="card-body">'
        f'<a href="https://www.voli.me/proizvod/{code}">{escape(name)}</a>'
        f'<add-to-cart-button :product="{escape(json.dumps(product), quote=True)}"></add-to-cart-button>'
        "</div></div>"
    )


def voli_page(cards: list[str], max_page: int | None = None) -> str:
    pagination = ""
    if max_page:
        links = "".join(f'<li><a class="page-link" href="https://www.voli.me/tag/akcije?page={p}">{p}</a></li>' for p in range(1, max_page + 1))
        pagination = f'<ul class="pagination">{links}</ul>'
    return f"<html><body>{''.join(cards)}{pagination}</body></html>"


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url: str) -> str:
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"neočekivan zahtjev: {url}")
        return self.pages[url]


def make_offer(**overrides) -> Offer:
    values = dict(store="Voli", product_id="1", name="Proizvod", category="Kategorija",
                  regular_price=Decimal("10.00"), sale_price=Decimal("7.00"))
    values.update(overrides)
    return Offer(**values)


@pytest.fixture
def real_overview_html() -> str:
    return (FIXTURES / "voli_overview_snippet.html").read_text(encoding="utf-8")
