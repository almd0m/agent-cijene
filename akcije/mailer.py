from __future__ import annotations

import requests

RESEND_URL = "https://api.resend.com/emails"
# Without a verified domain, Resend only delivers mail sent from this address to the account owner's email.
DEFAULT_SENDER = "Akcije agent <onboarding@resend.dev>"


class MailError(RuntimeError):
    pass


def send_email(
    api_key: str,
    to: str,
    subject: str,
    html: str,
    sender: str = DEFAULT_SENDER,
    session: requests.Session | None = None,
    timeout_seconds: float = 20.0,
) -> str:
    if not api_key:
        raise MailError("RESEND_API_KEY nije postavljen.")
    if not to or "@" not in to:
        raise MailError("REPORT_TO nije ispravna email adresa.")

    http = session or requests.Session()
    try:
        response = http.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": sender, "to": [to], "subject": subject, "html": html},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise MailError(f"Slanje emaila nije uspjelo: {exc.__class__.__name__}") from exc

    if not 200 <= response.status_code < 300:
        # Resend error bodies describe the problem (e.g. wrong recipient) and never echo the API key.
        raise MailError(f"Resend je vratio {response.status_code}: {response.text[:300]}")
    return str(response.json().get("id", ""))
