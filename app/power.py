"""Klucz odpowiedników: te same substancje czynne i ta sama moc po normalizacji.

Pomysł ze starego MedicinalProductBuilder: substancje bierzemy z pola commonName
(nazwy powszechne, bez form soli), a moc sprowadzamy do jednej jednostki, np.
"100 mg/5 ml" -> "20 mg/ml", "(89 mg + 89 mg)/g" -> "89 mg/g" + "89 mg/g", "0,4 g" -> "400 mg".
"""
from __future__ import annotations

import re

MASS_TO_MG = {"g": 1000.0, "mg": 1.0, "µg": 0.001, "mcg": 0.001, "ug": 0.001, "ng": 0.000001}
RE_ION = re.compile(r"\b(Mg2\+|Ca2\+|Zn2\+|Fe2\+|Fe3\+|H\+|Na\+|K\+|Li\+)")
RE_NUM = r"\d+(?:\.\d+)?"
RE_COMPONENT = re.compile(rf"^({RE_NUM})\s*([^\d/\s][^/]*?)\s*(?:/\s*({RE_NUM})?\s*(\S.*))?$")
RE_BRACKETS = re.compile(rf"^\((.+)\)\s*/\s*({RE_NUM})?\s*(\S.*)$")


def _clean(text: str) -> str:
    text = text.lower().replace(" ", " ").replace("–", "-").replace("mikrogramów", "µg")
    text = re.sub(r"mikrogram\w*", "µg", text)
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    return re.sub(r"\s+", " ", text).strip()


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _component(text: str, divisor: float = 1.0, divisor_unit: str | None = None) -> str | None:
    m = RE_COMPONENT.match(text.strip())
    if not m:
        return None
    value, unit, div, div_unit = float(m.group(1)), m.group(2).strip(), m.group(3), m.group(4)
    if div_unit is None and divisor_unit:
        div, div_unit = None, divisor_unit
        value /= divisor
    elif div:
        value /= float(div)
    if unit in MASS_TO_MG:
        value, unit = value * MASS_TO_MG[unit], "mg"
    return f"{_fmt(value)} {unit}" + (f"/{div_unit.strip()}" if div_unit else "")


def normalize_power(power: str | None) -> list[str] | None:
    """Lista mocy znormalizowanych, po jednej na substancję; None, gdy nie da się rozebrać."""
    if not power:
        return None
    text = _clean(power)
    m = RE_BRACKETS.match(text)
    if m:
        divisor = float(m.group(2)) if m.group(2) else 1.0
        parts = [_component(p, divisor, m.group(3)) for p in m.group(1).split("+")]
    else:
        parts = [_component(p) for p in text.split("+")]
    return parts if all(parts) else None


def active_substances(common_name: str | None) -> list[str]:
    if not common_name:
        return []
    protected = RE_ION.sub(lambda m: m.group(0).replace("+", "⁺"), common_name)
    return [s.strip().lower() for s in protected.split("+") if s.strip()]


def group_key(common_name: str | None, power: str | None) -> str | None:
    subs = active_substances(common_name)
    if not subs:
        return None
    powers = normalize_power(power)
    if powers and len(powers) == len(subs):
        return "|".join(sorted(f"{s}={p}" for s, p in zip(subs, powers)))
    # nie da się sparować: grupujemy po zbiorze substancji i surowej mocy
    return "|".join(sorted(subs)) + "=" + _clean(power or "")
