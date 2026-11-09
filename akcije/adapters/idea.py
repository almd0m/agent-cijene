"""Idea catalogs (idea.co.me/Katalozi -> iPaper viewer on catalog.idea.co.me).

Verified Nov 2026:
- /Katalozi lists active catalogs as /Katalozi/<Naziv>-<dan>-<DD>.<mjesec>-<YYYY>.-HH-MM-SS-<dan>-<DD>.<mjesec>-<YYYY>.-HH-MM-SS
  with Croatian month names (listopad=10, studeni=11...).
- Each catalog page links to https://catalog.idea.co.me/<godina>/<mjesec>/<slug>/?page=1
- The iPaper HTML contains "pageTexts":[...] with the text of every page. The text is jumbled
  (e.g. "-39% cijena uz 1 99 super karticu 3,25 EUR MASLAC Moja Kravica 125g" = 1,99 € instead of 3,25 €),
  so Gemini pairs names and prices. Every pair is then checked against the raw text and the
  printed percentage, and anything that does not add up is dropped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from akcije.categories import GROUPS, OTHER
from akcije.http import HttpClient
from akcije.models import AdapterError, Offer

log = logging.getLogger(__name__)

STORE = "Idea"
BASE_URL = "https://www.idea.co.me"
CATALOGS_URL = f"{BASE_URL}/Katalozi"
PAGES_PER_REQUEST = 12
MAX_CATALOGS = 8
PCT_TOLERANCE = Decimal("2")  # printed % is rounded by the store

MONTHS = {
    "sijecanj": 1, "siječanj": 1, "veljaca": 2, "veljača": 2, "ozujak": 3, "ožujak": 3, "travanj": 4,
    "svibanj": 5, "lipanj": 6, "srpanj": 7, "kolovoz": 8, "rujan": 9, "listopad": 10, "studeni": 11, "prosinac": 12,
}
_CATALOG_LINK_RE = re.compile(r'href="(/Katalozi/[^"#?]+)"')
_DATE_RE = re.compile(r"(\d{1,2})\.([a-zčćšžđ]+)-(\d{4})", re.IGNORECASE)
_IPAPER_RE = re.compile(r"https://catalog\.idea\.co\.me/[^\"'\s<>?]+")
_SPLIT_DIACRITIC = re.compile(r"(?<=\w) ([čćšžđČĆŠŽĐ]) (?=\w)|(?:(?<=\s)|^)([čćšžđČĆŠŽĐ]) (?=\w)|(?<=\w) ([čćšžđČĆŠŽĐ])(?=\s|$)")


@dataclass(frozen=True)
class Catalog:
    title: str
    page_url: str
    valid_from: date | None
    valid_to: date | None


def fetch_offers(client: HttpClient, gemini=None, today: date | None = None, warnings: list[str] | None = None) -> list[Offer]:
    """`warnings` receives problems that lost part of the data (a catalog or some pages) while the rest
    was read; they are shown in the email and on the page. Losing everything raises AdapterError."""
    if gemini is None:
        raise AdapterError("Idea: preskočeno jer GEMINI_API_KEY nije postavljen.")
    today = today or date.today()
    catalogs = [c for c in parse_catalog_list(client.get_text(CATALOGS_URL)) if c.valid_to is None or c.valid_to >= today]
    if not catalogs:
        raise AdapterError("Idea: nijedan aktivan katalog nije pronađen na /Katalozi.")

    offers: dict[str, Offer] = {}
    failures: list[str] = []
    for catalog in catalogs[:MAX_CATALOGS]:
        try:
            viewer_url = find_viewer_url(client.get_text(catalog.page_url))
            texts = extract_page_texts(client.get_text(viewer_url))
            if not any(t.strip() for t in texts):
                # Some catalogs (e.g. "Specijal Italije", Nov 2026) are images only; reading them would need OCR.
                log.warning("Idea: katalog „%s“ nema teksta (samo slike), preskočen", catalog.title)
                continue
            problems: list[str] = []
            found = read_catalog(catalog, viewer_url, texts, gemini, problems)
            if problems and not found:
                raise AdapterError(f"Idea: katalog „{catalog.title}“ nije pročitan ({'; '.join(problems)})")
            if problems:
                message = f"Idea: katalog „{catalog.title}“: {', '.join(problems)} nijesu pročitane, te akcije nedostaju."
                log.warning("%s", message)
                if warnings is not None:
                    warnings.append(message)
        except AdapterError as exc:
            failures.append(str(exc))
            log.warning("%s", exc)
            continue
        except Exception as exc:  # Gemini/network problems with one catalog must not lose the others
            failures.append(f"Idea: katalog „{catalog.title}“ nije obrađen ({exc.__class__.__name__})")
            log.warning("Idea: katalog %s nije obrađen: %s", catalog.title, exc)
            continue
        for offer in found:
            current = offers.get(offer.product_id)
            if current is None or (offer.valid_to or date.max) > (current.valid_to or date.max):
                offers[offer.product_id] = offer
        log.info("Idea: katalog „%s“: %d provjerenih akcija", catalog.title, len(found))

    if not offers:
        raise AdapterError("; ".join(failures) or "Idea: iz kataloga nije pročitana nijedna akcija.")
    if warnings is not None:
        warnings.extend(failures)  # some catalogs failed while others worked
    return list(offers.values())


def parse_catalog_list(html: str) -> list[Catalog]:
    catalogs: dict[str, Catalog] = {}
    for path in _CATALOG_LINK_RE.findall(html):
        slug = path.rsplit("/", 1)[-1]
        dates = [_slug_date(*m) for m in _DATE_RE.findall(slug)]
        title = slug.split("-")[0] if not dates else re.split(r"-(?:ponedjeljak|utorak|srijeda|cetvrtak|četvrtak|petak|subota|nedjelja)-", slug, maxsplit=1)[0]
        catalogs[path] = Catalog(
            title=title.replace("-", " "),
            page_url=BASE_URL + path,
            valid_from=dates[0] if dates else None,
            valid_to=dates[1] if len(dates) > 1 else None,
        )
    return list(catalogs.values())


def _slug_date(day: str, month: str, year: str) -> date | None:
    number = MONTHS.get(month.lower())
    try:
        return date(int(year), number, int(day)) if number else None
    except ValueError:
        return None


def find_viewer_url(html: str) -> str:
    match = _IPAPER_RE.search(html)
    if not match:
        raise AdapterError("Idea: link na iPaper katalog nije pronađen.")
    return match.group(0).rstrip("/") + "/"


def extract_page_texts(html: str) -> list[str]:
    marker = '"pageTexts":'
    start = html.find(marker)
    if start < 0:
        raise AdapterError("Idea: iPaper stranica nema pageTexts (promijenjen format).")
    try:
        texts, _ = json.JSONDecoder().raw_decode(html, start + len(marker))
    except ValueError:
        raise AdapterError("Idea: pageTexts nije ispravan JSON.") from None
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise AdapterError("Idea: pageTexts ima neočekivan format.")
    return [fix_diacritics(t) for t in texts]


def fix_diacritics(text: str) -> str:
    """iPaper splits letters with diacritics into separate tokens: "Č OKOLADA", "VO Ć NI", "vi š e"."""
    return _SPLIT_DIACRITIC.sub(lambda m: m.group(1) or m.group(2) or m.group(3), text)


_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "page": {"type": "INTEGER"},
            "name": {"type": "STRING"},
            "sale_price": {"type": "NUMBER"},
            "regular_price": {"type": "NUMBER"},
            "discount_pct": {"type": "INTEGER", "nullable": True},
            "card_required": {"type": "BOOLEAN"},
            "category": {"type": "STRING", "enum": list(GROUPS)},
        },
        "required": ["page", "name", "sale_price", "regular_price", "card_required", "category"],
    },
}

_PROMPT = """Ovo je tekst iz kataloga akcija prodavnice Idea (Crna Gora). Tekst je izmiješan jer je izvučen iz PDF-a.
Pravila čitanja cijena:
- Nova (akcijska) cijena je štampana krupno kao cijeli euri i centi odvojeno, npr. "1 99" = 1.99, često uz "-39%".
- Stara (redovna) cijena je "3,25 EUR".
- "cijena uz super karticu" znači da akcijska cijena važi samo uz Super karticu -> card_required=true.
Izvuci SVE proizvode za koje sa sigurnošću znaš naziv, akcijsku i redovnu cijenu. Naziv piši kao u katalogu,
sa brendom i pakovanjem (npr. "MASLAC Moja Kravica Imlek 125g"). Ako nijesi siguran koji naziv ide uz koju cijenu, izostavi proizvod.
discount_pct je štampani procenat (bez minusa) ili null. category izaberi sa ponuđene liste.
page je broj stranice naveden u zaglavlju "=== STRANA N ===".

