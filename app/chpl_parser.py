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
NO_LEAFLET_SECTION = "NIE_ZNALEZIONO_PUNKTU_6_ULOTKI"
NO_EXCIPIENT_LIST = "ULOTKA_BEZ_LISTY_SUBSTANCJI_POMOCNICZYCH"

REASON_LABELS = {
    NO_TEXT: "PDF bez warstwy tekstowej (skan)",
    PDF_ERROR: "nie udało się odczytać PDF",
    NO_SECTION: "nie znaleziono punktu 6.1",
    NO_SECTION_END: "nie znaleziono końca punktu 6.1",
    UNKNOWN_SUBSTANCE: "nazwa spoza słownika",
    UNKNOWN_LABEL: "nieznana etykieta",
    UNKNOWN_E: "nieznany numer E",
    EMPTY: "pusta lista substancji",
    NO_LEAFLET_SECTION: "nie znaleziono w ulotce części „Co zawiera lek”",
    NO_EXCIPIENT_LIST: "ulotka nie wymienia osobno substancji pomocniczych",
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
# Odesłania w treści listy: "(patrz punkt 2)", ", patrz punkt 4.4", "(zob. pkt 2)"
RE_REFERENCE = re.compile(r"\(?\s*(?:patrz|zob\.?|zobacz)\s+(?:też\s+|również\s+)?(?:punkt|pkt\.?)\s*\d+(?:\.\d+)*"
                          r"[^,;()\n]*\)?", re.I)
RE_CONTAINS = re.compile(r"^(?P<name>.*?)\s*\(?\s*(?:zawierając\w*|zawiera)\s+(?P<list>[^()]*)\)?\s*$", re.I)
RE_INLINE_LABEL = re.compile(r"^(?P<label>[^,:()]{1,60}(?:\([^()]*\))?)\s*:\s*(?P<rest>.*)$")
RE_NONE = re.compile(r"^(nie zawiera|brak|nie dotyczy)\b", re.I)
RE_GRADE = re.compile(r"(?:\s+(?:typ\s+\w+|k[\s-]?\d+|\d+(?:[.,]\d+)?\s*(?:cp|mpas|mpa\.s)|ph-?\d+\S*|\d+/\d+|\d+(?:\s*[a-z])?))+$", re.I)
# Same określenia, które po podziale po przecinkach należą do poprzedniej nazwy ("skrobia żelowana, kukurydziana")
MODIFIERS = {"kukurydziana", "ziemniaczana", "pszenna", "ryżowa", "grochowa", "bezwodna", "bezwodny", "jednowodna",
             "jednowodny", "dwuwodny", "dwuwodna", "oczyszczona", "oczyszczony", "częściowo odwodniony",
             "częściowo odwodniona", "niekrystalizujący", "krystalizujący", "rafinowany", "rafinowana"}
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

    def is_label_prefix(self, text: str) -> bool:
        """Etykieta z dwukropkiem, która zaczyna się od znanej: "otoczka tabletki powlekanej", "otoczka opadry ii"."""
        key = norm_key(text)
        return key in self.ignored or any(key.startswith(label + " ") for label in self.ignored)


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
    nieznane: list[str] = field(default_factory=list)   # nazwy do decyzji redaktora (substancje, etykiety, numery E)
    blad: str = ""                                       # techniczny opis błędu odczytu pliku
    eu: list[str] = field(default_factory=list)          # numery pozwoleń EU z punktu 8 (leki centralne)
    # Dokument z kilkoma charakterystykami (np. informacja o produkcie EMA dla kilku mocy):
    # [{"nazwa": linia z punktu 1, "eu": [...], "surowy_6_1": "...", "wynik": {...}}]
    czesci: list[dict] = field(default_factory=list)
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
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
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
    # od 2 pozycji; "Sorbitol ciekły, częściowo odwodniony" scala potem lista MODIFIERS
    return len(split_top_level(line)) >= 2


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
    piece = RE_REFERENCE.sub(" ", RE_NOTE.sub(" ", piece))
    if tables.is_label(RE_E.sub(" ", piece)):     # tekst oznaczony przez redaktora do pomijania, w dowolnym miejscu listy
        return []
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
            elif not tables.is_label_prefix(label):
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

        pieces: list[str] = []
        for piece in (split_top_level(text, ",;") if _is_comma_list(text) else [text]):
            lm = RE_INLINE_LABEL.match(piece)
            if lm and lm.group("rest").strip() and tables.is_label_prefix(lm.group("label")):
                piece = lm.group("rest")
            bare = norm(re.sub(r"\([^()]*\)", " ", RE_E.sub(" ", piece)))
            if pieces and (bare in MODIFIERS or bare.startswith(("roztwór", "roztwor"))):   # "sodu wodorotlenek, roztwór 10%"
                pieces[-1] = f"{pieces[-1]} {piece}"
            else:
                pieces.append(piece)
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


# --- dokumenty z kilkoma charakterystykami (EMA) ------------------------------------------
RE_ANNEX_II = re.compile(r"^[ \t]*ANEKS[ \t]+II\b", re.M)
RE_NAME_HEADER = re.compile(r"^[ \t]*1\.?[ \t]+NAZWA[ \t]+PRODUKTU[ \t]+LECZNICZEGO[^\n]*\n", re.M)
RE_EU = re.compile(r"EU/(\d)/(\d{2})/(\d{3,4})/(\d{3})"
                   r"(?:\s*[-–]\s*(?:EU/\d/\d{2}/\d{3,4}/)?(\d{3})(?!\d))?")


def eu_numbers(text: str) -> list[str]:
    """Numery pozwoleń EU, z rozwinięciem zakresów: "EU/1/97/046/001-003" -> 001, 002, 003."""
    out: list[str] = []
    for m in RE_EU.finditer(text):
        base = f"EU/{m.group(1)}/{m.group(2)}/{m.group(3)}/"
        first = int(m.group(4))
        last = int(m.group(5)) if m.group(5) and int(m.group(5)) >= first and int(m.group(5)) - first < 200 else first
        out.extend(f"{base}{n:03d}" for n in range(first, last + 1))
    return list(dict.fromkeys(out))


def eu_base(number: str) -> str:
    """"EU/1/97/046/004" -> "EU/1/97/046" (pozwolenie bez numeru opakowania)."""
    return number.rsplit("/", 1)[0]


def split_smpc(text: str) -> list[str]:
    """Dzieli tekst na charakterystyki. W informacji o produkcie EMA bierze tylko Aneks I
    (dalej są etykiety, które też mają punkt "1. Nazwa produktu leczniczego")."""
    m = RE_ANNEX_II.search(text)
    body = text[:m.start()] if m else text
    starts = [h.start() for h in RE_NAME_HEADER.finditer(body)]
    if len(starts) <= 1:
        return [body]
    return [body[a:b] for a, b in zip(starts, starts[1:] + [len(body)])]


def _parse_part(text: str, tables: Tables) -> Result:
    section, has_end = cut_section(text)
    if not section.strip():
        r = Result()
        r.flag(NO_SECTION)
    else:
        r = parse_section(section, tables)
        if not has_end:
            r.flag(NO_SECTION_END)
    r.eu = eu_numbers(text)
    return r


def _part_name(text: str) -> str:
    m = RE_NAME_HEADER.search(text)
    rest = text[m.end():] if m else text
    return next((l.strip() for l in rest.splitlines() if l.strip()), "")[:200]


def _combine(parts: list[tuple[str, Result]]) -> Result:
    top = Result()
    for name, r in parts:
        top.czesci.append({"nazwa": name, "eu": r.eu, "surowy_6_1": r.surowy_6_1, "wynik": r.to_dict()})
        for reason in r.powody:
            top.flag(reason)
        top.nieznane.extend(n for n in r.nieznane if n not in top.nieznane)
        top.eu.extend(e for e in r.eu if e not in top.eu)
    top.surowy_6_1 = "\n\n".join(f"[{name}]\n{r.surowy_6_1}" for name, r in parts)
    return top


def parse_text(text: str, tables: Tables) -> Result:
    """Parsuje cały tekst ChPL (albo informacji o produkcie EMA z kilkoma charakterystykami)."""
    if len(text.strip()) < MIN_TEXT_CHARS:
        r = Result()
        r.flag(NO_TEXT)
        return r
    parts = split_smpc(text)
    if len(parts) == 1:
        return _parse_part(parts[0], tables)
    return _combine([(_part_name(p), _parse_part(p, tables)) for p in parts])


def reparse(previous: dict, raw_section: str | None, tables: Tables) -> Result:
    """Ponowne parsowanie zapisanych punktów 6.1 po zmianach w słownikach (bez pobierania PDF)."""
    prev = Result.from_dict(previous or {})
    if prev.czesci:
        parts = []
        for part in prev.czesci:
            old = Result.from_dict(part.get("wynik", {}))
            r = parse_section(part["surowy_6_1"], tables) if part.get("surowy_6_1") else old
            for keep in (NO_SECTION, NO_SECTION_END):
                if keep in old.powody:
                    r.flag(keep)
            r.eu = part.get("eu", [])
            parts.append((part.get("nazwa", ""), r))
        return _combine(parts)
    r = parse_section(raw_section or "", tables)
    if NO_SECTION_END in prev.powody:
        r.flag(NO_SECTION_END)
    r.eu = prev.eu
    return r


def wybierz_czesc(result: Result, product_eu: list[str], moc: str | None) -> Result | None:
    """Charakterystyka z dokumentu właściwa dla leku: po numerach EU jego opakowań,
    a gdy ich brak, po mocy w nazwie z punktu 1. None = nie da się wskazać jednoznacznie."""
    if not result.czesci:
        return result
    wanted = set(product_eu or [])
    by_eu = [p for p in result.czesci if wanted & set(p.get("eu", []))]
    if len(by_eu) == 1:
        return Result.from_dict(by_eu[0]["wynik"])
    candidates = by_eu or result.czesci
    d = norm_dose(moc or "")
    by_power = [p for p in candidates
                if d and re.search(rf"(?<![\d.]){re.escape(d)}", norm_dose(p.get("nazwa", "")))]
    if len(by_power) == 1:
        return Result.from_dict(by_power[0]["wynik"])
    return None


# --- ulotka dla pacjenta, punkt 6 "Co zawiera lek X" -----------------------------------
RE_LEAFLET_START = re.compile(r"^[ \t]*(?:6\.\d\.?[ \t]*)?Co[ \t]+zawiera\b[^\n]*$", re.I | re.M)
RE_LEAFLET_END = re.compile(r"^[ \t]*(?:6\.\d\.?[ \t]*)?Jak[ \t]+wygląda\b", re.I | re.M)
RE_LEAFLET_LEAD = re.compile(
    r"(?:(?:Pozostałe|Inne)[ \t]+(?:składniki|substancje)(?:[ \t]+pomocnicze)?"
    r"(?:[ \t]*\((?:substancje[ \t]+pomocnicze)\))?(?:[ \t]+leku)?(?:[ \t]+to)?"
    r"|Substancje[ \t]+pomocnicze(?:[ \t]+to)?)[ \t]*:?", re.I)
RE_LEAFLET_NONE = re.compile(r"nie[ \t]+zawiera[ \t]+(?:żadnych[ \t]+)?substancji[ \t]+pomocniczych", re.I)
# Dopiski po liście: odesłania do punktu 2, osmolalność, zawartość etanolu itp.
RE_LEAFLET_STOP = re.compile(r"^\(?\s*(?:patrz\b|lek\b.*\bzawiera\b|produkt\b.*\bzawiera\b|osmolalno|ph\s*[:≈~]"
                             r"|wartość\s+ph|ekstrahent|substancj\w*\s+czynn)", re.I)
RE_BULLET = re.compile(r"^[ \t]*[-‒–−•\uf0b7\u2022\u25aa\u25cf*]?[ \t]*(?=\S)", re.M)
RE_FOOTNOTE_LINE = re.compile(r"^[ \t]*(\*+)[ \t]*([^:\n]+):[ \t]*(.*)$")
RE_AMOUNT = re.compile(r"[ \t]*[–-][ \t]*\d+(?:[.,]\d+)?[ \t]*(?:mg|g|µg|mcg|ml)\b", re.I)


def leaflet_section(text: str) -> tuple[str | None, str]:
    """Wycina listę substancji pomocniczych z punktu 6 ulotki.
    Zwraca (tekst listy albo None, kod powodu, gdy listy brak)."""
    start = None
    for m in RE_LEAFLET_START.finditer(text):
        start = m                                    # ostatnie "Co zawiera" = punkt 6 (wcześniej bywa w spisie treści)
    if start is None:
        return None, NO_LEAFLET_SECTION
    end = RE_LEAFLET_END.search(text, start.end())
    block = text[start.end(): end.start() if end and end.start() - start.end() < MAX_SECTION_CHARS
                 else start.end() + MAX_SECTION_CHARS]
    lead = RE_LEAFLET_LEAD.search(block)
    if lead is None:
        return None, ("BRAK" if RE_LEAFLET_NONE.search(block) else NO_EXCIPIENT_LIST)
    body = block[lead.end():]

    # przypisy "* Aromat pomarańczowy: maltodekstryna, …" -> skład w nawiasie przy składniku z gwiazdką
    notes: dict[str, str] = {}
    kept: list[str] = []
    current = None
    for line in body.splitlines():
        m = RE_FOOTNOTE_LINE.match(line)
        if m:
            current = m.group(1)
            notes[current] = m.group(3).strip()
        elif current and line.strip() and not RE_LEAFLET_STOP.match(line.strip()):
            notes[current] += " " + line.strip()            # ciąg dalszy przypisu w następnej linii
        else:
            current = None
            kept.append(line)
    if notes:
        body = "\n".join(kept)
        notes = {k: re.sub(r"\s+", " ", v).strip(" .") for k, v in notes.items()}
        for stars in sorted(notes, key=len, reverse=True):
            body = re.sub(rf"(?<=[\w)])[ \t]?{re.escape(stars)}(?!\*)", f" ({notes[stars]})", body, count=1)

    lines: list[str] = []
    for line in RE_BULLET.sub("", RE_PAGE_LINE.sub("", body)).splitlines():
        line = line.strip()
        if not line:
            continue
        if RE_LEAFLET_STOP.match(line):
            break
        prev = lines[-1] if lines else ""
        # nazwa przełamana między liniami: poprzednia linia nie kończy się znakiem zamykającym,
        # a jest częścią listy po przecinkach albo następna zaczyna się małą literą
        if prev and (prev.endswith(",") or (not prev.endswith((".", ";", ":"))
                                            and ("," in prev or line[:1].islower()))):
            lines[-1] = f"{prev} {line}"
        else:
            lines.append(line)
    out = []
    for line in lines:
        line = RE_AMOUNT.sub("", line).replace("[", "(").replace("]", ")")
        line = re.sub(r"\s+lub\s+", ", ", line)                 # "sodu wodorotlenek lub kwas solny"
        line = re.sub(r"(\(E[ \t]?\d{3,4}[a-z]?\))[ \t]+(?=[a-ząćęłńóśźż])", r"\1, ", line)  # brak przecinka po (E 551)
        line = re.sub(r"\(([^()]*)\)", lambda m: "(" + re.sub(r"(?<!-)\s+i\s+", ", ", m.group(1)) + ")", line)
        parts = split_top_level(line, ",")
        if len(parts) >= 2:                                     # "…, glicerol, sodu wodorotlenek i woda do wstrzykiwań"
            last = re.split(r"(?<!-)\s+i\s+", parts[-1])
            if len(last) == 2 and "(" not in parts[-1]:
                line = ", ".join(parts[:-1] + last)
        out.extend(seg.strip() + ";" for seg in line.split(";") if seg.strip())   # "…; otoczka: …" -> osobne linie
    return "\n".join(l.rstrip(";") if i == len(out) - 1 else l for i, l in enumerate(out)).strip(), ""


def parse_leaflet(text: str, tables: Tables) -> Result:
    """Substancje pomocnicze z punktu 6 ulotki (dla leków, których rejestr nie udostępnia ChPL)."""
    if len(text.strip()) < MIN_TEXT_CHARS:
        r = Result()
        r.flag(NO_TEXT)
        return r
    body, reason = leaflet_section(text)
    if body is None:
        r = Result()
        if reason == "BRAK":
            r.brak_substancji = True
        else:
            r.flag(reason)
        return r
    return parse_section(body, tables)


def parse_pdf(pdf: bytes | Path, tables: Tables, kind: str = "chpl") -> tuple[Result, str]:
    """Zwraca (wynik, cały tekst PDF). kind: "chpl" (także informacja o produkcie EMA) albo "ulotka"."""
    try:
        text = extract_text(pdf)
    except Exception as exc:  # uszkodzony lub zaszyfrowany plik
        r = Result()
        r.flag(PDF_ERROR)
        r.blad = f"{exc.__class__.__name__}: {exc}"[:300]   # nie trafia na listę nieznanych nazw
        return r, ""
    return (parse_leaflet(text, tables) if kind == "ulotka" else parse_text(text, tables)), text


def skladniki_dla_mocy(result: Result, moc: str | None) -> list[Excipient] | None:
    """Skład dla rekordu z rejestru o danej mocy (pole medicinalProductPower).
    None = nie da się przypisać automatycznie, rekord trafia do weryfikacji."""
    d = norm_dose(moc or "")
    if result.dawki_z_naglowkow and d not in result.dawki_z_naglowkow:
        return None
    return [ex for ex in result.substancje if ex.dawki is None or d in ex.dawki]
