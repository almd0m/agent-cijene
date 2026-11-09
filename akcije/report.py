from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape

from akcije.categories import GROUPS, group_of
from akcije.history import Flags
from akcije.models import Offer

TOP_N = 5
STORE_ORDER = ("Voli", "Idea", "Cosmetics", "Kuća hemije")


@dataclass(frozen=True)
class Report:
    subject: str
    html: str


def build_report(
    offers: Sequence[Offer],
    errors: Sequence[str],
    today: date,
    min_discount_pct: Decimal,
    site_url: str | None = None,
    flags: Mapping[tuple[str, str], Flags] | None = None,
    only_new: bool = False,
) -> Report:
    """Short summary + link when the web page exists (site_url), otherwise the full table."""
    flags = flags or {}
    threshold = _number(min_discount_pct)
    new_count = sum(1 for o in offers if flags.get(o.key, Flags()).is_new)
    subject = f"Akcije ≥{threshold}%: {len(offers)} proizvoda ({today.day}.{today.month}.)"
    if new_count:
        subject += f", {new_count} novih"
    if errors:
        subject += " ⚠️"

    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:760px">',
        f'<h2 style="margin:0 0 4px">{len(offers)} akcija ≥{threshold}%</h2>',
        f'<p style="margin:0 0 16px;color:#666">Izvještaj za {today.day}.{today.month}.{today.year}. · {_store_counts(offers)}</p>',
    ]
    if errors:
        items = "".join(f"<li>{escape(e)}</li>" for e in errors)
        parts.append(
            '<div style="background:#fff4e5;border:1px solid #f0b46a;padding:8px 12px;margin-bottom:16px">'
            f"<strong>Upozorenja:</strong><ul style=\"margin:4px 0 0\">{items}</ul></div>"
        )
    if not offers:
        parts.append("<p>Nema proizvoda koji ispunjavaju uslove.</p>")

    link = site_url if site_url and site_url.startswith("https://") else None
    shown = [o for o in offers if flags.get(o.key, Flags()).is_new] if only_new else list(offers)
    if only_new:
        parts.append(f'<p style="margin:0 0 12px">Prikazane su samo <strong>nove</strong> akcije ({len(shown)}).</p>')

    if link:
        top = sorted(shown, key=lambda o: (-o.discount_pct, o.name.lower()))[:TOP_N]
        if top:
            parts.append(f'<h3 style="margin:16px 0 6px">Top {len(top)} po popustu</h3>')
            parts.append(_table(top, flags, with_store=True))
        if shown:
            parts.append(f'<p style="margin:16px 0 0;color:#444"><strong>Po kategorijama:</strong> {_group_counts(shown)}</p>')
        parts.append(
            f'<p style="margin:20px 0"><a href="{escape(link, quote=True)}" style="background:#1a5fb4;color:#fff;'
            'padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:bold">Otvori sve akcije →</a></p>'
        )
    else:
        for category, items in _group_by_category(shown):
            parts.append(f'<h3 style="margin:20px 0 6px;border-bottom:2px solid #eee">{escape(category)} ({len(items)})</h3>')
            parts.append(_table(items, flags, with_store=True))

    parts.append(
        '<p style="margin-top:24px;color:#999;font-size:12px">💳 = cijena uz karticu · 🆕 = nije bilo u prošlom izvještaju · '
        "⚠️ = redovna cijena je podignuta u odnosu na ranije. Cijene su preuzete sa sajtova prodavnica; provjeri u radnji prije kupovine.</p></div>"
    )
    return Report(subject=subject, html="".join(parts))


def _store_counts(offers: Sequence[Offer]) -> str:
    counts = Counter(o.store for o in offers)
    order = [s for s in STORE_ORDER if s in counts] + sorted(s for s in counts if s not in STORE_ORDER)
    return " · ".join(f"{escape(store)} {counts[store]}" for store in order) or "nema akcija"


def _group_by_category(offers: Sequence[Offer]) -> list[tuple[str, list[Offer]]]:
    """Common groups (akcije.categories) in their fixed order, best discount first inside each group."""
    groups: dict[str, list[Offer]] = defaultdict(list)
    for offer in offers:
        groups[group_of(offer)].append(offer)
    for items in groups.values():
        items.sort(key=lambda o: (-o.discount_pct, o.name.lower()))
    order = {name: i for i, name in enumerate(GROUPS)}
    return sorted(groups.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0].lower()))


def _group_counts(offers: Sequence[Offer]) -> str:
    return " · ".join(f"{escape(name)} {len(items)}" for name, items in _group_by_category(offers))


def _table(offers: Sequence[Offer], flags: Mapping[tuple[str, str], Flags], with_store: bool) -> str:
    head = (
        '<tr style="text-align:left;color:#666;font-size:12px">'
        "<th style=\"padding:4px\">Proizvod</th>"
        + ("<th style=\"padding:4px\">Prodavnica</th>" if with_store else "")
        + "<th style=\"padding:4px\">Cijena</th><th style=\"padding:4px\">Popust</th><th style=\"padding:4px\">Važi do</th></tr>"
    )
    rows = "".join(_row(o, flags.get(o.key, Flags()), with_store) for o in offers)
    return f'<table style="border-collapse:collapse;width:100%">{head}{rows}</table>'


def _row(offer: Offer, flag: Flags, with_store: bool) -> str:
    name = escape(offer.name)
    if offer.url and offer.url.startswith("https://"):
        name = f'<a href="{escape(offer.url, quote=True)}" style="color:#1a5fb4;text-decoration:none">{name}</a>'
    if flag.is_new:
        name = "🆕 " + name
    card = " 💳" if offer.card_required else ""
    warn = " ⚠️" if flag.suspicious else ""
    unit = f" / {escape(offer.unit)}" if offer.unit else ""
    valid_to = f"{offer.valid_to.day}.{offer.valid_to.month}." if offer.valid_to else "–"
    cell = 'style="padding:6px 4px;border-bottom:1px solid #f2f2f2;vertical-align:top"'
    store = f"<td {cell}>{escape(offer.store)}</td>" if with_store else ""
    return (
        "<tr>"
        f"<td {cell}>{name}</td>"
        f"{store}"
        f'<td {cell}><strong>{_money(offer.sale_price)}</strong>{unit}{card}<br>'
        f'<span style="color:#999;text-decoration:line-through;font-size:12px">{_money(offer.regular_price)}</span></td>'
        f'<td {cell}><strong style="color:#c01c28">-{_number(offer.discount_pct)}%</strong>{warn}</td>'
        f"<td {cell}>{valid_to}</td>"
        "</tr>"
    )


def _money(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


def _number(value: Decimal) -> str:
    rounded = value.quantize(Decimal("1")) if value == value.to_integral_value() else value.quantize(Decimal("0.1"))
    return str(rounded).replace(".", ",")
