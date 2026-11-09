"""Cosmetics, Kuća hemije and Idea adapters (no network: FakeClient / FakeGemini)."""
import json
import re
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import pytest

from akcije.adapters import cosmetics, idea, kucahemije
from akcije.models import AdapterError
from tests.conftest import FakeClient


# ---------- Cosmetics ----------

def cosmetics_article(db_id=1, name="deo sprej", brand="NIVEA", regular=10.0, sale=6.0, sub="Deo sprej muški", **extra):
    article = {
        "id": 900 + db_id, "db_id": db_id, "name": name, "brand": brand, "priceWithTax": regular,
        "priceWithDiscount": sale, "discountPercentage": 40, "discountStartDate": "2026-11-03",
        "discountEndDate": "2026-11-30", "mainCategoryDescription": "Njega tijela",
        "categoryDescription": "Deo program muški", "subCategoryDescription": sub,
        "firstClassifier": "150ML", "priceBeautyFriend": 1.0,
        "share_url": f"https://cosmetics-market.com/products/x/{db_id}",
    }
    article.update(extra)
    return article


def cosmetics_page(articles, page, last_page):
    return json.dumps({"response": {"current_page": page, "last_page": last_page, "total": 3, "data": articles}})


class TestCosmetics:
    def test_reads_all_pages_and_maps_fields(self):
        client = FakeClient({
            cosmetics.API_URL.format(per_page=100, page=1): cosmetics_page([cosmetics_article(1), cosmetics_article(2)], 1, 2),
            cosmetics.API_URL.format(per_page=100, page=2): cosmetics_page([cosmetics_article(3, regular=9.89, sale=7.39)], 2, 2),
        })
        offers = {o.product_id: o for o in cosmetics.fetch_offers(client)}
        assert set(offers) == {"1", "2", "3"}
        o = offers["1"]
        assert o.name == "NIVEA deo sprej 150ML" and o.store == "Cosmetics"
        assert o.regular_price == Decimal("10.00") and o.sale_price == Decimal("6.00")
        assert o.valid_to == date(2026, 11, 30) and o.unit == "150ML"
        assert o.category == "Deo program muški" and "Deo sprej muški" in o.tags
        assert offers["3"].sale_price == Decimal("7.39")

    @pytest.mark.parametrize("name,unit,expected", [
        ("poru homme edt", "100ML", "TED poru homme edt 100ML"),
        ("poru homme edt 100ml", "100ML", "TED poru homme edt 100ml"),  # already there
        ("poru homme edt 100 ml", "100ML", "TED poru homme edt 100 ml"),
        ("bokserice", None, "TED bokserice"),
    ])
    def test_size_is_added_to_name_once(self, name, unit, expected):
        o = cosmetics.to_offer(cosmetics_article(name=name, brand="TED", firstClassifier=unit))
        assert o.name == expected and o.unit == unit

    def test_beauty_friend_price_is_ignored(self):
        o = cosmetics.to_offer(cosmetics_article(priceBeautyFriend=0.5))
        assert o.sale_price == Decimal("6.00")

    @pytest.mark.parametrize("body", ["<html>", json.dumps({"response": {"data": "x"}}), json.dumps({"foo": 1})])
    def test_format_change_raises(self, body):
        client = FakeClient({cosmetics.API_URL.format(per_page=100, page=1): body})
        with pytest.raises(AdapterError, match="Cosmetics"):
            cosmetics.fetch_offers(client)

    def test_bad_article_is_skipped(self):
        assert cosmetics.to_offer(cosmetics_article(priceWithDiscount=None)) is None
        assert cosmetics.to_offer({"name": "x"}) is None

    def test_non_https_url_dropped(self):
        assert cosmetics.to_offer(cosmetics_article(share_url="javascript:alert(1)")).url is None


# ---------- Kuća hemije ----------

def kh_item(sku="1", name="NIVEA MEN DEO", regular=5.0, final=3.0, cats=("MUŠKA NJEGA",)):
    return {
        "sku": sku, "name": name, "url_key": f"p-{sku}", "url_suffix": ".html",
        "special_to_date": "2026-11-30 00:00:00", "special_from_date": "2026-11-02 00:00:00",
        "categories": [{"name": c} for c in cats],
        "price_range": {"minimum_price": {"regular_price": {"value": regular}, "final_price": {"value": final}}},
    }


