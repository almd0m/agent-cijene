"""Run: python -m akcije [--dry-run [--cache | --refresh-cache]] [--min-discount 30] [--samo-nove] [-v]"""
from __future__ import annotations

import argparse
import functools
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from akcije.adapters import cosmetics, idea, kucahemije, voli
from akcije.cache import DEFAULT_CACHE_DIR, OfferCache, VerdictCache, format_age
from akcije.filters import select_offers
from akcije.gemini import Gemini
from akcije.gender import apply_store_rules
from akcije.history import PriceHistory
from akcije.http import HttpClient, PoliteClient
from akcije.mailer import MailError, send_email
from akcije.models import AdapterError, Offer
from akcije.report import build_report
from akcije.sitedata import CarriedOver, build_site_data, carry_over, load_previous, write_site_data

log = logging.getLogger("akcije")

LOCAL_TZ = ZoneInfo("Europe/Podgorica")
DEFAULT_MIN_DISCOUNT = Decimal("30")
DEFAULT_SITE_JSON = "site/akcije.json"
DEFAULT_HISTORY_DB = "data/istorija.sqlite"
CACHE_DIR = DEFAULT_CACHE_DIR  # relative to the current folder
# A store that suddenly delivers fewer than this share of its last successful count was probably read only
# partly (changed paging, blocked requests...). Its data is still used, but the email and the page warn.
LOW_COUNT_SHARE_PCT = 30

Adapter = Callable[[HttpClient], list[Offer]]
# Idea is added at runtime (it needs Gemini); without GEMINI_API_KEY it fails like a broken store.
ADAPTERS: dict[str, Adapter] = {
    "Voli": voli.fetch_offers,
    "Cosmetics": cosmetics.fetch_offers,
    "Kuća hemije": kucahemije.fetch_offers,
}


@dataclass
class Collected:
    offers: list[Offer] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ok: dict[str, int] = field(default_factory=dict)  # store -> number of offers it delivered
    failed: list[str] = field(default_factory=list)


def collect(adapters: dict[str, Adapter], client: HttpClient, cache: OfferCache | None = None, refresh: bool = False) -> Collected:
    """Runs every adapter. With `cache` (dev only), reuses fresh cached results unless `refresh`
    and saves new successful results; a failing adapter is never cached."""
    result = Collected()
    offers, errors = result.offers, result.errors
    for name, fetch in adapters.items():
        if cache is not None and not refresh:
            hit = cache.load(name)
            if hit is not None:
                cached, age = hit
                log.info("%s: %d ponuda iz keša (staro %s)", name, len(cached), format_age(age))
                offers.extend(cached)
                result.ok[name] = len(cached)
                continue
        try:
            fresh = fetch(client)
        except AdapterError as exc:
            log.error("%s", exc)
            errors.append(str(exc))
            result.failed.append(name)
            continue
        except Exception as exc:  # one broken store must not stop the others
            log.exception("%s: neočekivana greška", name)
            errors.append(f"{name}: neočekivana greška ({exc.__class__.__name__}). Detalji su u logu.")
            result.failed.append(name)
            continue
        if cache is not None:
            cache.save(name, fresh)
            log.info("%s: %d ponuda, sveže skinuto (sačuvano u keš)", name, len(fresh))
        else:
            log.info("%s: %d ponuda, sveže skinuto", name, len(fresh))
        offers.extend(fresh)
        result.ok[name] = len(fresh)
    return result