"""


def read_catalog(catalog: Catalog, viewer_url: str, texts: list[str], gemini, problems: list[str] | None = None) -> list[Offer]:
    """With `problems`, a failed Gemini call loses only its pages (noted in `problems`), not the whole catalog."""
    offers: list[Offer] = []
    rejected = 0
    for start in range(0, len(texts), PAGES_PER_REQUEST):
        chunk = {n + 1: texts[n] for n in range(start, min(start + PAGES_PER_REQUEST, len(texts))) if texts[n].strip()}
        if not any("EUR" in t for t in chunk.values()):
            continue
        prompt = _PROMPT + "\n".join(f"=== STRANA {n} ===\n{t}" for n, t in chunk.items())
        try:
            items = gemini.generate_json(prompt, _SCHEMA)
        except Exception as exc:
            if problems is None:
                raise
            pages = list(chunk)
            problems.append(f"strane {pages[0]}–{pages[-1]} ({exc.__class__.__name__})")
            log.warning("Idea: „%s“: strane %d–%d nijesu pročitane: %s", catalog.title, pages[0], pages[-1], exc)
            continue
        for item in items if isinstance(items, list) else []:
            offer = validate_item(item, chunk, catalog, viewer_url)
            if offer is None:
                rejected += 1
            else:
                offers.append(offer)
    if rejected:
        log.info("Idea: „%s“: odbačeno %d stavki koje nijesu prošle provjeru", catalog.title, rejected)
    return offers


def validate_item(item, chunk: dict[int, str], catalog: Catalog, viewer_url: str) -> Offer | None:
    """Accepts a Gemini item only if its prices really appear in that page's text and agree with the printed %."""
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    page = item.get("page")
    sale, regular = _money(item.get("sale_price")), _money(item.get("regular_price"))
    if not name or len(name) > 150 or sale is None or regular is None or not isinstance(page, int) or page not in chunk:
        return None
    if not (Decimal("0") < sale < regular <= Decimal("2000")):
        return None

    text = chunk[page]
    euros, cents = divmod(int(sale * 100), 100)
    if not re.search(rf"(?<![\d,]){re.escape(_fmt(regular))}(?!\d)", text):
        return None
    # Big price is printed as euros and cents in separate text boxes, sometimes with a few words or the % between:
    # "1 99", "1 -21% 49", "cijena uz 1 super karticu 89".
    gap = r"(?:\s+(?:-?\d{1,2}%|[^\d\s]+)){0,4}"
    if not re.search(rf"(?<![\d,]){euros}{gap}\s+{cents:02d}(?![\d,])|(?<![\d,]){re.escape(_fmt(sale))}(?!\d)", text):
        return None

    computed = ((regular - sale) / regular * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    printed = item.get("discount_pct")
    if isinstance(printed, int) and not isinstance(printed, bool):
        if f"{printed}%" not in text or abs(computed - printed) > PCT_TOLERANCE:
            return None
    elif computed > 80:
        return None  # no printed % to confirm a suspiciously large discount

    product_id = hashlib.sha1(f"{name.lower()}|{sale}".encode()).hexdigest()[:12]
    return Offer(
        store=STORE,
        product_id=product_id,
        name=name,
        category=item.get("category") if item.get("category") in GROUPS else OTHER,
        regular_price=regular,
        sale_price=sale,
        valid_from=catalog.valid_from,
        valid_to=catalog.valid_to,
        url=f"{viewer_url}?page={page}",
        card_required=item.get("card_required") is True,
    )


def _money(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")