class GraphqlClient:
    def __init__(self, by_category: dict[int, list[list[dict]]]):
        self.by_category = by_category
        self.requested = []

    def get_text(self, url):
        query = unquote(url.split("?query=", 1)[1])
        self.requested.append(query)
        cid = int(query.split('eq:"')[1].split('"')[0])
        page = int(query.split("currentPage:")[1].split(")")[0])
        pages = self.by_category.get(cid, [[]])
        return json.dumps({"data": {"products": {"total_count": 0, "page_info": {"total_pages": len(pages)}, "items": pages[page - 1]}}})


class TestKucaHemije:
    def test_reads_categories_pages_and_skips_not_discounted(self):
        client = GraphqlClient({
            1039: [[kh_item("1"), kh_item("2", final=5.0)], [kh_item("3")]],
            958: [[kh_item("1"), kh_item("4", name="BOSS BOTTLED EDT")]],
        })
        offers = {o.product_id: o for o in kucahemije.fetch_offers(client)}
        assert set(offers) == {"1", "3", "4"}  # "2" has no discount, "1" deduplicated
        o = offers["1"]
        assert o.category == "Muška njega" and o.store == "Kuća hemije"
        assert o.url == "https://drogerija.me/p-1.html"
        assert o.valid_to == date(2026, 11, 30) and o.tags == ("MUŠKA NJEGA",)
        assert offers["4"].category == "Muški parfemi"

    def test_graphql_error_raises(self):
        class Broken:
            def get_text(self, url):
                return json.dumps({"errors": [{"message": "nope"}]})
        with pytest.raises(AdapterError, match="GraphQL"):
            kucahemije.fetch_offers(Broken())

    def test_unexpected_format_raises(self):
        class Broken:
            def get_text(self, url):
                return "<html>maintenance</html>"
        with pytest.raises(AdapterError, match="neočekivan"):
            kucahemije.fetch_offers(Broken())

    def test_zero_discounted_products_raises(self):
        client = GraphqlClient({1039: [[kh_item("1", final=5.0)]]})  # nothing discounted anywhere
        with pytest.raises(AdapterError, match="nijedan proizvod"):
            kucahemije.fetch_offers(client)


# ---------- Idea ----------

CATALOG_LIST = """
<a href="/Katalozi/Redovna-akcija-cetvrtak-05.studeni-2026.-07-00-00-srijeda-18.studeni-2026.-23-00-00">x</a>
<a href="/Katalozi/Redovna-akcija-cetvrtak-05.studeni-2026.-07-00-00-srijeda-18.studeni-2026.-23-00-00">dup</a>
<a href="/Katalozi/Stara-akcija-cetvrtak-01.listopad-2026.-07-00-00-srijeda-14.listopad-2026.-23-00-00">old</a>
<a href="/Katalozi">all</a>
"""
CATALOG_PAGE = '<a href="https://catalog.idea.co.me/2026/novembar-2026/redovna-akcija-0511-1811/?page=1">Pogledaj</a>'
PAGE_TEXT = ("MA LY -39% cijena uz 1 99 super karticu 3,25 EUR MASLAC Moja Kravica Imlek 125g "
             "KROMPIR 2,5kg 2 29 2,49 EUR Č OKOLADICA Pingui Kinder 30g -34% 1 89 2,85 EUR")


def ipaper_html(texts):
    return '<script>var c={"x":1,"pageTexts":' + json.dumps(texts) + ',"device":{}};</script>'


class FakeGemini:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def generate_json(self, prompt, schema):
        self.prompts.append(prompt)
        return self.answers.pop(0)


VIEWER = "https://catalog.idea.co.me/2026/novembar-2026/redovna-akcija-0511-1811/"
REDOVNA = idea.BASE_URL + "/Katalozi/Redovna-akcija-cetvrtak-05.studeni-2026.-07-00-00-srijeda-18.studeni-2026.-23-00-00"


