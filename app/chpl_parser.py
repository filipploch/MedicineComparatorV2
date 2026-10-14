"""Parser punktu 6.1 ChPL ("Wykaz substancji pomocniczych").

Moduł nie zależy od Flaska: dostaje tekst PDF i tabele słownikowe (Tables), zwraca Result.

Reguły:
  * płaska lista substancji, bez podziału na rdzeń/otoczkę;
  * etykiety części leku są pomijane według tabeli pomijanych etykiet;
    nieznana etykieta z dwukropkiem jest pomijana i zgłaszana redaktorowi;
  * mieszanki ("Opadry II (...)", "aromat (zawiera ...)", "tusz zawierający ...") są rozbijane
    na składniki; mieszanki podane bez składu rozwija tabela mieszanek;
  * numery E są tłumaczone na nazwy, nazwy sprowadzane do nazw kanonicznych (synonimy);
    nazwa spoza słownika kieruje dokument do weryfikacji;
  * skład zależny od mocy ("[Tylko dla 20 mg]", "267 mg tabletka") jest zapisywany przy
    substancji, a skladniki_dla_mocy() zwraca listę dla konkretnego rekordu z rejestru.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import pdfplumber

MIN_TEXT_CHARS = 200
MAX_SECTION_CHARS = 4000

# Powody skierowania do weryfikacji
NO_TEXT = "BRAK_WARSTWY_TEKSTOWEJ"
PDF_ERROR = "BLAD_ODCZYTU_PDF"
NO_SECTION = "NIE_ZNALEZIONO_PUNKTU_6_1"
NO_SECTION_END = "NIE_ZNALEZIONO_KONCA_PUNKTU_6_1"
UNKNOWN_SUBSTANCE = "NIEZNANA_SUBSTANCJA"
UNKNOWN_LABEL = "NIEZNANA_ETYKIETA"
UNKNOWN_E = "NIEZNANY_NUMER_E"
EMPTY = "PUSTA_LISTA"

REASON_LABELS = {
    NO_TEXT: "PDF bez warstwy tekstowej (skan)",
    PDF_ERROR: "nie udało się odczytać PDF",
    NO_SECTION: "nie znaleziono punktu 6.1",
    NO_SECTION_END: "nie znaleziono końca punktu 6.1",
    UNKNOWN_SUBSTANCE: "nazwa spoza słownika",
    UNKNOWN_LABEL: "nieznana etykieta",
    UNKNOWN_E: "nieznany numer E",
    EMPTY: "pusta lista substancji",
    "MOC_NIEDOPASOWANA": "moc leku nie występuje w nagłówkach ChPL",
}

RE_START = re.compile(r"^[ \t]*6\.1\.?[ \t]+Wykaz[ \t]+substancji[ \t]+pomocniczych[^\n]*\n", re.I | re.M)
RE_END = re.compile(r"^[ \t]*6\.2\.?[ \t]+Niezgodno", re.I | re.M)
RE_END_FALLBACK = re.compile(r"^[ \t]*(6\.[2-9]\.?[ \t]|7\.[ \t]+PODMIOT)", re.I | re.M)
RE_PAGE_LINE = re.compile(r"^[ \t]*(strona[ \t]+)?\d{1,3}([ \t]*(/|z)[ \t]*\d{1,3})?[ \t]*$", re.I | re.M)

_D = (r"\d+(?:[.,]\d+)?\s*(?:mg|µg|mcg|mikrogram(?:ów|y)?|g|ml|j\.\s?m\.|%)"
      r"(?:\s*/\s*(?:ml|g|dawkę|dawka))?(?![a-ząćęłńóśźż])")
RE_DOSE = re.compile(rf"{_D}(?:\s*\+\s*{_D})*", re.I)
RE_SCOPE = re.compile(r"[\(\[]\s*(?:tylko\s+)?dla\s+([^()\[\]]*)[\)\]]", re.I)
RE_E = re.compile(r"\(?\b[EΕε][ \t-]?(\d{3,4}[a-z]?)\b\)?", re.I)
RE_NOTE = re.compile(r"\((?:do|w celu)\s+(?:ustalenia|korekty|regulacji|dostosowania)\s+(?:wartości\s+)?ph\)", re.I)
RE_CONTAINS = re.compile(r"^(?P<name>.*?)\s*\(?\s*(?:zawierając\w*|zawiera)\s+(?P<list>[^()]*)\)?\s*$", re.I)
RE_INLINE_LABEL = re.compile(r"^(?P<label>[^,:()]{1,60}(?:\([^()]*\))?)\s*:\s*(?P<rest>.*)$")
RE_NONE = re.compile(r"^(nie zawiera|brak|nie dotyczy)\b", re.I)
RE_GRADE = re.compile(r"(?:\s+(?:typ\s+\w+|k[\s-]?\d+|\d+(?:[.,]\d+)?\s*(?:cp|mpas|mpa\.s)|ph-?\d+\S*|\d+))+$", re.I)
FORM_WORDS = re.compile(r"\b(tabletk\w*|kapsuł\w*|powlekan\w*|drażowan\w*|o\s+mocy|moc\w*|dawk\w*|dla)\b", re.I)


def norm(text: str) -> str:
    text = text.replace(" ", " ").replace("–", "-")
    return re.sub(r"\s+", " ", text).strip(" .;,:-").lower()


def norm_key(text: str) -> str:
    """Klucz słownikowy: małe litery, bez przecinków i podwójnych spacji."""
    return re.sub(r"\s+", " ", norm(text).replace(",", " ")).strip()


def norm_dose(text: str) -> str:
    """'12,5 mg' -> '12.5mg', '5 mg + 5 mg' -> '5mg+5mg'."""
    text = text.lower().replace(",", ".").replace("mcg", "µg")
    text = re.sub(r"mikrogram\w*", "µg", text)
    return re.sub(r"\s+", "", text)


# --- tabele słownikowe -----------------------------------------------------------
@dataclass
class Tables:
    ignored: set[str]
    substances: dict[str, str]        # klucz (nazwa lub synonim) -> nazwa kanoniczna
    e_numbers: dict[str, str]         # "E171" -> nazwa kanoniczna
    premixes: dict[str, list[str]]    # klucz nazwy mieszanki -> składniki

    @classmethod
    def build(cls, ignored: Iterable[str], substances: dict[str, Iterable[str]],
              e_numbers: dict[str, str], premixes: dict[str, list[str]]) -> "Tables":
        subst: dict[str, str] = {}
        for canon, synonyms in substances.items():
            for s in [canon, *synonyms]:
                subst[norm_key(s)] = canon
        return cls(ignored={norm_key(i) for i in ignored},
                   substances=subst,
                   e_numbers={k.upper(): v for k, v in e_numbers.items()},
                   premixes={norm_key(k): list(v) for k, v in premixes.items()})

    @classmethod
    def from_seed(cls, folder: Path) -> "Tables":
        return cls.build(*read_seed(folder))

    def canonical(self, name: str) -> str | None:
        key = norm_key(name)
        no_paren = norm_key(re.sub(r"\([^()]*\)", " ", key))
        for candidate in (key, no_paren, RE_GRADE.sub("", no_paren).strip()):
            if candidate in self.substances:
                return self.substances[candidate]
        return None

    def is_label(self, text: str) -> bool:
        return norm_key(text) in self.ignored


def read_seed(folder: Path):
    ignored = [l for l in (folder / "pomijane_etykiety.txt").read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.startswith("#")]
    substances = json.loads((folder / "substancje.json").read_text(encoding="utf-8"))
    e_numbers = json.loads((folder / "numery_e.json").read_text(encoding="utf-8"))
    premixes = json.loads((folder / "mieszanki.json").read_text(encoding="utf-8"))
    return ignored, substances, e_numbers, premixes


# --- wynik --------------------------------------------------------------------------
@dataclass
class Excipient:
    nazwa: str                         # nazwa kanoniczna albo oczyszczony tekst z ChPL
    oryginal: str                      # tekst z ChPL
    e: str | None = None
    znana: bool = True                 # czy jest w słowniku
    dawki: list[str] | None = None     # None = wszystkie moce z tego ChPL


@dataclass
class Result:
    status: str = "OK"                 # OK | WYMAGA_WERYFIKACJI
    powody: list[str] = field(default_factory=list)
    substancje: list[Excipient] = field(default_factory=list)
    brak_substancji: bool = False      # ChPL deklaruje "Nie zawiera"
    dawki_z_naglowkow: list[str] = field(default_factory=list)
    nieznane: list[str] = field(default_factory=list)
    surowy_6_1: str = ""

    def flag(self, reason: str) -> None:
        self.status = "WYMAGA_WERYFIKACJI"
        if reason not in self.powody:
            self.powody.append(reason)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("surowy_6_1")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        d = dict(d)
        d["substancje"] = [Excipient(**e) for e in d.get("substancje", [])]
        return cls(**d)


# --- ekstrakcja -----------------------------------------------------------------------
def extract_text(pdf: bytes | Path) -> str:
    source = io.BytesIO(pdf) if isinstance(pdf, (bytes, bytearray)) else pdf
    with pdfplumber.open(source) as doc:
        return "\n".join((page.extract_text() or "") for page in doc.pages)


def cut_section(text: str) -> tuple[str, bool]:
    for start in RE_START.finditer(text):
        rest = text[start.end():]
        end = RE_END.search(rest) or RE_END_FALLBACK.search(rest)
        if end and end.start() <= MAX_SECTION_CHARS:
            return rest[:end.start()], True
    start = RE_START.search(text)
    if start:
        return text[start.end():start.end() + MAX_SECTION_CHARS], False
    return "", False


def split_top_level(text: str, seps: str = ",") -> list[str]:
    """Dzieli po separatorach poza nawiasami; przecinek między cyframi (0,5) nie dzieli."""
    parts, buf, depth = [], [], 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and ch in seps and not (
                ch == "," and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()):
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _unbalanced(line: str) -> bool:
    return line.count("(") > line.count(")") or line.count("[") > line.count("]")


def _is_comma_list(line: str) -> bool:
    return len(split_top_level(line)) >= 3


def logical_lines(section: str) -> list[str]:
    """Skleja linie zawinięte przez PDF."""
    text = RE_PAGE_LINE.sub("", section)
    text = re.sub(r"(?<=[a-ząćęłńóśźż])-\n(?=[a-ząćęłńóśźż])", "", text)
    out: list[str] = []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if out:
            prev = out[-1]
            if (_unbalanced(prev) or prev.endswith(",")
                    or (_is_comma_list(prev) and not prev.endswith((".", ";", ":")) and line[:1].islower())):
                out[-1] = prev + " " + line
                continue
        out.append(line)
    return out


def _doses(text: str) -> list[str]:
    return [norm_dose(d) for d in RE_DOSE.findall(text)]


def _dose_label(line: str, tables: Tables) -> list[str] | None:
    """'267 mg tabletka', 'Osłonka kapsułki (5 mg + 5 mg + 12,5 mg)', 'Tabletki 200 mg:'."""
    doses = _doses(line)
    if not doses:
        return None
    rest = re.sub(r"[()\[\]:]", " ", RE_DOSE.sub(" ", line))
    if tables.is_label(rest) or not norm_key(FORM_WORDS.sub(" ", rest)):
        return doses
    return None


def _split_components(text: str) -> list[str]:
    """Rozbija mieszankę na składniki; nazwa handlowa mieszanki jest pomijana."""
    m = RE_CONTAINS.match(text)
    if m and m.group("list").strip():
        return [i for i in re.split(r",\s*|\s+i\s+|\s+oraz\s+", m.group("list")) if i.strip()]
    start = text.find("(")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                inner = split_top_level(text[start + 1:i], ",;")
                if len(inner) > 1:
                    out: list[str] = []
                    for part in inner:
                        out.extend(_split_components(part))
                    tail = text[i + 1:].strip(" .")
                    if tail:
                        out.append(tail)
                    return out
                break
    return [text]


def _resolve(piece: str, doses: list[str] | None, tables: Tables, result: Result, depth: int = 0) -> list[Excipient]:
    piece = RE_NOTE.sub(" ", piece)
    premix = tables.premixes.get(norm_key(RE_E.sub(" ", piece)))
    if premix is not None and depth < 3:
        out: list[Excipient] = []
        for comp in premix:
            out.extend(_resolve(comp, doses, tables, result, depth + 1))
        return out
    codes = ["E" + c.lower() for c in RE_E.findall(piece)]
    code = codes[0] if codes else None
    name = norm(re.sub(r"\(\s*\)|\[\s*\]", " ", RE_E.sub(" ", piece)))
    original = re.sub(r"\s+", " ", piece).strip(" .;,")
    if not name:
        if not code:
            return []
        canon = tables.e_numbers.get(code.upper())
        if canon is None:
            result.flag(UNKNOWN_E)
            result.nieznane.append(code)
            return [Excipient(code, original, code, False, doses)]
        return [Excipient(canon, original, code, True, doses)]
    canon = tables.canonical(name)
    if canon is None:
        result.flag(UNKNOWN_SUBSTANCE)
        result.nieznane.append(name)
        return [Excipient(name, original, code, False, doses)]
    return [Excipient(canon, original, code, True, doses)]


def parse_section(section: str, tables: Tables) -> Result:
    """Parsuje surowy tekst punktu 6.1 (też ponownie, po zmianach w słownikach)."""
    result = Result(surowy_6_1=section.strip())
    scope: list[str] | None = None
    found: list[Excipient] = []
    lines = logical_lines(section)

    if len(lines) == 1 and RE_NONE.match(lines[0]):
        result.brak_substancji = True
        return result

    def set_scope(doses: list[str]) -> list[str]:
        result.dawki_z_naglowkow.extend(d for d in doses if d not in result.dawki_z_naglowkow)
        return doses

    for line in lines:
        text = line
        m = RE_INLINE_LABEL.match(line)
        if m and not RE_DOSE.fullmatch(m.group("label").strip()):
            label = m.group("label")
            doses = _dose_label(label, tables)
            if doses:
                scope = set_scope(doses)
            elif not tables.is_label(label):
                result.flag(UNKNOWN_LABEL)
                result.nieznane.append(norm(label) + ":")
            text = m.group("rest")
            if not text.strip():
                continue
        elif tables.is_label(line):
            continue
        else:
            doses = _dose_label(line, tables)
            if doses:
                scope = set_scope(doses)
                continue

        item_scope = scope
        for sm in RE_SCOPE.finditer(text):
            d = _doses(sm.group(1))
            if d:
                item_scope = d
                text = text.replace(sm.group(0), " ")

        pieces = split_top_level(text, ",;") if _is_comma_list(text) else [text]
        for piece in pieces:
            for sub in re.split(r"\s+oraz\s+", piece):
                for comp in _split_components(sub):
                    found.extend(_resolve(comp, list(item_scope) if item_scope else None, tables, result))

    # scalanie duplikatów: ta sama substancja w rdzeniu i otoczce albo w kilku mocach
    merged: dict[str, Excipient] = {}
    for ex in found:
        prev = merged.get(ex.nazwa)
        if prev is None:
            merged[ex.nazwa] = ex
            continue
        if prev.dawki is None or ex.dawki is None:
            prev.dawki = None
        else:
            prev.dawki = prev.dawki + [d for d in ex.dawki if d not in prev.dawki]  # nowa lista: zakresy są współdzielone
        prev.e = prev.e or ex.e
    result.substancje = list(merged.values())
    result.nieznane = list(dict.fromkeys(result.nieznane))
    if not result.substancje:
        result.flag(EMPTY)
    return result


def parse_text(text: str, tables: Tables) -> Result:
    """Parsuje cały tekst ChPL."""
    if len(text.strip()) < MIN_TEXT_CHARS:
        r = Result()
        r.flag(NO_TEXT)
        return r
    section, has_end = cut_section(text)
    if not section.strip():
        r = Result()
        r.flag(NO_SECTION)
        return r
    result = parse_section(section, tables)
    if not has_end:
        result.flag(NO_SECTION_END)
    return result


def parse_pdf(pdf: bytes | Path, tables: Tables) -> tuple[Result, str]:
    """Zwraca (wynik, cały tekst PDF)."""
    try:
        text = extract_text(pdf)
    except Exception as exc:  # uszkodzony lub zaszyfrowany plik
        r = Result()
        r.flag(PDF_ERROR)
        r.nieznane.append(exc.__class__.__name__)
        return r, ""
    return parse_text(text, tables), text


def skladniki_dla_mocy(result: Result, moc: str | None) -> list[Excipient] | None:
    """Skład dla rekordu z rejestru o danej mocy (pole medicinalProductPower).
    None = nie da się przypisać automatycznie, rekord trafia do weryfikacji."""
    d = norm_dose(moc or "")
    if result.dawki_z_naglowkow and d not in result.dawki_z_naglowkow:
        return None
    return [ex for ex in result.substancje if ex.dawki is None or d in ex.dawki]
