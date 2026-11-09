"""Cosmetics Market (cosmetics-market.com) via its internal JSON API.

Verified Nov 2026:
- GET https://cosmetics.2bi.me/wapi/articles?action_filter=Akcije&per_page=100&page=N
- response.{current_page,last_page,total,data[]}; ~3.700 discounted articles -> ~37 requests.
- Beauty Friend (loyalty card) prices are ignored on purpose.
This is an undocumented API: any format change raises AdapterError instead of guessing.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from akcije.http import HttpClient
from akcije.models import AdapterError, Offer

log = logging.getLogger(__name__)

STORE = "Cosmetics"
API_URL = "https://cosmetics.2bi.me/wapi/articles?action_filter=Akcije&per_page={per_page}&page={page}"
PER_PAGE = 100
MAX_PAGES = 100


def fetch_offers(client: HttpClient) -> list[Offer]:
    offers: dict[str, Offer] = {}
    skipped = 0
    page, last_page, total = 1, 1, 0
    while page <= min(last_page, MAX_PAGES):
        response = _load(client.get_text(API_URL.format(per_page=PER_PAGE, page=page)))
        last_page, total = int(response["last_page"]), int(response.get("total") or 0)
        for article in response["data"]:
            offer = to_offer(article)
            if offer is None:
                skipped += 1
            else:
                offers[offer.product_id] = offer
        page += 1

    if not offers:
        raise AdapterError("Cosmetics: nijedan artikal nije pronađen. Moguće je da se promijenio API.")
    if skipped:
        log.warning("Cosmetics: preskočeno %d artikala sa neispravnim podacima", skipped)
    log.info("Cosmetics: %d artikala na akciji (%d stranica, API kaže ukupno %d)", len(offers), page - 1, total)
    return list(offers.values())


def _load(text: str) -> dict:
    try:
        data = json.loads(text)
        response = data["response"]
        if not isinstance(response["data"], list) or "last_page" not in response:
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise AdapterError("Cosmetics: API je vratio neočekivan format.") from None
    return response


def to_offer(article: dict) -> Offer | None:
    if not isinstance(article, dict):
        return None
    article_id = article.get("db_id") or article.get("id")
    name = " ".join(str(article.get(k) or "").strip() for k in ("brand", "name")).strip()
    regular = _decimal(article.get("priceWithTax"))
    sale = _decimal(article.get("priceWithDiscount"))
    if article_id is None or not name or regular is None or sale is None or sale <= 0:
        return None
    unit = str(article.get("firstClassifier") or "").strip() or None
    if unit and _compact(unit) not in _compact(name):
        # Variants (100ML / 30ML) share one name; the size keeps them apart on the page and in the shopping list.
        name = f"{name} {unit}"

    url = article.get("share_url")
    return Offer(
        store=STORE,
        product_id=str(article_id),
        name=name,
        category=str(article.get("categoryDescription") or article.get("mainCategoryDescription") or "Ostalo"),
        regular_price=regular,
        sale_price=sale,
        valid_from=_date(article.get("discountStartDate")),
        valid_to=_date(article.get("discountEndDate")),
        unit=unit,
        url=url if isinstance(url, str) and url.startswith("https://") else None,
        tags=tuple(str(article.get(k)) for k in ("mainCategoryDescription", "subCategoryDescription") if article.get(k)),
    )


def _compact(text: str) -> str:
    return "".join(text.lower().split())


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