class TestIdea:
    def test_catalog_list_parses_dates_and_dedupes(self):
        cats = idea.parse_catalog_list(CATALOG_LIST)
        assert len(cats) == 2
        c = cats[0]
        assert c.title == "Redovna akcija"
        assert (c.valid_from, c.valid_to) == (date(2026, 11, 5), date(2026, 11, 18))
        assert cats[1].valid_to == date(2026, 10, 14)

    @pytest.mark.parametrize("raw,fixed", [
        ("Č OKOLADICA Pingui", "ČOKOLADICA Pingui"),
        ("VO Ć NI JOGURT", "VOĆNI JOGURT"),
        ("MLIJE Č NI NAPITAK vi š e vrsta", "MLIJEČNI NAPITAK više vrsta"),
        ("PISTA Ć I neslani", "PISTAĆI neslani"),
    ])
    def test_fix_diacritics(self, raw, fixed):
        assert idea.fix_diacritics(raw) == fixed

    def test_extract_page_texts(self):
        assert idea.extract_page_texts(ipaper_html(["a", "VO Ć E"])) == ["a", "VOĆE"]
        with pytest.raises(AdapterError):
            idea.extract_page_texts("<html></html>")

    def test_full_flow_validates_gemini_output(self):
        client = FakeClient({
            idea.CATALOGS_URL: CATALOG_LIST,
            REDOVNA: CATALOG_PAGE,
            VIEWER + "?page=1": "unused",
            VIEWER: ipaper_html(["naslovna bez cijena", PAGE_TEXT]),
        })
        gemini = FakeGemini([[
            {"page": 2, "name": "MASLAC Moja Kravica Imlek 125g", "sale_price": 1.99, "regular_price": 3.25,
             "discount_pct": 39, "card_required": True, "category": "Mliječni proizvodi"},
            {"page": 2, "name": "ČOKOLADICA Pingui Kinder 30g", "sale_price": 1.89, "regular_price": 2.85,
             "discount_pct": 34, "card_required": False, "category": "Slatkiši"},
            {"page": 2, "name": "KROMPIR 2,5kg", "sale_price": 2.29, "regular_price": 2.49,
             "discount_pct": None, "card_required": False, "category": "Voće i povrće"},
            # hallucinated: prices not in text
            {"page": 2, "name": "KAFA", "sale_price": 3.00, "regular_price": 9.99, "discount_pct": 70, "card_required": False, "category": "x"},
            # printed % disagrees with prices
            {"page": 2, "name": "MASLAC pogrešno", "sale_price": 1.99, "regular_price": 3.25, "discount_pct": 10, "card_required": False, "category": "x"},
            # page outside chunk
            {"page": 9, "name": "X", "sale_price": 1.99, "regular_price": 3.25, "discount_pct": 39, "card_required": False, "category": "x"},
        ]])
        offers = {o.name: o for o in idea.fetch_offers(client, gemini=gemini, today=date(2026, 11, 8))}
        assert set(offers) == {"MASLAC Moja Kravica Imlek 125g", "ČOKOLADICA Pingui Kinder 30g", "KROMPIR 2,5kg"}
        m = offers["MASLAC Moja Kravica Imlek 125g"]
        assert m.card_required and m.sale_price == Decimal("1.99") and m.regular_price == Decimal("3.25")
        assert m.valid_to == date(2026, 11, 18) and m.url == VIEWER + "?page=2"
        assert len(gemini.prompts) == 1 and "=== STRANA 2 ===" in gemini.prompts[0]
        assert "Stara" not in " ".join(client.requested)  # expired catalog is not fetched

    def test_without_gemini_raises_clear_error(self):
        with pytest.raises(AdapterError, match="GEMINI_API_KEY"):
            idea.fetch_offers(FakeClient({}), gemini=None)

    def test_one_broken_catalog_keeps_the_rest(self):
        slug = "Druga-akcija-cetvrtak-01.studeni-2026.-07-00-00-srijeda-20.studeni-2026.-23-00-00"
        two = CATALOG_LIST + f'<a href="/Katalozi/{slug}">2</a>'
        druga = idea.BASE_URL + "/Katalozi/" + slug
        client = FakeClient({idea.CATALOGS_URL: two, REDOVNA: CATALOG_PAGE, VIEWER: ipaper_html([PAGE_TEXT]), druga: "<html>no link</html>"})
        gemini = FakeGemini([[{"page": 1, "name": "MASLAC", "sale_price": 1.99, "regular_price": 3.25,
                               "discount_pct": 39, "card_required": True, "category": "M"}]])
        assert [o.name for o in idea.fetch_offers(client, gemini=gemini, today=date(2026, 11, 8))] == ["MASLAC"]


