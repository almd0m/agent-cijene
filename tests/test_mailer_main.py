from decimal import Decimal

import pytest
import requests

import akcije.__main__ as app
from akcije.mailer import DEFAULT_SENDER, RESEND_URL, MailError, send_email
from akcije.models import AdapterError
from tests.conftest import make_offer


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class TestMailer:
    def test_sends_expected_payload(self):
        session = FakeSession(FakeResponse(200, {"id": "abc"}))
        assert send_email("re_key", "me@example.com", "Naslov", "<p>x</p>", session=session) == "abc"
        url, kwargs = session.calls[0]
        assert url == RESEND_URL
        assert kwargs["headers"] == {"Authorization": "Bearer re_key"}
        assert kwargs["json"] == {"from": DEFAULT_SENDER, "to": ["me@example.com"], "subject": "Naslov", "html": "<p>x</p>"}
        assert kwargs["timeout"] > 0

    def test_error_status_raises_without_leaking_key(self):
        session = FakeSession(FakeResponse(403, text="You can only send testing emails to your own email address"))
        with pytest.raises(MailError, match="403") as info:
            send_email("re_secret", "other@example.com", "s", "h", session=session)
        assert "re_secret" not in str(info.value)

    def test_network_error_is_wrapped(self):
        session = FakeSession(error=requests.ConnectionError("dns"))
        with pytest.raises(MailError, match="ConnectionError"):
            send_email("re_key", "me@example.com", "s", "h", session=session)

    @pytest.mark.parametrize("key,to", [("", "me@example.com"), ("re_key", ""), ("re_key", "nije-adresa")])
    def test_missing_config_fails_before_network(self, key, to):
        session = FakeSession(FakeResponse(200))
        with pytest.raises(MailError):
            send_email(key, to, "s", "h", session=session)
        assert session.calls == []


class TestMain:
    @pytest.fixture(autouse=True)
    def isolated_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # no real .env is picked up
        for var in ("RESEND_API_KEY", "REPORT_TO", "MIN_DISCOUNT", "GEMINI_API_KEY", "SITE_URL", "ONLY_NEW"):
            monkeypatch.delenv(var, raising=False)

    def test_dry_run_writes_report_and_does_not_send(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda client: [make_offer(name="Jeftino", sale_price=Decimal("5"))]})
        monkeypatch.setattr(app, "send_email", lambda *a, **k: pytest.fail("email ne smije biti poslat"))
        out = tmp_path / "r.html"
        assert app.main(["--dry-run", "--output", str(out)], client=object()) == 0
        assert "Jeftino" in out.read_text(encoding="utf-8")

    def test_one_failing_adapter_does_not_stop_others(self, monkeypatch, tmp_path):
        def broken(client):
            raise AdapterError("Idea: format promijenjen")

        def crashing(client):
            raise KeyError("bug")

        monkeypatch.setattr(app, "ADAPTERS", {
            "Idea": broken, "Bug": crashing, "Voli": lambda c: [make_offer(name="Radi", sale_price=Decimal("5"))],
        })
        out = tmp_path / "r.html"
        assert app.main(["--dry-run", "--output", str(out)], client=object()) == 0
        html = out.read_text(encoding="utf-8")
        assert "Radi" in html and "Idea: format promijenjen" in html and "Bug: neočekivana greška (KeyError)" in html

    def test_all_adapters_failing_returns_error_code(self, monkeypatch, tmp_path):
        def broken(client):
            raise AdapterError("Voli: nedostupan")

        monkeypatch.setattr(app, "ADAPTERS", {"Voli": broken})
        assert app.main(["--dry-run", "--output", str(tmp_path / "r.html")], client=object()) == 1

    def test_sends_email_with_env_config(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_key")
        monkeypatch.setenv("REPORT_TO", "me@example.com")
        monkeypatch.setenv("MIN_DISCOUNT", "50")
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: [
            make_offer(product_id="1", name="Pola", sale_price=Decimal("5")),
            make_offer(product_id="2", name="Trideset", sale_price=Decimal("7")),
        ]})
        sent = {}

        def fake_send(key, to, subject, html):
            sent.update(key=key, to=to, subject=subject, html=html)
            return "id1"

        monkeypatch.setattr(app, "send_email", fake_send)
        assert app.main([], client=object()) == 0
        assert sent["to"] == "me@example.com" and "Pola" in sent["html"] and "Trideset" not in sent["html"]
        assert sent["subject"].startswith("Akcije ≥50%: 1 ")

    def test_mail_failure_returns_error_code(self, monkeypatch):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: []})
        assert app.main([], client=object()) == 1  # RESEND_API_KEY missing

    @pytest.mark.parametrize("bad", ["abc", "150", "-5", "NaN"])
    def test_invalid_min_discount_is_rejected(self, monkeypatch, bad):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: pytest.fail("ne smije se pokrenuti")})
        assert app.main(["--dry-run", "--min-discount", bad], client=object()) == 2

    def test_comma_decimal_threshold_is_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app, "ADAPTERS", {"Voli": lambda c: []})
        assert app.main(["--dry-run", "--min-discount", "27,5", "--output", str(tmp_path / "r.html")], client=object()) == 0
