"""Rozpoznawanie tego, co użytkownik wpisał lub zeskanował: nazwa, GTIN albo kod Data Matrix.

Data Matrix na opakowaniach leków (serializacja, GS1) zawiera kolejne pola z identyfikatorami
zastosowań (AI): (01) GTIN-14, (17) data ważności, (10) numer serii, (21) numer seryjny. Skaner
działający jak klawiatura wpisuje je jednym ciągiem, czasem z prefiksem symbologii "]d2"
i separatorem GS (znak 29) między polami o zmiennej długości. GTIN ma stałą długość i zwykle stoi
na początku, więc da się go wyciągnąć także wtedy, gdy separatory zginęły po drodze.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

GS = "\x1d"
# Znaki, które mogą poprzedzać kod (np. "@" ze skanera); lista do uzupełnienia w konfiguracji.
DEFAULT_PREFIXES = os.environ.get("SCAN_PREFIXES", "@")
SYMBOLOGY_IDS = ("]d2", "]C1", "]e0", "]Q3")
RE_AI_PARENS = re.compile(r"\((\d{2,4})\)")


@dataclass(frozen=True)
class Query:
    kind: str                # "name" | "gtin" | "empty"
    value: str               # nazwa albo GTIN-14
    source: str = ""         # "gtin" | "datamatrix" (skąd wzięto GTIN)
    valid: bool = True       # czy cyfra kontrolna GTIN się zgadza


def gtin_check_ok(code: str) -> bool:
    """Cyfra kontrolna GS1 (mod 10) dla GTIN-8/12/13/14."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(d) for d in code]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def to_gtin14(code: str) -> str:
    return code.zfill(14)


def gtin_from_datamatrix(data: str) -> str | None:
    """Zwraca GTIN-14 z treści kodu GS1 Data Matrix albo None."""
    text = data.strip()
    for sid in SYMBOLOGY_IDS:
        if text.startswith(sid):
            text = text[len(sid):]
    text = text.lstrip(GS)
    if "(" in text:                                   # zapis czytelny: (01)0590...(17)...
        text = RE_AI_PARENS.sub(lambda m: GS + m.group(1), text).lstrip(GS)
    if text.startswith("01") and len(text) >= 16 and gtin_check_ok(text[2:16]):
        return text[2:16]
    # (01) nie na początku: szukamy po separatorze GS albo w całym ciągu (z cyfrą kontrolną)
    for m in re.finditer(r"(?:^|\x1d)01(\d{14})", text):
        if gtin_check_ok(m.group(1)):
            return m.group(1)
    for m in re.finditer(r"01(\d{14})", text):
        if gtin_check_ok(m.group(1)):
            return m.group(1)
    return None


def classify(raw: str, prefixes: str = DEFAULT_PREFIXES) -> Query:
    text = (raw or "").replace("\r", "").replace("\n", "").strip()
    while text and text[0] in prefixes:
        text = text[1:].lstrip()
    if not text:
        return Query("empty", "")
    compact = text.replace(" ", "").replace("-", "")
    # Sam GTIN: EAN-8, UPC-A, EAN-13 (kod kreskowy na pudełku) albo GTIN-14
    if compact.isdigit() and len(compact) in (8, 12, 13, 14):
        return Query("gtin", to_gtin14(compact), "gtin", gtin_check_ok(compact))
    # Data Matrix: dłuższy ciąg zaczynający się od AI (01) lub z prefiksem symbologii / separatorem GS
    looks_like_dm = (GS in text or text.startswith(SYMBOLOGY_IDS) or text.startswith("(01)")
                     or (re.match(r"01\d{14}", compact) is not None and len(compact) > 16))
    if looks_like_dm:
        gtin = gtin_from_datamatrix(text)
        if gtin:
            return Query("gtin", gtin, "datamatrix", True)
    if compact.isdigit() and len(compact) > 14:
        gtin = gtin_from_datamatrix(compact)
        if gtin:
            return Query("gtin", gtin, "datamatrix", True)
    return Query("name", text)
