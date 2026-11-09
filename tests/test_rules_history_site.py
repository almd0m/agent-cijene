"""Men's filter, Gemini client, price history, site JSON and short email."""
import json
from datetime import date, datetime
from decimal import Decimal

import pytest

import akcije.__main__ as app
from akcije.gemini import Gemini, GeminiError
from akcije.gender import Gender, apply_store_rules, classify
from akcije.history import Flags, PriceHistory
from akcije.report import build_report
from akcije.sitedata import build_site_data, write_site_data
from tests.conftest import make_offer

TODAY = date(2026, 11, 8)
THIRTY = Decimal("30")


def cos(name, category="Ostalo", tags=(), pid=None):
    return make_offer(store="Cosmetics", product_id=pid or name, name=name, category=category, tags=tags)


class TestGender:
    @pytest.mark.parametrize("name,category,tags,expected", [
        ("NIVEA deo sprej men", "Kupanje", (), Gender.MEN),
        ("BOSS bottled EDT", "Deo program muški", ("Parfem muški",), Gender.MEN),
        ("DOLCE&GABBANA K EDP man", "Parfimisani proizvodi za žene", (), Gender.MEN),  # name beats category
        ("GILLETTE pjena za brijanje", "Njega tijela", (), Gender.MEN),
        ("GILLETTE Venus brijač", "Proizvodi prije i posle brijanja", ("Brijac ženski",), Gender.NOT_MEN),
        ("brijač jednokratni", "Proizvodi prije i posle brijanja", ("Brijac ženski",), Gender.NOT_MEN),
        ("CK one EDT unisex", "Parfemi", (), Gender.NOT_MEN),
        ("set for him & for her", "Setovi", (), Gender.NOT_MEN),
        ("Šampon kopriva", "Njega kose", ("Šamponi",), Gender.NOT_MEN),
        ("MONTALE black aoud EDP", "Parfemi", (), Gender.UNCLEAR),
        ("BOROTALCO dezodorans sprej", "Dezodoransi", (), Gender.UNCLEAR),
        ("Ženski parfem X", "Muški parfemi", (), Gender.NOT_MEN),
        # "man" inside character names is not a men's signal; children's products never are
        ("Spider-Man pjena za kupanje", "Kupanje", (), Gender.NOT_MEN),
        ("SPIDER MAN šampon 300ml", "Muška njega", (), Gender.NOT_MEN),
        ("Iron Man gel za tuširanje", "Kupanje", (), Gender.NOT_MEN),
        ("Pac-Man figura", "Igračke", (), Gender.NOT_MEN),
        ("He-Man edt", "Parfemi", (), Gender.NOT_MEN),
        ("X-Man gel", "Kupanje", (), Gender.NOT_MEN),
        ("Batman deo sprej", "Dezodoransi", (), Gender.NOT_MEN),
        ("Dječji šampon za dječake", "Muška njega", (), Gender.NOT_MEN),
        ("NIVEA kids deo", "Dezodoransi", (), Gender.NOT_MEN),
        ("Gel za kosu", "Dječja kozmetika", (), Gender.NOT_MEN),
        ("HUGO Man EDT", "Parfemi", (), Gender.MEN),
        ("Dezodorans man black & white", "DEO", (), Gender.MEN),
        ("Women deo", "DEO", (), Gender.NOT_MEN),
        ("Mentol bombone", "Slatkiši", (), Gender.NOT_MEN),
        ("HUMAN hair serum", "Njega kose", (), Gender.NOT_MEN),
    ])
    def test_classify(self, name, category, tags, expected):
        assert classify(cos(name, category, tags)) is expected

    def test_rules_only_apply_to_mens_only_stores(self):
        voli = make_offer(store="Voli", name="Ženski šampon")
        kept = apply_store_rules([voli, cos("NIVEA men"), cos("Lak za nokte")])
        assert [o.name for o in kept] == ["Ženski šampon", "NIVEA men"]

    def test_unclear_needs_gemini_and_unknown_answers_are_dropped(self):
        unclear = [cos("MONTALE EDP", "Parfemi", pid="a"), cos("X EDT", "Parfemi", pid="b")]
        assert apply_store_rules(unclear) == []

        class G:
            def generate_json(self, prompt, schema):
                assert "MONTALE" in prompt
                return [{"i": 0, "mens_only": True}, {"i": 1, "mens_only": False}, {"i": 7, "mens_only": True}, "junk"]
        assert [o.product_id for o in apply_store_rules(unclear, G())] == ["a"]

    def test_gemini_failure_drops_unclear(self):
        class Broken:
            def generate_json(self, prompt, schema):
                raise GeminiError("quota")
        assert apply_store_rules([cos("MONTALE EDP", "Parfemi")], Broken()) == []


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else "err"

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