def main(argv: list[str] | None = None, client: HttpClient | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Only the .env in the current folder: python-dotenv would otherwise search upwards from this file,
    # which made tests pick up the real .env and send real emails.
    load_dotenv(Path.cwd() / ".env")

    use_cache = args.cache or args.refresh_cache
    if use_cache and not args.dry_run:
        log.error("--cache i --refresh-cache rade samo uz --dry-run (keš je samo za lokalni razvoj).")
        return 2

    try:
        min_discount = _min_discount(args.min_discount or os.getenv("MIN_DISCOUNT"))
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    now = datetime.now(LOCAL_TZ)
    today = now.date()
    gemini = _gemini()
    adapters = dict(ADAPTERS)
    partial_losses: list[str] = []  # stores that worked but lost part of their data
    if "Idea" not in adapters:
        adapters["Idea"] = functools.partial(idea.fetch_offers, gemini=gemini, today=today, warnings=partial_losses)

    cache = OfferCache(CACHE_DIR, now) if use_cache else None
    verdicts = VerdictCache(CACHE_DIR, now, refresh=args.refresh_cache) if use_cache else None
    got = collect(adapters, client or PoliteClient(), cache, refresh=args.refresh_cache)
    selected = apply_store_rules(select_offers(got.offers, min_discount, today), gemini, verdicts)
    if verdicts is not None:
        verdicts.save()
    log.info("Ukupno %d akcija, %d ispunjava uslove, %d grešaka", len(got.offers), len(selected), len(got.errors))

    all_failed = not got.ok
    notes: list[str] = list(partial_losses)
    carried = CarriedOver()
    if all_failed:
        notes.append("Nijedna prodavnica nije obrađena, pa stranica nije ažurirana (ostaju prethodni podaci).")
    elif got.failed:
        carried = carry_over(load_previous(args.site_json), got.failed, today, min_discount)
        for store, since in carried.stale_stores.items():
            notes.append(f"{store}: podaci nisu osvježeni, na stranici su akcije od {since.day}.{since.month}.{since.year}.")
    flags = {}
    if not args.no_history:
        with PriceHistory(args.history) as history:
            flags = history.flags(selected, today)
            notes.extend(_low_count_warnings(history, got.ok, today))
    for note in notes[len(partial_losses):]:  # adapters already logged their own
        log.warning("%s", note)

    shown_errors = got.errors + notes
    if all_failed:
        log.error("Sve prodavnice su pale: %s se ne mijenja.", args.site_json)
    else:
        write_site_data(args.site_json, build_site_data(selected, shown_errors, min_discount, now, flags, carried))
        log.info("Podaci za sajt sačuvani u %s", args.site_json)

    only_new = args.samo_nove or os.getenv("ONLY_NEW", "").lower() in ("1", "true", "da")
    report = build_report(selected, shown_errors, today, min_discount, os.getenv("SITE_URL") or None, flags, only_new)

    if args.dry_run:
        out = Path(args.output)
        out.write_text(report.html, encoding="utf-8")
        log.info("Dry run: izvještaj sačuvan u %s (email nije poslat, istorija nije upisana). Naslov: %s", out.resolve(), report.subject)
        return 1 if all_failed else 0

    try:
        email_id = send_email(os.getenv("RESEND_API_KEY", ""), os.getenv("REPORT_TO", ""), report.subject, report.html)
    except MailError as exc:
        # History is not written either, so these offers are still "new" in the next email.
        log.error("%s", exc)
        return 1
    log.info("Email poslat (id %s)", email_id)

    if not args.no_history and not all_failed:
        with PriceHistory(args.history) as history:
            # all offers (not only selected) for better price history; only stores that delivered data count as a run
            history.record(got.offers, today, stores=got.ok)
    return 1 if all_failed else 0


def _low_count_warnings(history: PriceHistory, counts: dict[str, int], today) -> list[str]:
    warnings = []
    for store, count in counts.items():
        last = history.last_count(store, today)
        if last and count * 100 < last * LOW_COUNT_SHARE_PCT:
            warnings.append(f"{store}: pročitano samo {count} proizvoda, a prošli put {last}. Moguće je da sajt nije pročitan do kraja.")
    return warnings


def _gemini() -> Gemini | None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return Gemini(key, model=os.getenv("GEMINI_MODEL") or None) if key else None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="akcije", description="Skuplja akcije iz prodavnica i šalje email izvještaj.")
    parser.add_argument("--dry-run", action="store_true", help="ne šalji email i ne upisuj istoriju, samo sačuvaj HTML izvještaj")
    parser.add_argument("--output", default="izvjestaj.html", help="fajl za --dry-run (podrazumijevano: izvjestaj.html)")
    parser.add_argument("--min-discount", help="minimalni popust u procentima (podrazumijevano: 30 ili MIN_DISCOUNT iz .env)")
    parser.add_argument("--samo-nove", action="store_true", help="u emailu prikaži samo akcije kojih nije bilo prošli put")
    parser.add_argument("--site-json", default=DEFAULT_SITE_JSON, help=f"gdje se piše JSON za sajt (podrazumijevano: {DEFAULT_SITE_JSON})")
    parser.add_argument("--history", default=DEFAULT_HISTORY_DB, help=f"SQLite istorija cijena (podrazumijevano: {DEFAULT_HISTORY_DB})")
    parser.add_argument("--no-history", action="store_true", help="ne koristi istoriju cijena")
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument("--cache", action="store_true",
                             help="samo uz --dry-run: koristi ponude iz .cache/ ako nisu starije od 12 h, inače skini i sačuvaj")
    cache_group.add_argument("--refresh-cache", action="store_true",
                             help="samo uz --dry-run: uvijek skini sveže ponude i prepiši .cache/")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _min_discount(raw: str | None) -> Decimal:
    if raw is None or raw == "":
        return DEFAULT_MIN_DISCOUNT
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"Neispravan minimalni popust: {raw!r}") from None
    if not value.is_finite() or not 0 <= value <= 100:
        raise ValueError(f"Minimalni popust mora biti između 0 i 100, a dobijeno je {raw!r}")
    return value


if __name__ == "__main__":
    sys.exit(main())
