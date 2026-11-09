"""What happens when stores fail: old data is kept, the page is never emptied, history stays honest."""
import json
from datetime import date, datetime
from decimal import Decimal

import pytest

import akcije.__main__ as app
import akcije.sitedata as sitedata
from akcije.history import PriceHistory
from akcije.models import AdapterError
from akcije.sitedata import build_site_data, carry_over, load_previous, write_site_data
from tests.conftest import make_offer

TODAY = datetime.now(app.LOCAL_TZ).date()


def row(store, pid, name, discount=40.0, valid_to="2099-12-31", **extra):
    return {"store": store, "product_id": pid, "name": name, "group": "Ostalo", "category": "x", "regular_price": 10.0,
            "sale_price": round(10 - discount / 10, 2), "discount_pct": discount, "valid_to": valid_to, "unit": None,
            "url": None, "card_required": False, "is_new": True, "suspicious": False, **extra}


def previous_json(*rows, generated_at="2026-11-04T07:17+01:00"):
    return {"generated_at": generated_at, "min_discount": 30.0, "groups": [], "errors": [], "offers": list(rows)}


def broken(name):
    def fetch(client):
        raise AdapterError(f"{name}: sajt nedostupan")
    return fetch


class TestCarryOver:
    def test_keeps_only_valid_offers_of_failed_stores(self):
        prev = previous_json(
            row("Voli", "1", "Važi"), row("Voli", "2", "Istekla", valid_to="2020-01-01"), row("Voli", "3", "Bez datuma", valid_to=None),
            row("Voli", "4", "Mali popust", discount=20.0), row("Idea", "5", "Druga prodavnica"),
        )
        carried = carry_over(prev, ["Voli"], date(2026, 11, 8), Decimal("30"))
        assert [r["name"] for r in carried.rows] == ["Važi", "Bez datuma"]
        assert all(r["stale_since"] == "2026-11-04" and r["is_new"] is False for r in carried.rows)
        assert carried.stale_stores == {"Voli": date(2026, 11, 4)}

    def test_data_stale_twice_keeps_original_date(self):
        prev = previous_json(row("Voli", "1", "Stara", stale_since="2026-11-01"), generated_at="2026-11-04T07:17+01:00")
        carried = carry_over(prev, ["Voli"], date(2026, 11, 8), Decimal("30"))
        assert carried.rows[0]["stale_since"] == "2026-11-01" and carried.stale_stores == {"Voli": date(2026, 11, 1)}

    @pytest.mark.parametrize("bad", [{"stale_since": None, "name": ""}, {"sale_price": "x"}, {"discount_pct": None}])
    def test_broken_rows_are_skipped(self, bad):
        assert carry_over(previous_json({**row("Voli", "1", "X"), **bad}), ["Voli"], date(2026, 11, 8), Decimal("30")).rows == []

    @pytest.mark.parametrize("content", ["{pokvaren", "[]", '{"offers": 5}'])
    def test_broken_previous_file_is_ignored(self, tmp_path, caplog, content):
        path = tmp_path / "akcije.json"
        path.write_text(content, encoding="utf-8")
        assert load_previous(path) is None and "nije čitljiv" in caplog.text
        assert load_previous(tmp_path / "nema.json") is None

    def test_site_data_merges_and_lists_stale_stores(self):
        carried = carry_over(previous_json(row("Voli", "1", "Stara", discount=50.0)), ["Voli"], date(2026, 11, 8), Decimal("30"))
        data = build_site_data([make_offer(store="Idea", name="Nova", sale_price=Decimal("6"))], [], Decimal("30"),
                               datetime(2026, 11, 8, 7, 17), carried=carried)
        assert [o["name"] for o in data["offers"]] == ["Stara", "Nova"]  # sorted by discount
        assert data["stale_stores"] == {"Voli": "2026-11-04"}
        assert "stale_since" not in data["offers"][1]


