"""Rule "exclusively men's product" for Cosmetics and Kuća hemije.

Order of trust: product name > store category. Unisex never qualifies.
Products with no signal at all are only "unclear" when they belong to a product type that
usually comes in men's/women's versions (perfume, deodorant, shaving...). Those can be
decided by Gemini; without Gemini, or when in doubt, the product is left out.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from enum import Enum
from typing import Protocol

from akcije.models import Offer

log = logging.getLogger(__name__)

MENS_ONLY_STORES = frozenset({"Cosmetics", "Kuća hemije"})


class Gender(Enum):
    MEN = "men"
    NOT_MEN = "not_men"
    UNCLEAR = "unclear"


# All patterns run on lower-case text without diacritics (see fold()).
_MEN_STRONG = re.compile(
    r"\b(men|man|for him|pour homme|homme|uomo|muski|muska|musko|muskarce|muskarci|muskarac|"
    r"old spice|axe|bulldog|proraso|brion)\b"
)
_MEN_WEAK = re.compile(r"\b(brijanj\w*|brijac\w*|after ?shave|aftershave|brad[aeu]|beard|gillette)\b")
_WOMEN = re.compile(r"\b(women|woman|zenski|zenska|zensko|zene|zena|femme|donna|for her|pour femme|lady|ladies|venus|girl)\b")
_UNISEX = re.compile(r"\b(unisex|uniseks)\b")
# "man" is not a men's signal inside a name like "X-Man". Products with heroes ("Spider-Man pjena za kupanje")
# are children's products, and children's products are never "exclusively men's", whatever the store category says.
_HERO_MAN = re.compile(r"\b(?:spider|iron|bat|super|pac|he|ant|aqua)[\s-]*man\b")
_HYPHEN_MAN = re.compile(r"\w-man\b")
_KIDS = re.compile(r"\b(djec\w*|djeca|dijete|kids?|junior|baby|bebe|beba|bebi\w*|djecak\w*|decak\w*)\b")
_GENDERED_TYPES = re.compile(r"\b(parfem\w*|parfim\w*|edt|edp|toaletna voda|kolonjsk\w*|deo|dezodor\w*|antiperspir\w*)\b")


def fold(text: str) -> str:
    text = text.replace("đ", "dj").replace("Đ", "Dj")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def classify(offer: Offer) -> Gender:
    name = fold(f"{offer.name}")
    cats = fold(" | ".join((offer.category, *offer.tags)))

    if _UNISEX.search(name) or _KIDS.search(name) or _HERO_MAN.search(name):
        return Gender.NOT_MEN
    name = _HYPHEN_MAN.sub(" ", name)
    # Strong words ("men", "homme", "muški"...) in the name beat the store category, which is sometimes wrong.
    # Weak words ("brijač", "Gillette"...) do not: a razor in "Brijač ženski" is a women's razor.
    name_strong = bool(_MEN_STRONG.search(name))
    name_weak = bool(_MEN_WEAK.search(name))
    if _WOMEN.search(name):
        return Gender.NOT_MEN  # also covers sets "for him & her"
    if name_strong:
        return Gender.MEN

    if _UNISEX.search(cats) or _KIDS.search(cats):
        return Gender.NOT_MEN
    cat_men_strong = bool(_MEN_STRONG.search(cats))
    if _WOMEN.search(cats):
        return Gender.UNCLEAR if cat_men_strong else Gender.NOT_MEN
    if name_weak or cat_men_strong or _MEN_WEAK.search(cats):
        return Gender.MEN
    if _GENDERED_TYPES.search(name) or _GENDERED_TYPES.search(cats):
        return Gender.UNCLEAR
    return Gender.NOT_MEN


_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"i": {"type": "INTEGER"}, "mens_only": {"type": "BOOLEAN"}},
        "required": ["i", "mens_only"],
    },
}


def resolve_unclear(offers: Sequence[Offer], gemini) -> set[tuple[str, str]]:
    """Asks Gemini which unclear products are made exclusively for men. Returns their keys."""
    return _ask_gemini(offers, gemini) or set()


def _ask_gemini(offers: Sequence[Offer], gemini) -> set[tuple[str, str]] | None:
    """Like resolve_unclear, but None when Gemini gave no usable answer (so it is not cached)."""
    if not offers:
        return set()
    if gemini is None:
        return None
    lines = "\n".join(f"{i}. {o.name} (kategorija: {o.category})" for i, o in enumerate(offers))
    prompt = (
        "Za svaki proizvod iz drogerije odgovori da li je namijenjen ISKLJUČIVO muškarcima "
        "(muški parfem, muški dezodorans, proizvod za brijanje za muškarce...). "
        "Unisex, ženski ili nepoznato = false. Ako nijesi siguran, odgovori false.\n\n" + lines
    )
    try:
        answer = gemini.generate_json(prompt, _SCHEMA)
    except Exception as exc:  # a broken AI answer must never break the report
        log.warning("Gemini nije odlučio nejasne proizvode (%s); izostavljeni su.", exc)
        return None
    if not isinstance(answer, list):
        log.warning("Gemini je vratio neočekivan odgovor za nejasne proizvode; izostavljeni su.")
        return None
    keys: set[tuple[str, str]] = set()
    for item in answer:
        if isinstance(item, dict) and item.get("mens_only") is True and isinstance(item.get("i"), int):
            if 0 <= item["i"] < len(offers):
                keys.add(offers[item["i"]].key)
    return keys


class VerdictStore(Protocol):
    def get(self, offer: Offer) -> bool | None: ...
    def put(self, offer: Offer, mens_only: bool) -> None: ...


def apply_store_rules(offers: Sequence[Offer], gemini=None, verdicts: VerdictStore | None = None) -> list[Offer]:
    """Keeps every offer from normal stores and only men's products from MENS_ONLY_STORES.

    `verdicts` (local dev cache) supplies earlier Gemini answers and receives new ones.
    """
    kept: list[Offer] = []
    unclear: list[Offer] = []
    for offer in offers:
        if offer.store not in MENS_ONLY_STORES:
            kept.append(offer)
            continue
        verdict = classify(offer)
        if verdict is Gender.MEN:
            kept.append(offer)
        elif verdict is Gender.UNCLEAR:
            unclear.append(offer)

    if unclear:
        approved: set[tuple[str, str]] = set()
        pending: list[Offer] = []
        for offer in unclear:
            cached = verdicts.get(offer) if verdicts is not None else None
            if cached is None:
                pending.append(offer)
            elif cached:
                approved.add(offer.key)
        for start in range(0, len(pending), 80):
            chunk = pending[start:start + 80]
            answer = _ask_gemini(chunk, gemini)
            if answer is None:
                continue  # failed answers are not cached, the next run asks again
            approved |= answer
            if verdicts is not None:
                for offer in chunk:
                    verdicts.put(offer, offer.key in answer)
        from_cache = len(unclear) - len(pending)
        cache_note = f" ({from_cache} odluka iz keša)" if verdicts is not None else ""
        log.info("Muški filter: %d nejasnih proizvoda%s, odobreno %d", len(unclear), cache_note, len(approved))
        kept.extend(o for o in unclear if o.key in approved)
    return kept
