from datetime import date
from decimal import Decimal

import pytest

from akcije.filters import select_offers
from akcije.report import build_report
from tests.conftest import make_offer

TODAY = date(2026, 11, 8)
THIRTY = Decimal("30")


class TestDiscount:
    @pytest.mark.parametrize("regular,sale,expected", [
        ("10.00", "7.00", "30.00"),
        ("0.75", "0.49", "34.67"),
        ("10.00", "10.00", "0"),
        ("10.00", "12.00", "0"),
        ("0", "0", "0"),
    ])
    def test_discount_pct(self, regular, sale, expected):
        offer = make_offer(regular_price=Decimal(regular), sale_price=Decimal(sale))
        assert offer.discount_pct == Decimal(expected)


class TestSelectOffers:
    def test_exactly_thirty_percent_is_included(self):
        assert len(select_offers([make_offer(sale_price=Decimal("7.00"))], THIRTY, TODAY)) == 1

    def test_just_below_threshold_is_excluded(self):
        assert select_offers([make_offer(sale_price=Decimal("7.01"))], THIRTY, TODAY) == []

    def test_rounding_does_not_lift_below_threshold_into_it(self):
        offer = make_offer(regular_price=Decimal("66.67"), sale_price=Decimal("46.67"))  # 29.998 %
        assert offer.discount_pct == Decimal("30.00")  # shown rounded...
        assert select_offers([offer], THIRTY, TODAY) == []  # ...but compared exactly

    def test_price_increase_is_excluded(self):
        assert select_offers([make_offer(sale_price=Decimal("11"))], Decimal("0.01"), TODAY) == []

    def test_expired_offer_is_excluded_but_last_day_is_kept(self):
        expired = make_offer(product_id="a", valid_to=date(2026, 11, 7))
        last_day = make_offer(product_id="b", valid_to=TODAY)
        no_end = make_offer(product_id="c", valid_to=None)
        assert [o.product_id for o in select_offers([expired, last_day, no_end], THIRTY, TODAY)] == ["b", "c"]

    def test_empty_input(self):
        assert select_offers([], THIRTY, TODAY) == []

    @pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("100.01")])
    def test_invalid_threshold_raises(self, bad):
        with pytest.raises(ValueError):
            select_offers([], bad, TODAY)


class TestReport:
    def test_groups_by_category_and_sorts_by_discount(self):
        offers = [
            make_offer(product_id="1", name="Mali popust", category="Slatkiši", sale_price=Decimal("6.90")),
            make_offer(product_id="2", name="Veliki popust", category="Slatkiši", sale_price=Decimal("4.00")),
            make_offer(product_id="3", name="Kafa", category="Napici", sale_price=Decimal("5.00")),
        ]
        html = build_report(offers, [], TODAY, THIRTY).html
        assert html.index("Piće (1)") < html.index("Slatkiši i grickalice (2)")  # common groups, fixed order
        assert html.index("Veliki popust") < html.index("Mali popust")
        assert "-60%" in html and "4,00 €" in html and "10,00 €" in html

    def test_subject_contains_count_threshold_and_date(self):
        report = build_report([make_offer()], [], TODAY, THIRTY)
        assert report.subject == "Akcije ≥30%: 1 proizvoda (8.11.)"

    def test_html_in_product_data_is_escaped(self):
        evil = make_offer(name="<script>alert(1)</script>", category="<b>x</b>", url="javascript:alert(1)")
        html = build_report([evil], ["<img src=x onerror=1>"], TODAY, THIRTY).html
        assert "<script>" not in html and "<b>x</b>" not in html and "<img src=x" not in html
        assert "javascript:" not in html  # non-https links are dropped

    def test_only_https_links_in_email(self):
        html = build_report([make_offer(name="Plain", url="http://shop.example/1"), make_offer(product_id="2", name="Safe", url="https://shop.example/2")],
                            [], TODAY, THIRTY).html
        assert "http://shop.example/1" not in html and 'href="https://shop.example/2"' in html

    def test_errors_are_shown_and_flag_subject(self):
        report = build_report([], ["Voli: sajt nedostupan"], TODAY, THIRTY)
        assert "Voli: sajt nedostupan" in report.html
        assert report.subject.endswith("⚠️")
        assert "Nema proizvoda" in report.html

    def test_card_only_price_is_marked(self):
        assert "💳" in build_report([make_offer(card_required=True)], [], TODAY, THIRTY).html

    def test_fractional_threshold_in_subject(self):
        assert "≥27,5%" in build_report([], [], TODAY, Decimal("27.5")).subject