def ok(payload):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


class TestGemini:
    def test_sends_key_in_header_and_parses_json(self):
        session = FakeSession([ok([{"a": 1}])])
        g = Gemini("secret", session=session, sleep=lambda s: None)
        assert g.generate_json("p", {"type": "ARRAY"}) == [{"a": 1}]
        url, kw = session.calls[0]
        assert "secret" not in url and kw["headers"] == {"x-goog-api-key": "secret"}
        assert kw["json"]["generationConfig"]["responseMimeType"] == "application/json"
        assert "gemini-flash-lite-latest" in url

    def test_retries_on_429(self):
        slept = []
        session = FakeSession([FakeResp(429, headers={"Retry-After": "3"}), ok({"x": 1})])
        assert Gemini("k", session=session, sleep=slept.append).generate_json("p", {}) == {"x": 1}
        assert 3.0 in slept

    def test_error_does_not_leak_key(self):
        session = FakeSession([FakeResp(400, {"error": "bad model"})])
        with pytest.raises(GeminiError, match="400") as info:
            Gemini("secret", session=session, sleep=lambda s: None).generate_json("p", {})
        assert "secret" not in str(info.value)

    def test_invalid_json_answer(self):
        session = FakeSession([FakeResp(200, {"candidates": [{"content": {"parts": [{"text": "nije json"}]}}]})])
        with pytest.raises(GeminiError):
            Gemini("k", session=session, sleep=lambda s: None).generate_json("p", {})

    def test_requires_key(self):
        with pytest.raises(GeminiError):
            Gemini("")


class TestHistory:
    def test_new_and_suspicious_flags(self, tmp_path):
        db = tmp_path / "h.sqlite"
        a = make_offer(product_id="a", regular_price=Decimal("10"), sale_price=Decimal("7"))
        with PriceHistory(db) as h:
            assert h.flags([a], date(2026, 11, 2)) == {a.key: Flags()}  # first run: nothing is "new"
            h.record([a], date(2026, 11, 2))

        b = make_offer(product_id="b")
        a_inflated = make_offer(product_id="a", regular_price=Decimal("12"), sale_price=Decimal("7"))
        with PriceHistory(db) as h:
            flags = h.flags([a_inflated, b], date(2026, 11, 5))
            assert flags[b.key].is_new and not flags[b.key].suspicious
            assert not flags[a.key].is_new
            assert flags[a.key].suspicious and flags[a.key].lowest_regular == Decimal("10")
            h.record([a_inflated, b], date(2026, 11, 5))

    def test_unchanged_price_extends_row(self, tmp_path):
        db = tmp_path / "h.sqlite"
        a = make_offer(product_id="a")
        with PriceHistory(db) as h:
            for day in (2, 5, 9):
                h.record([a], date(2026, 11, day))
            rows = h._db.execute("SELECT first_seen, last_seen FROM prices").fetchall()
        assert rows == [("2026-11-02", "2026-11-09")]

    def test_product_gone_for_a_run_is_new_again(self, tmp_path):
        a = make_offer(product_id="a")
        with PriceHistory(tmp_path / "h.sqlite") as h:
            h.record([a], date(2026, 11, 2))
            h.record([], date(2026, 11, 5), stores=["Voli"])  # Voli worked, product was gone
            assert h.flags([a], date(2026, 11, 9))[a.key].is_new

    def test_failed_store_does_not_make_its_products_new(self, tmp_path):
        voli, cos = make_offer(product_id="a"), make_offer(store="Cosmetics", product_id="c")
        with PriceHistory(tmp_path / "h.sqlite") as h:
            h.record([voli, cos], date(2026, 11, 2))
            h.record([cos], date(2026, 11, 5), stores=["Cosmetics"])  # Voli failed on 5.11.
            flags = h.flags([voli, cos], date(2026, 11, 9))
            assert not flags[voli.key].is_new and not flags[cos.key].is_new
            assert h.previous_run(date(2026, 11, 9), "Voli") == date(2026, 11, 2)
            assert h.previous_run(date(2026, 11, 9), "Cosmetics") == date(2026, 11, 5)
            assert h.last_count("Voli", date(2026, 11, 9)) == 1 and h.last_count("Idea", date(2026, 11, 9)) is None

    def test_old_database_without_store_runs_still_works(self, tmp_path):
        a = make_offer(product_id="a")
        with PriceHistory(tmp_path / "h.sqlite") as h:
            h.record([a], date(2026, 11, 2))
            h._db.execute("DELETE FROM store_runs")  # as written by the previous version
            h._db.commit()
            assert h.previous_run(date(2026, 11, 5), "Voli") == date(2026, 11, 2)
            assert not h.flags([a], date(2026, 11, 5))[a.key].is_new


