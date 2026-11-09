"""Voli web shop (voli.me/tag/akcije).

Every product card embeds the full product as JSON in the `:product` attribute of
<add-to-cart-button>, so no AI or price-text parsing is needed.

Site behaviour (verified Nov 2026):
- /tag/akcije?page=N lists discounted products grouped by category, max 10 per category.
- /tag/akcije/kategorije/<id> lists all discounted products of one category, no pagination,
  but every card appears twice (desktop + mobile markup) and the JSON lacks the `category` object.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from akcije.http import HttpClient
from akcije.models import AdapterError, Offer

log = logging.getLogger(__name__)

STORE = "Voli"
BASE_URL = "https://www.voli.me"
OVERVIEW_URL = f"{BASE_URL}/tag/akcije"
CATEGORY_URL = f"{BASE_URL}/tag/akcije/kategorije/{{category_id}}"
OVERVIEW_PER_CATEGORY_LIMIT = 10
MAX_PAGES = 60  # guard against a broken paginator sending us into an endless crawl
LOCAL_TZ = ZoneInfo("Europe/Podgorica")
UNKNOWN_CATEGORY = "Ostalo"

_PAGE_RE = re.compile(r"[?&]page=(\d+)")


@dataclass
class ParsedPage:
    offers: dict[str, Offer] = field(default_factory=dict)
    category_ids: dict[str, str] = field(default_factory=dict)  # article_code -> category_id
    category_names: dict[str, str] = field(default_factory=dict)  # category_id -> name
    max_page: int = 1
    skipped_cards: int = 0


def fetch_offers(client: HttpClient) -> list[Offer]:
    first = parse_page(client.get_text(OVERVIEW_URL))
    pages = [first]
    last_page = min(first.max_page, MAX_PAGES)
    for page in range(2, last_page + 1):
        pages.append(parse_page(client.get_text(f"{OVERVIEW_URL}?page={page}")))

    offers: dict[str, Offer] = {}
    category_ids: dict[str, str] = {}
    category_names: dict[str, str] = {}
    skipped = 0
    for parsed in pages:
        offers.update(parsed.offers)
        category_ids.update(parsed.category_ids)
        category_names.update(parsed.category_names)
        skipped += parsed.skipped_cards

    counts: dict[str, int] = {}
    for category_id in category_ids.values():
        counts[category_id] = counts.get(category_id, 0) + 1
    truncated = sorted(cid for cid, n in counts.items() if n >= OVERVIEW_PER_CATEGORY_LIMIT)

    for category_id in truncated:
        parsed = parse_page(
            client.get_text(CATEGORY_URL.format(category_id=category_id)),
            category_names=category_names,
        )
        skipped += parsed.skipped_cards
        offers.update(parsed.offers)

    if not offers:
        raise AdapterError("Voli: nijedan proizvod nije pronađen. Moguće je da se promijenio format sajta.")
    if skipped:
        log.warning("Voli: preskočeno %d kartica sa neispravnim podacima", skipped)
    log.info("Voli: %d proizvoda na akciji (%d stranica, %d dopunjenih kategorija)", len(offers), len(pages), len(truncated))
    return list(offers.values())


def parse_page(html: str, category_names: dict[str, str] | None = None) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    result = ParsedPage(max_page=_max_page(soup))
    known_names = dict(category_names or {})

    for card in soup.select("div.product-card"):
        product = _product_json(card)
        if product is None:
            result.skipped_cards += 1
            continue

        category_id = str(product.get("category_id") or "")
        category_name = _category_name(product.get("category"))
        if category_id and category_name:
            known_names[category_id] = category_name
            result.category_names[category_id] = category_name

        offer = _to_offer(product, card, known_names.get(category_id, UNKNOWN_CATEGORY))
        if offer is None:
            result.skipped_cards += 1
            continue
        result.offers[offer.product_id] = offer
        if category_id:
            result.category_ids[offer.product_id] = category_id
    return result


def _max_page(soup: BeautifulSoup) -> int:
    pages = [int(m.group(1)) for a in soup.select("ul.pagination a[href]") if (m := _PAGE_RE.search(a["href"]))]
    return max(pages, default=1)


def _product_json(card) -> dict | None:
    button = card.find("add-to-cart-button")
    raw = button.get(":product") if button else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _category_name(category) -> str | None:
    if not isinstance(category, dict):
        return None
    name = category.get("name")
    if isinstance(name, dict):
        name = name.get("me") or name.get("en")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _to_offer(product: dict, card, category: str) -> Offer | None:
    code = product.get("article_code")
    name = product.get("name_me") or product.get("name")
    regular = _decimal(product.get("regular_price"))
    sale = _decimal(product.get("special_price"))
    if code is None or not name or regular is None or sale is None:
        return None

    link = card.select_one('a[href*="/proizvod/"]')
    return Offer(
        store=STORE,
        product_id=str(code),
        name=str(name).strip(),
        category=category,
        regular_price=regular,
        sale_price=sale,
        valid_from=_local_date(product.get("special_price_date_from")),
        valid_to=_local_date(product.get("special_price_date_to")),
        unit=product.get("selling_quantity_unit") or None,
        url=link["href"] if link else None,
    )


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result >= 0 else None


def _local_date(value) -> date | None:
    """Voli stores timestamps in UTC (e.g. 2026-11-16T22:59Z means 16.11. 23:59 local)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(LOCAL_TZ).date()
