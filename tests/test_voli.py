from datetime import date
from decimal import Decimal

import pytest

from akcije.adapters import voli
from akcije.models import AdapterError
from tests.conftest import FakeClient, voli_card, voli_page

OVERVIEW = voli.OVERVIEW_URL


def page_url(n: int) -> str:
    return f"{OVERVIEW}?page={n}"


def cat_url(cid: int) -> str:
    return voli.CATEGORY_URL.format(category_id=cid)


class TestParseRealMarkup:
    def test_parses_real_card_fields(self, real_overview_html):
        parsed = voli.parse_page(real_overview_html)
        offer = parsed.offers["8807"]
        assert offer.name == "Pahuljice ražane 250 g Fun&Fit Company"
        assert offer.regular_price == Decimal("0.75")
        assert offer.sale_price == Decimal("0.65")
        assert offer.category == "Cerealije (musli, corn flakes)"
        assert offer.unit == "kom"
        assert offer.url == "https://www.voli.me/proizvod/4014"
        assert offer.store == "Voli"

    def test_converts_utc_timestamps_to_local_dates(self, real_overview_html):
        offer = voli.parse_page(real_overview_html).offers["8807"]
        # 2026-11-03T23:00:01Z is 4.11. in Podgorica; 2026-11-30T22:59Z is still 30.11.
        assert offer.valid_from == date(2026, 11, 4)
        assert offer.valid_to == date(2026, 11, 30)

    def test_reads_last_page_from_pagination(self, real_overview_html):
        assert voli.parse_page(real_overview_html).max_page == 19

    def test_discount_is_computed_from_prices(self, real_overview_html):
        assert voli.parse_page(real_overview_html).offers["169263"].discount_pct == Decimal("36.52")


class TestParseEdgeCases:
    def test_page_without_pagination_has_one_page(self):
        assert voli.parse_page(voli_page([voli_card(1, "A", "2.00", "1.00")])).max_page == 1

    def test_duplicate_cards_are_deduplicated(self):
        card = voli_card(1, "A", "2.00", "1.00")
        assert list(voli.parse_page(voli_page([card, card])).offers) == ["1"]

    def test_card_without_category_object_uses_known_name(self):
        html = voli_page([voli_card(1, "A", "2.00", "1.00", category_id=44, category_name=None)])
        offer = voli.parse_page(html, category_names={"44": "Cerealije"}).offers["1"]
        assert offer.category == "Cerealije"

    def test_unknown_category_falls_back(self):
        html = voli_page([voli_card(1, "A", "2.00", "1.00", category_name=None)])
        assert voli.parse_page(html).offers["1"].category == voli.UNKNOWN_CATEGORY

    @pytest.mark.parametrize("bad_card", [
        '<div class="product-card"><p>bez dugmeta</p></div>',
        '<div class="product-card"><add-to-cart-button :product="{nije json"></add-to-cart-button></div>',
        '<div class="product-card"><add-to-cart-button :product="[1,2]"></add-to-cart-button></div>',
    ])
    def test_malformed_cards_are_skipped_and_counted(self, bad_card):
        parsed = voli.parse_page(voli_page([bad_card, voli_card(1, "A", "2.00", "1.00")]))
        assert list(parsed.offers) == ["1"]
        assert parsed.skipped_cards == 1

    @pytest.mark.parametrize("regular,special", [("abc", "1.00"), ("2.00", None), ("-1", "0.5"), ("NaN", "1"), (True, "1")])
    def test_invalid_prices_are_skipped(self, regular, special):
        parsed = voli.parse_page(voli_page([voli_card(1, "A", regular, special)]))
        assert parsed.offers == {}
        assert parsed.skipped_cards == 1

    def test_invalid_dates_become_none(self):
        offer = voli.parse_page(voli_page([voli_card(1, "A", "2.00", "1.00", date_to="sutra")])).offers["1"]
        assert offer.valid_to is None


class TestFetchOffers:
    def test_crawls_all_overview_pages(self):
        client = FakeClient({
            OVERVIEW: voli_page([voli_card(1, "A", "2", "1", category_id=10)], max_page=3),
            page_url(2): voli_page([voli_card(2, "B", "2", "1", category_id=11)], max_page=3),
            page_url(3): voli_page([voli_card(3, "C", "2", "1", category_id=12)], max_page=3),
        })
        offers = voli.fetch_offers(client)
        assert sorted(o.product_id for o in offers) == ["1", "2", "3"]
        assert client.requested == [OVERVIEW, page_url(2), page_url(3)]

    def test_truncated_category_is_fetched_in_full(self):
        overview_cards = [voli_card(i, f"P{i}", "2", "1", category_id=44, category_name="Cerealije")
                          for i in range(voli.OVERVIEW_PER_CATEGORY_LIMIT)]
        full_category = [voli_card(i, f"P{i}", "2", "1", category_id=44, category_name=None)
                         for i in range(voli.OVERVIEW_PER_CATEGORY_LIMIT + 5)]
        client = FakeClient({
            OVERVIEW: voli_page(overview_cards + [voli_card(99, "Mali", "2", "1", category_id=7, category_name="Med")]),
            cat_url(44): voli_page(full_category + full_category),  # category pages repeat every card
        })
        offers = {o.product_id: o for o in voli.fetch_offers(client)}
        assert len(offers) == voli.OVERVIEW_PER_CATEGORY_LIMIT + 5 + 1
        assert offers["14"].category == "Cerealije"  # name learned from the overview page
        assert cat_url(7) not in client.requested  # complete categories are not re-fetched

    def test_category_below_limit_is_not_fetched(self):
        cards = [voli_card(i, f"P{i}", "2", "1") for i in range(voli.OVERVIEW_PER_CATEGORY_LIMIT - 1)]
        client = FakeClient({OVERVIEW: voli_page(cards)})
        voli.fetch_offers(client)
        assert client.requested == [OVERVIEW]

    def test_page_count_is_capped(self):
        huge = voli_page([voli_card(1, "A", "2", "1")], max_page=voli.MAX_PAGES + 50)
        pages = {OVERVIEW: huge, **{page_url(n): huge for n in range(2, voli.MAX_PAGES + 51)}}
        client = FakeClient(pages)
        voli.fetch_offers(client)
        assert len(client.requested) == voli.MAX_PAGES

    def test_no_products_raises_adapter_error(self):
        with pytest.raises(AdapterError, match="format"):
            voli.fetch_offers(FakeClient({OVERVIEW: "<html><body>Održavanje sajta</body></html>"}))

    def test_network_error_propagates(self):
        class Broken:
            def get_text(self, url):
                raise ConnectionError("timeout")

        with pytest.raises(ConnectionError):
            voli.fetch_offers(Broken())