class TestSiteData:
    def test_structure_and_rounding(self, tmp_path):
        offers = [
            make_offer(product_id="1", name="B", regular_price=Decimal("0.75"), sale_price=Decimal("0.49"), url="https://x/1",
                       valid_to=date(2026, 11, 30), card_required=True),
            make_offer(product_id="2", name="A", sale_price=Decimal("5"), url="javascript:alert(1)"),
        ]
        flags = {offers[1].key: Flags(is_new=True, suspicious=True)}
        data = build_site_data(offers, ["Idea: greška"], THIRTY, datetime(2026, 11, 8, 7, 17), flags)
        assert data["generated_at"] == "2026-11-08T07:17" and data["min_discount"] == 30.0 and data["errors"] == ["Idea: greška"]
        first, second = data["offers"]
        assert first["name"] == "A" and first["discount_pct"] == 50.0 and first["url"] is None
        assert first["is_new"] and first["suspicious"]
        assert second == {
            "store": "Voli", "product_id": "1", "name": "B", "group": "Ostalo", "category": "Kategorija", "regular_price": 0.75, "sale_price": 0.49,
            "discount_pct": 34.7, "valid_to": "2026-11-30", "unit": None, "url": "https://x/1",
            "card_required": True, "is_new": False, "suspicious": False,
        }
        assert first["product_id"] == "2"
        path = write_site_data(tmp_path / "site" / "akcije.json", data)
        assert json.loads(path.read_text(encoding="utf-8")) == data


class TestShortEmail:
    def offers(self):
        return [make_offer(store=s, product_id=f"{s}{i}", name=f"{s} {i}", sale_price=Decimal(str(7 - i * 0.5)))
                for s in ("Voli", "Cosmetics") for i in range(4)]

    def test_summary_has_counts_top5_and_link(self):
        html = build_report(self.offers(), [], TODAY, THIRTY, site_url="https://akcije.vercel.app").html
        assert "Voli 4 · Cosmetics 4" in html
        assert "Top 5 po popustu" in html and html.count("<tr>") == 5
        assert 'href="https://akcije.vercel.app"' in html and "Otvori sve akcije" in html

    def test_non_https_site_url_falls_back_to_full_table(self):
        html = build_report(self.offers(), [], TODAY, THIRTY, site_url="javascript:alert(1)").html
        assert "javascript:" not in html and html.count("<tr>") == 8

    def test_escaping_in_summary(self):
        evil = make_offer(store="<b>S</b>", name="<script>x</script>")
        html = build_report([evil], ["<img src=x>"], TODAY, THIRTY, site_url="https://a.b").html
        assert "<script>" not in html and "<b>S</b>" not in html and "<img src=x>" not in html

    def test_new_and_suspicious_markers_and_only_new(self):
        a, b = make_offer(product_id="a", name="Novo"), make_offer(product_id="b", name="Staro")
        flags = {a.key: Flags(is_new=True, suspicious=True)}
        report = build_report([a, b], [], TODAY, THIRTY, flags=flags, only_new=True)
        assert "🆕 Novo" in report.html and "⚠️" in report.html and "Staro" not in report.html
        assert report.subject == "Akcije ≥30%: 2 proizvoda (8.11.), 1 novih"