class TestAtomicWrite:
    def test_failed_write_keeps_old_file(self, tmp_path, monkeypatch):
        target = tmp_path / "akcije.json"
        target.write_text('{"stari": true}', encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk pun")

        monkeypatch.setattr(sitedata.os, "replace", boom)
        with pytest.raises(OSError):
            write_site_data(target, {"novi": True})
        assert target.read_text(encoding="utf-8") == '{"stari": true}'
        assert not (tmp_path / "akcije.tmp").exists()


class TestMainWhenStoresFail:
    @pytest.fixture(autouse=True)
    def isolated_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for var in ("RESEND_API_KEY", "REPORT_TO", "MIN_DISCOUNT", "GEMINI_API_KEY", "SITE_URL", "ONLY_NEW"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("RESEND_API_KEY", "re_test")
        monkeypatch.setenv("REPORT_TO", "me@example.com")
        self.sent = []
        monkeypatch.setattr(app, "send_email", lambda key, to, subject, html: self.sent.append((subject, html)) or "id")
        site = tmp_path / "site"
        site.mkdir()
        self.site_json = site / "akcije.json"
        self.history = tmp_path / "data" / "istorija.sqlite"

    def write_previous(self, *rows):
        self.site_json.write_text(json.dumps(previous_json(*rows), ensure_ascii=False), encoding="utf-8")

    def site(self):
        return json.loads(self.site_json.read_text(encoding="utf-8"))

    def test_one_failed_store_keeps_its_old_offers(self, monkeypatch):
        self.write_previous(row("Voli", "1", "Stari Voli"), row("Voli", "2", "Istekao Voli", valid_to="2020-01-01"),
                            row("Idea", "9", "Stara Idea"))
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": broken("Voli"), "Idea": lambda c: [make_offer(store="Idea", product_id="7", name="Nova Idea")]})
        assert app.main([], client=object()) == 0
        data = self.site()
        names = {o["name"]: o for o in data["offers"]}
        assert set(names) == {"Stari Voli", "Nova Idea"}  # old Idea replaced by fresh Idea, expired Voli dropped
        assert names["Stari Voli"]["stale_since"] == "2026-11-04" and "stale_since" not in names["Nova Idea"]
        assert data["stale_stores"] == {"Voli": "2026-11-04"}
        assert any("Voli: podaci nisu osvježeni" in e for e in data["errors"])
        subject, html = self.sent[0]
        assert "Voli: podaci nisu osvježeni, na stranici su akcije od 4.11.2026." in html
        assert "Stari Voli" not in html  # unverified old prices are only on the page, not in the email
        assert subject.endswith("⚠️")

    def test_idea_without_gemini_key_counts_as_failed_store(self, monkeypatch):
        self.write_previous(row("Idea", "9", "Stara Idea"))
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [make_offer(name="Svjež Voli")]})
        assert app.main([], client=object()) == 0
        data = self.site()
        assert {o["name"] for o in data["offers"]} == {"Svjež Voli", "Stara Idea"}
        assert any("GEMINI_API_KEY" in e for e in data["errors"]) and data["stale_stores"] == {"Idea": "2026-11-04"}

    def test_all_stores_failed_keeps_site_and_history_and_still_mails(self, monkeypatch):
        self.write_previous(row("Voli", "1", "Stari Voli"))
        before = self.site_json.read_bytes()
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": broken("Voli")})
        assert app.main([], client=object()) == 1
        assert self.site_json.read_bytes() == before
        assert len(self.sent) == 1 and "Nijedna prodavnica nije obrađena" in self.sent[0][1]
        with PriceHistory(self.history) as h:
            assert h._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    def test_all_stores_failed_in_dry_run_also_keeps_site(self, monkeypatch, tmp_path):
        self.write_previous(row("Voli", "1", "Stari Voli"))
        before = self.site_json.read_bytes()
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": broken("Voli")})
        assert app.main(["--dry-run", "--output", str(tmp_path / "r.html")], client=object()) == 1
        assert self.site_json.read_bytes() == before

    def test_mail_failure_does_not_write_history(self, monkeypatch):
        from akcije.mailer import MailError

        def fail(*a):
            raise MailError("Resend je vratio 500")

        monkeypatch.setattr(app, "send_email", fail)
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [make_offer()], "Idea": lambda c: []})
        assert app.main([], client=object()) == 1
        with PriceHistory(self.history) as h:
            assert h._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    def test_store_recovering_after_failure_is_not_all_new(self, monkeypatch):
        days = iter([date(2026, 11, 2), date(2026, 11, 5), date(2026, 11, 9)])

        class Clock(datetime):
            current = None

            @classmethod
            def now(cls, tz=None):
                return datetime.combine(Clock.current, datetime.min.time(), tzinfo=tz)

        monkeypatch.setattr(app, "datetime", Clock)
        voli_ok = lambda c: [make_offer(product_id="v1", name="Voli proizvod")]  # noqa: E731
        idea_ok = lambda c: [make_offer(store="Idea", product_id="i1", name="Idea proizvod")]  # noqa: E731
        for adapters in ({"Voli": voli_ok, "Idea": idea_ok}, {"Voli": broken("Voli"), "Idea": idea_ok}, {"Voli": voli_ok, "Idea": idea_ok}):
            Clock.current = next(days)
            monkeypatch.setattr(app, "ADAPTERS", adapters)
            assert app.main([], client=object()) == 0
        flags = {o["name"]: o["is_new"] for o in self.site()["offers"]}
        assert flags == {"Voli proizvod": False, "Idea proizvod": False}