class FlakyGemini:
    """Fails on the given call numbers (1-based), answers MASLAC otherwise."""

    def __init__(self, fail_calls):
        self.fail_calls, self.calls = set(fail_calls), 0

    def generate_json(self, prompt, schema):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("Gemini 503")
        page = int(re.findall(r"=== STRANA (\d+) ===", prompt)[0])
        return [{"page": page, "name": f"MASLAC {page}", "sale_price": 1.99, "regular_price": 3.25,
                 "discount_pct": 39, "card_required": True, "category": "M"}]


class TestIdeaPartialLosses:
    def client(self, pages):
        return FakeClient({idea.CATALOGS_URL: CATALOG_LIST, REDOVNA: CATALOG_PAGE, VIEWER: ipaper_html([PAGE_TEXT] * pages)})

    def test_failed_gemini_call_loses_only_its_pages_and_warns(self):
        warnings = []
        offers = idea.fetch_offers(self.client(30), gemini=FlakyGemini({2}), today=date(2026, 11, 8), warnings=warnings)
        assert sorted(o.name for o in offers) == ["MASLAC 1", "MASLAC 25"]  # chunks 1-12 and 25-30 read
        assert len(warnings) == 1 and "strane 13–24 (RuntimeError)" in warnings[0] and "Redovna akcija" in warnings[0]

    def test_all_pages_failing_is_a_catalog_failure(self):
        with pytest.raises(AdapterError, match="nije pročitan"):
            idea.fetch_offers(self.client(5), gemini=FlakyGemini({1}), today=date(2026, 11, 8), warnings=[])

    def test_broken_catalog_next_to_working_one_is_reported(self):
        slug = "Druga-akcija-cetvrtak-01.studeni-2026.-07-00-00-srijeda-20.studeni-2026.-23-00-00"
        client = FakeClient({idea.CATALOGS_URL: CATALOG_LIST + f'<a href="/Katalozi/{slug}">2</a>', REDOVNA: CATALOG_PAGE,
                             VIEWER: ipaper_html([PAGE_TEXT]), idea.BASE_URL + "/Katalozi/" + slug: "<html>no link</html>"})
        warnings = []
        assert len(idea.fetch_offers(client, gemini=FlakyGemini(set()), today=date(2026, 11, 8), warnings=warnings)) == 1
        assert warnings == ["Idea: link na iPaper katalog nije pronađen."]


def test_idea_image_only_catalog_is_skipped_without_gemini_call():
    client = FakeClient({idea.CATALOGS_URL: CATALOG_LIST, REDOVNA: CATALOG_PAGE, VIEWER: ipaper_html([])})
    gemini = FakeGemini([])
    with pytest.raises(AdapterError, match="nijedna akcija"):
        idea.fetch_offers(client, gemini=gemini, today=date(2026, 11, 8))
    assert gemini.prompts == []


def test_idea_price_split_by_words_is_accepted():
    cat = idea.Catalog("x", "u", None, None)
    chunk = {1: "-34% cijena uz 1 super karticu 89 2,85 EUR PAVLAKA ZA KUVANJE"}
    item = {"page": 1, "name": "PAVLAKA", "sale_price": 1.89, "regular_price": 2.85, "discount_pct": 34,
            "card_required": True, "category": "Mliječni proizvodi i jaja"}
    offer = idea.validate_item(item, chunk, cat, "https://v/")
    assert offer is not None and offer.category == "Mliječni proizvodi i jaja"
    assert idea.validate_item({**item, "category": "izmišljeno"}, chunk, cat, "https://v/").category == "Ostalo"