class TestMainIntegration:
    @pytest.fixture(autouse=True)
    def isolated_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for var in ("RESEND_API_KEY", "REPORT_TO", "MIN_DISCOUNT", "GEMINI_API_KEY", "SITE_URL", "ONLY_NEW"):
            monkeypatch.delenv(var, raising=False)

    def test_dry_run_writes_json_applies_mens_rule_and_notes_missing_gemini(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app, "ADAPTERS", {
            "Voli": lambda c: [make_offer(name="Hljeb", sale_price=Decimal("5"))],
            "Cosmetics": lambda c: [cos("NIVEA men deo", pid="m"), cos("Lak za nokte", pid="w")],
        })
        assert app.main(["--dry-run", "--output", "r.html"], client=object()) == 0
        data = json.loads((tmp_path / "site" / "akcije.json").read_text(encoding="utf-8"))
        assert sorted(o["name"] for o in data["offers"]) == ["Hljeb", "NIVEA men deo"]
        assert any("GEMINI_API_KEY" in e for e in data["errors"])

    def test_dry_run_does_not_record_history(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [make_offer(sale_price=Decimal("5"))]})
        app.main(["--dry-run", "--output", "r.html"], client=object())
        with PriceHistory(tmp_path / "data" / "istorija.sqlite") as h:
            assert h._db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0

    def test_real_run_records_history_and_sends_short_email(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RESEND_API_KEY", "re_key")
        monkeypatch.setenv("REPORT_TO", "me@example.com")
        monkeypatch.setenv("SITE_URL", "https://akcije.example.com")
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [make_offer(sale_price=Decimal("5"))]})
        sent = {}
        monkeypatch.setattr(app, "send_email", lambda k, to, s, h: sent.update(html=h) or "id")
        assert app.main([], client=object()) == 0
        assert "https://akcije.example.com" in sent["html"]
        with PriceHistory(tmp_path / "data" / "istorija.sqlite") as h:
            assert h._db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 1


class TestCategories:
    @pytest.mark.parametrize("category,tags,name,expected", [
        ("Tuna", (), "Tuna u ulju", "Meso i riba"),
        ("Smrznuta piletina", (), "x", "Smrznuto"),
        ("Baby kozmetika", (), "x", "Bebe i djeca"),
        ("Mliječni deserti", (), "x", "Mliječni proizvodi i jaja"),
        ("Hladna kafa", (), "x", "Piće"),
        ("Čokolada za kuvanje", (), "x", "Slatkiši i grickalice"),
        ("DEO", (), "x", "Lična higijena"),
        ("Pranje veša", (), "x", "Domaćinstvo i čišćenje"),
        ("Higijena odjeće", ("Sve za domaćinstvo",), "x", "Domaćinstvo i čišćenje"),
        ("Muški veš", ("Tekstil",), "x", "Odjeća i ostalo"),
        ("Čarape i hulahop", ("Čarape, sokne i nazuvci muški",), "x", "Odjeća i ostalo"),
        ("Parfimisani proizvodi za muškarce", (), "x", "Parfemi"),
        ("Proizvodi za njegu lica", (), "x", "Kozmetika i njega"),
        ("Muška njega", ("Sredstva za brijanje",), "Gillette", "Lična higijena"),
        ("Meso i riba", (), "Kulen", "Meso i riba"),  # Idea: Gemini already picked a group
        ("Domaćinstvo i čišćenje", (), "MUŠKI BRIJAČ AlphaTrim", "Lična higijena"),  # name beats Gemini
        ("Nepoznato", (), "Čajna kobasica", "Meso i riba"),
        ("Nepoznato", (), "Nešto treće", "Ostalo"),
    ])
    def test_group_of(self, category, tags, name, expected):
        from akcije.categories import group_of
        assert group_of(make_offer(category=category, tags=tags, name=name)) == expected


class TestSitePage:
    def test_page_never_uses_html_injection_apis(self):
        from pathlib import Path
        page = (Path(__file__).parent.parent / "site" / "index.html").read_text(encoding="utf-8")
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert api not in page, f"{api} je zabranjen u site/index.html (podaci samo preko textContent)"