def test_partial_idea_loss_is_shown_in_email_and_site(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("RESEND_API_KEY", "REPORT_TO", "MIN_DISCOUNT", "GEMINI_API_KEY", "SITE_URL", "ONLY_NEW"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "_gemini", lambda: object())

    def partial_idea(client, gemini, today, warnings):
        warnings.append("Idea: katalog „X“: strane 13–24 (RuntimeError) nijesu pročitane, te akcije nedostaju.")
        return [make_offer(store="Idea", name="Pročitano")]

    monkeypatch.setattr(app.idea, "fetch_offers", partial_idea)
    monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [make_offer()]})
    assert app.main(["--dry-run", "--output", "r.html"], client=object()) == 0
    data = json.loads((tmp_path / "site" / "akcije.json").read_text(encoding="utf-8"))
    assert any("strane 13–24" in e for e in data["errors"])
    assert "strane 13–24" in (tmp_path / "r.html").read_text(encoding="utf-8")


class TestLowCountWarning:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for var in ("RESEND_API_KEY", "REPORT_TO", "MIN_DISCOUNT", "GEMINI_API_KEY", "SITE_URL", "ONLY_NEW"):
            monkeypatch.delenv(var, raising=False)
        self.db = tmp_path / "data" / "istorija.sqlite"
        with PriceHistory(self.db) as h:  # last successful Voli run delivered 100 products
            h.record([make_offer(product_id=str(i)) for i in range(100)], date(2020, 1, 1))

    def run(self, monkeypatch, tmp_path, n):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [make_offer(product_id=str(i)) for i in range(n)]})
        assert app.main(["--dry-run", "--output", "r.html"], client=object()) == 0  # data is still used
        return json.loads((tmp_path / "site" / "akcije.json").read_text(encoding="utf-8"))

    def test_big_drop_warns_in_site_and_email(self, monkeypatch, tmp_path):
        data = self.run(monkeypatch, tmp_path, 29)
        assert any("Voli: pročitano samo 29 proizvoda, a prošli put 100" in e for e in data["errors"])
        assert len(data["offers"]) == 29
        assert "pročitano samo 29" in (tmp_path / "r.html").read_text(encoding="utf-8")

    def test_exactly_threshold_does_not_warn(self, monkeypatch, tmp_path):
        assert app.LOW_COUNT_SHARE_PCT == 30
        data = self.run(monkeypatch, tmp_path, 30)
        assert not any("pročitano samo" in e for e in data["errors"])
