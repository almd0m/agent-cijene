import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import akcije.__main__ as app
from akcije.cache import OfferCache, VerdictCache, format_age, slug
from akcije.gender import apply_store_rules
from akcije.history import PriceHistory
from akcije.models import AdapterError
from tests.conftest import make_offer

TZ = ZoneInfo("Europe/Podgorica")
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=TZ)


def tricky_offer(**overrides):
    values = dict(
        product_id="42", name="Čokolada „Đurđa“", regular_price=Decimal("10.00"), sale_price=Decimal("1.005"),
        valid_from=date(2026, 11, 1), valid_to=None, unit="kom", url="https://x/42", card_required=True,
        tags=("Slatkiši", "Akcija"),
    )
    values.update(overrides)
    return make_offer(**values)


class CountingAdapter:
    def __init__(self, offers=None, error: Exception | None = None):
        self.offers = offers or []
        self.error = error
        self.calls = 0

    def __call__(self, client):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.offers)


class TestOfferCache:
    def test_roundtrip_keeps_decimals_dates_and_tags(self, tmp_path):
        offers = [tricky_offer(), make_offer(product_id="2", valid_to=date(2026, 11, 30), regular_price=Decimal("3"))]
        OfferCache(tmp_path, NOW).save("Voli", offers)
        loaded, age = OfferCache(tmp_path, NOW + timedelta(hours=2, minutes=15)).load("Voli")
        assert loaded == offers
        assert str(loaded[0].sale_price) == "1.005" and str(loaded[0].regular_price) == "10.00"
        assert str(loaded[1].regular_price) == "3"
        assert isinstance(loaded[0].sale_price, Decimal) and loaded[0].valid_from == date(2026, 11, 1)
        assert loaded[0].tags == ("Slatkiši", "Akcija") and loaded[0].valid_to is None
        assert format_age(age) == "2 h 15 min"

    def test_one_ascii_file_per_store(self, tmp_path):
        cache = OfferCache(tmp_path, NOW)
        cache.save("Kuća hemije", [])
        assert cache.path("Kuća hemije") == tmp_path / "kuca-hemije.json"
        assert json.loads((tmp_path / "kuca-hemije.json").read_text(encoding="utf-8"))["created_at"] == NOW.isoformat()
        assert slug("Voli") == "voli"

    def test_missing_cache_is_none(self, tmp_path):
        assert OfferCache(tmp_path, NOW).load("Voli") is None

    def test_cache_older_than_12h_is_ignored(self, tmp_path):
        OfferCache(tmp_path, NOW).save("Voli", [make_offer()])
        assert OfferCache(tmp_path, NOW + timedelta(hours=12)).load("Voli") is not None
        assert OfferCache(tmp_path, NOW + timedelta(hours=12, seconds=1)).load("Voli") is None

    @pytest.mark.parametrize("content", [
        "{ne valja",
        "[]",
        '{"version": 1, "store": "Voli", "created_at": "2026-11-08T09:00:00+01:00", "offers": [{"store": "Voli"}]}',
        '{"version": 1, "store": "Voli", "created_at": "2026-11-08T09:00:00+01:00", "offers": [{"store": "Voli", '
        '"product_id": "1", "name": "x", "category": "y", "regular_price": 10.5, "sale_price": "5"}]}',
        '{"version": 1, "store": "Voli", "created_at": "juče", "offers": []}',
        '{"version": 1, "store": "Voli", "created_at": "2026-11-09T09:00:00+01:00", "offers": []}',
    ])
    def test_broken_cache_is_ignored_with_warning(self, tmp_path, caplog, content):
        (tmp_path / "voli.json").write_text(content, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert OfferCache(tmp_path, NOW).load("Voli") is None
        assert "oštećen" in caplog.text


class TestCollectWithCache:
    def test_second_run_reads_cache_instead_of_adapter(self, tmp_path, caplog):
        adapter = CountingAdapter([tricky_offer()])
        app.collect({"Voli": adapter}, object(), OfferCache(tmp_path, NOW))
        caplog.clear()
        with caplog.at_level(logging.INFO):
            got = app.collect({"Voli": adapter}, object(), OfferCache(tmp_path, NOW + timedelta(minutes=30)))
        offers, errors = got.offers, got.errors
        assert adapter.calls == 1 and errors == [] and offers == [tricky_offer()]
        assert "Voli: 1 ponuda iz keša (staro 30 min)" in caplog.text

    def test_fresh_fetch_is_logged(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO):
            app.collect({"Voli": CountingAdapter([make_offer()])}, object(), OfferCache(tmp_path, NOW))
        assert "Voli: 1 ponuda, sveže skinuto (sačuvano u keš)" in caplog.text

    def test_expired_cache_calls_adapter_again(self, tmp_path):
        adapter = CountingAdapter([make_offer()])
        app.collect({"Voli": adapter}, object(), OfferCache(tmp_path, NOW))
        app.collect({"Voli": adapter}, object(), OfferCache(tmp_path, NOW + timedelta(hours=13)))
        assert adapter.calls == 2

    def test_broken_cache_does_not_crash_and_is_replaced(self, tmp_path):
        (tmp_path / "voli.json").write_text("{pokvaren", encoding="utf-8")
        adapter = CountingAdapter([make_offer(name="Sveže")])
        got = app.collect({"Voli": adapter}, object(), OfferCache(tmp_path, NOW))
        offers, errors = got.offers, got.errors
        assert adapter.calls == 1 and errors == [] and offers[0].name == "Sveže"
        assert OfferCache(tmp_path, NOW).load("Voli")[0][0].name == "Sveže"

    @pytest.mark.parametrize("error", [AdapterError("Voli: nedostupan"), KeyError("bug")])
    def test_adapter_error_is_not_cached(self, tmp_path, error):
        cache = OfferCache(tmp_path, NOW)
        got = app.collect({"Voli": CountingAdapter(error=error)}, object(), cache)
        offers, errors = got.offers, got.errors
        assert offers == [] and len(errors) == 1
        assert not cache.path("Voli").exists()
        # the next run tries again
        adapter = CountingAdapter([make_offer()])
        app.collect({"Voli": adapter}, object(), cache)
        assert adapter.calls == 1

    def test_adapter_error_keeps_previous_cache(self, tmp_path):
        OfferCache(tmp_path, NOW).save("Voli", [make_offer(name="Staro")])
        before = (tmp_path / "voli.json").read_bytes()
        later = OfferCache(tmp_path, NOW + timedelta(hours=1))
        app.collect({"Voli": CountingAdapter(error=AdapterError("Voli: pao"))}, object(), later, refresh=True)
        assert (tmp_path / "voli.json").read_bytes() == before

    def test_refresh_ignores_valid_cache_and_overwrites_it(self, tmp_path):
        OfferCache(tmp_path, NOW).save("Voli", [make_offer(name="Staro")])
        adapter = CountingAdapter([make_offer(name="Novo")])
        offers = app.collect({"Voli": adapter}, object(), OfferCache(tmp_path, NOW), refresh=True).offers
        assert adapter.calls == 1 and offers[0].name == "Novo"
        assert OfferCache(tmp_path, NOW).load("Voli")[0][0].name == "Novo"


class FakeGemini:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def generate_json(self, prompt, schema):
        self.calls += 1
        if self.fail:
            raise RuntimeError("kvota")
        return [{"i": 0, "mens_only": True}, {"i": 1, "mens_only": False}]


class TestVerdictCache:
    def unclear(self):
        return [make_offer(store="Cosmetics", product_id="a", name="MONTALE EDP", category="Parfemi"),
                make_offer(store="Cosmetics", product_id="b", name="LATTAFA EDP", category="Parfemi")]

    def test_gemini_verdicts_are_reused(self, tmp_path):
        gemini = FakeGemini()
        first = VerdictCache(tmp_path, NOW)
        assert [o.product_id for o in apply_store_rules(self.unclear(), gemini, first)] == ["a"]
        first.save()
        second = VerdictCache(tmp_path, NOW + timedelta(hours=1))
        assert [o.product_id for o in apply_store_rules(self.unclear(), gemini, second)] == ["a"]
        assert gemini.calls == 1

    def test_expired_or_refreshed_verdicts_ask_again(self, tmp_path):
        gemini = FakeGemini()
        cache = VerdictCache(tmp_path, NOW)
        apply_store_rules(self.unclear(), gemini, cache)
        cache.save()
        apply_store_rules(self.unclear(), gemini, VerdictCache(tmp_path, NOW + timedelta(hours=13)))
        apply_store_rules(self.unclear(), gemini, VerdictCache(tmp_path, NOW, refresh=True))
        assert gemini.calls == 3

    def test_failed_gemini_answer_is_not_cached(self, tmp_path):
        cache = VerdictCache(tmp_path, NOW)
        assert apply_store_rules(self.unclear(), FakeGemini(fail=True), cache) == []
        cache.save()
        assert not (tmp_path / "muski-filter.json").exists()
        gemini = FakeGemini()
        assert [o.product_id for o in apply_store_rules(self.unclear(), gemini, VerdictCache(tmp_path, NOW))] == ["a"]
        assert gemini.calls == 1

    def test_broken_verdict_file_is_ignored_with_warning(self, tmp_path, caplog):
        (tmp_path / "muski-filter.json").write_text("nije json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            cache = VerdictCache(tmp_path, NOW)
        assert cache.get(self.unclear()[0]) is None and "oštećen" in caplog.text


class TestMainWithCache:
    @pytest.fixture(autouse=True)
    def isolated_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # no real .env, .cache/ lands in tmp_path
        for var in ("RESEND_API_KEY", "REPORT_TO", "MIN_DISCOUNT", "GEMINI_API_KEY", "SITE_URL", "ONLY_NEW"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(app, "send_email", lambda *a, **k: pytest.fail("email ne smije biti poslat"))

    @pytest.mark.parametrize("flag", ["--cache", "--refresh-cache"])
    def test_cache_without_dry_run_is_rejected(self, monkeypatch, tmp_path, caplog, flag):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: pytest.fail("ne smije se pokrenuti")})
        assert app.main([flag, "--site-json", str(tmp_path / "s.json")], client=object()) == 2
        assert "samo uz --dry-run" in caplog.text
        assert not (tmp_path / ".cache").exists()

    def test_cache_and_refresh_together_are_rejected(self):
        with pytest.raises(SystemExit):
            app.main(["--dry-run", "--cache", "--refresh-cache"], client=object())

    def test_dry_run_with_cache_uses_cache_on_second_run(self, monkeypatch, tmp_path):
        adapter = CountingAdapter([make_offer(name="Keširano", sale_price=Decimal("5"))])
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": adapter})
        args = ["--dry-run", "--cache", "--no-history", "--output", str(tmp_path / "r.html"), "--site-json", str(tmp_path / "s.json")]
        assert app.main(args, client=object()) == 0
        assert app.main(args, client=object()) == 0
        assert adapter.calls == 1
        assert (tmp_path / ".cache" / "voli.json").exists()
        assert "Keširano" in (tmp_path / "r.html").read_text(encoding="utf-8")

    def test_only_new_state_is_untouched(self, monkeypatch, tmp_path):
        db = tmp_path / "istorija.sqlite"
        with PriceHistory(db) as history:
            history.record([make_offer(product_id="staro")], date(2026, 11, 1))
        before = db.read_bytes()
        monkeypatch.setenv("ONLY_NEW", "1")
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": CountingAdapter([make_offer(product_id="novo", sale_price=Decimal("5"))])})
        for flag in ("--refresh-cache", "--cache"):
            args = ["--dry-run", flag, "--history", str(db), "--output", str(tmp_path / "r.html"), "--site-json", str(tmp_path / "s.json")]
            assert app.main(args, client=object()) == 0
        assert db.read_bytes() == before
        with PriceHistory(db) as history:
            assert history.previous_run(date(2026, 12, 1)) == date(2026, 11, 1)
