"""Kuća hemije web shop (drogerija.me, Magento 2) via the public Magento GraphQL endpoint.

Why GraphQL instead of the /akcije HTML: every listing page is ~6 MB (32 products),
while GraphQL returns 100 products in a few KB. robots.txt does not restrict /graphql.

Only the store's "exclusively men's" rule matters here, so only categories where men's products
live are read (verified Nov 2026 ids). The men's filter itself runs later (akcije.gender).
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from akcije.http import HttpClient
from akcije.models import AdapterError, Offer

log = logging.getLogger(__name__)

STORE = "Kuća hemije"
BASE_URL = "https://drogerija.me"
GRAPHQL_URL = f"{BASE_URL}/graphql?query={{query}}"
PAGE_SIZE = 100
MAX_PAGES = 30

# category id -> label shown in the report
CATEGORIES: dict[int, str] = {
    1039: "Muška njega",
    958: "Muški parfemi",
    960: "Parfemi – setovi",
    1023: "Dezodoransi",
    1013: "Kupanje",
    1038: "Poklon setovi",
}

_QUERY = (
    "{products(filter:{category_id:{eq:\"%d\"}},pageSize:%d,currentPage:%d){"
    "total_count page_info{total_pages} items{sku name url_key url_suffix special_to_date special_from_date "
    "categories{name} price_range{minimum_price{regular_price{value} final_price{value}}}}}}"
)


def fetch_offers(client: HttpClient) -> list[Offer]:
    offers: dict[str, Offer] = {}
    requests_made = 0
    for category_id, label in CATEGORIES.items():
        page, total_pages = 1, 1
        while page <= min(total_pages, MAX_PAGES):
            products = _load(client.get_text(GRAPHQL_URL.format(query=quote(_QUERY % (category_id, PAGE_SIZE, page)))))
            requests_made += 1
            total_pages = int(products["page_info"]["total_pages"] or 1)
            for item in products["items"]:
                offer = to_offer(item, label)
                if offer is not None and offer.product_id not in offers:
                    offers[offer.product_id] = offer
            page += 1

    if not offers:
        # Possible in theory (no men's products discounted), but so unlikely that it most probably means
        # a format or category change; failing keeps the previous data on the page instead of showing nothing.
        raise AdapterError("Kuća hemije: nijedan proizvod na popustu u praćenim kategorijama. Moguće je da se promijenio sajt.")
    log.info("Kuća hemije: %d proizvoda na popustu (%d zahtjeva)", len(offers), requests_made)
    return list(offers.values())


def _load(text: str) -> dict:
    try:
        data = json.loads(text)
        if data.get("errors"):
            raise AdapterError(f"Kuća hemije: GraphQL greška: {str(data['errors'])[:200]}")
        products = data["data"]["products"]
        if not isinstance(products["items"], list):
            raise TypeError
        products["page_info"]["total_pages"]
    except AdapterError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AdapterError("Kuća hemije: GraphQL je vratio neočekivan format.") from None
    return products


def to_offer(item: dict, label: str) -> Offer | None:
    if not isinstance(item, dict):
        return None
    try:
        price = item["price_range"]["minimum_price"]
        regular = _decimal(price["regular_price"]["value"])
        sale = _decimal(price["final_price"]["value"])
    except (KeyError, TypeError):
        return None
    sku, name = item.get("sku"), (item.get("name") or "").strip()
    if not sku or not name or regular is None or sale is None or sale <= 0 or sale >= regular:
        return None  # not discounted

    url = None
    if item.get("url_key"):
        url = f"{BASE_URL}/{quote(str(item['url_key']))}{item.get('url_suffix') or '.html'}"
    categories = tuple(str(c["name"]) for c in item.get("categories") or [] if isinstance(c, dict) and c.get("name"))
    return Offer(
        store=STORE,
        product_id=str(sku),
        name=name,
        category=label,
        regular_price=regular,
        sale_price=sale,
        valid_from=_date(item.get("special_from_date")),
        valid_to=_date(item.get("special_to_date")),
        url=url,
        tags=categories,
    )


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result.quantize(Decimal("0.01")) if result.is_finite() and result >= 0 else None


def _date(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
