"""Common product groups across all stores.

Every store has its own categories (Voli has hundreds: "Tuna", "Biber", "DEO"...), so the report
and the site sort products into a short fixed list. The store category is matched first, the
product name only when the category says nothing. Idea's Gemini picks from this same list.
"""
from __future__ import annotations

import re

from akcije.gender import fold
from akcije.models import Offer

OTHER = "Ostalo"

# Order matters: the first matching rule wins (e.g. "Smrznuta piletina" -> Smrznuto, "Baby kozmetika" -> Bebe).
# Patterns run on lower-case text without diacritics and must match at the start of a word.
_RULES: list[tuple[str, str]] = [
    ("Bebe i djeca", r"bebi|beba|baby|djec|dijete|pelen|dohran"),
    ("Kućni ljubimci", r"ljubim|pse\b|psi\b|macke|macak|pas\b|cat\b|dog\b"),
    ("Smrznuto", r"smrznut|zamrznut|sladoled"),
    ("Parfemi", r"parfem|parfim|edt\b|edp\b|toaletna voda|kolonjsk|body mist"),
    ("Odjeća i ostalo", r"ves\b|bokseric|carap|sokn|nazuv|hulahop|majic|galanter|bizuter|tekstil"),
    ("Lična higijena", r"higijen(?!a odjec)|sapun|kupk|kupanj|tusir|deo\b|dezodor|antiperspir|brij|after ?shave|zub|"
                       r"dental|oraln|samp|regenerator|balzam za kosu|maramic|ulosc|tampon|vata|pamuk"),
    ("Kozmetika i njega", r"kozmet|njeg|krem|losion|serum|lice|sminka|usn|oci\b|nokt|kos[aeu]\b|kose\b|"
                          r"depilac|manikir|pedikir|uljepsav|sunc|dekorativ"),
    ("Domaćinstvo i čišćenje", r"cisc|deterdz|pranje|vesa\b|omeksiv|domacinstv|kucna hemija|higijena odjec|papir|"
                               r"salvet|ubrus|toalet|kese|folij|sundjer|osvjezivac|wc\b|posudj|tablete za"),
    ("Piće", r"pice\b|napit|sok\b|sokov|voda\b|vode\b|gazir|kaf[aeiu]\b|caj\b|cajev|pivo|piva|vino|vina|rakij|viski|"
             r"vodk|liker|alkohol|energet|kakao"),
    ("Mliječni proizvodi i jaja", r"mlijec|mlijek|jogurt|sir\b|sirevi|sireva|siru|kajmak|pavlak|maslac|puter|jaja|"
                                  r"jaje|kefir|margarin|namaz od sir|desert"),
    ("Meso i riba", r"meso|mesn|piletin|pilec|svinj|junet|govedj|teletin|kobasic|virsl|salam|pariz|sunk|mortadel|"
                    r"slanin|pecenic|suhomes|tuna|riba|ribe|sardin|losos|pastet|cevap|pljeskavic"),
    ("Voće i povrće", r"voce|povrc|krompir|salat|paradajz|jabuk|banan|limun|narandz|pecurk|luk\b|kupus|tikvic|krastav"),
    ("Hljeb, tjestenina i žitarice", r"hljeb|hleb|pecivo|peciva|pekar|tost\b|tjestenin|testenin|spaget|makaron|"
                                     r"pirinac|riza|brasno|cerealij|musli|pahuljic|corn flakes|zitar|gluten"),
    ("Slatkiši i grickalice", r"slatk|cokolad|bombon|karamel|keks|biskvit|medenjak|vafl|napolitank|bar\b|barovi|"
                              r"impuls|grick|cips|smoki|slanis|kikiriki|orasast|kroasan|torta|kolac"),
    ("Namirnice i začini", r"ulj[ae]|secer|so\b|soli\b|zacin|biber|kecap|majonez|senf|preliv|sos|konzerv|dzem|"
                           r"marmelad|med\b|sastoj|pasulj|grasak|kukuruz|ocat|sirce|juh|supa|supe|diet|dijabet|apetis"),
    ("Odjeća i ostalo", r"pribor|aparat|uredaj|masazer|igrack"),
]
_COMPILED = [(group, re.compile(rf"\b(?:{pattern})")) for group, pattern in _RULES]
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(g for g, _ in _RULES)) + (OTHER,)


def group_of(offer: Offer) -> str:
    if offer.category in GROUPS:
        # Idea: Gemini already picked a group, but its choice is sometimes odd (a hair trimmer in "Domaćinstvo"),
        # so a clear keyword in the product name wins and Gemini's group is the fallback.
        return _match(offer.name) or offer.category
    return _match(" | ".join((offer.category, *offer.tags))) or _match(offer.name) or OTHER


def _match(text: str) -> str | None:
    folded = fold(text)
    for group, pattern in _COMPILED:
        if pattern.search(folded):
            return group
    return None
