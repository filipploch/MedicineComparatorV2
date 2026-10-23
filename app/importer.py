"""Cotygodniowy import: rejestr leków z pliku CSV RPL + punkt 6.1 z ChPL.

Rejestr: jeden plik CSV z rejestry.ezdrowie.gov.pl (ok. 16 MB) zawiera wszystkie leki z mocą, postacią,
kodami GTIN opakowań i linkami do ChPL i ulotki. Leki dopuszczone centralnie (EMA) i z importu
równoległego nie mają w rejestrze ChPL; dostają status "brak ChPL" zamiast trafiać do kolejki redaktora.

Wykrywanie zmian ChPL (rozwinięcie pomysłu ze starego repo, które porównywało Content-Length):
  1. zapytanie HEAD -> "podpis" z nazwy pliku (Content-Disposition), rozmiaru, ETag i Last-Modified;
     podpis bez zmian = plik bez zmian, nic nie pobieramy;
  2. inaczej pobieramy PDF i liczymy SHA-256; ten sam plik pod kilkoma rekordami (moce) parsujemy raz.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
import time
import traceback
from pathlib import Path
from typing import Callable, Iterator

import requests
from flask import current_app

from . import db
from .chpl_parser import (REASON_LABELS, Result, Tables, eu_base, eu_numbers, parse_pdf, reparse,
                          skladniki_dla_mocy, wybierz_czesc)
from .codes import gtin_check_ok, to_gtin14
from sqlalchemy import or_

from .models import (NO_CHPL, OK, PENDING, REVIEW, ChplDocument, ENumber, Excipient, Gtin,
                     IgnoredLabel, ImportRun, Premix, Product, Synonym, now)
from .power import group_key

log = logging.getLogger(__name__)
USER_AGENT = "porownywarka-substancji-pomocniczych/1.0 (import cotygodniowy)"

# Kolumny pliku CSV RPL
COL_ID = "Identyfikator Produktu Leczniczego"
COL_NAME = "Nazwa Produktu Leczniczego"
COL_COMMON = "Nazwa powszechnie stosowana"
COL_KIND = "Rodzaj preparatu"
COL_POWER = "Moc"
COL_FORM = "Postać farmaceutyczna"
COL_PROCEDURE = "Typ procedury"
COL_REG_NO = "Numer pozwolenia"
COL_ATC = "Kod ATC"
COL_HOLDER = "Podmiot odpowiedzialny"
COL_PACKAGES = "Opakowanie"
COL_CHPL = "Charakterystyka"
LEAFLET_COLS = ("Ulotka", "Etykieto-ulotka", "Ulotka importu równoległego", "Etykieto-ulotka importu równoległego")
RE_RPL_ID = re.compile(r"/medicinal-products/(\d+)/")
RE_PACKAGE = re.compile(r"^\s*(\d{8,14})\s*¦(.*)$")


# --- słowniki z bazy -------------------------------------------------------------
def tables_from_db() -> Tables:
    substances: dict[str, list[str]] = {e.name: [] for e in Excipient.query.all()}
    for s in Synonym.query.all():
        substances.setdefault(s.excipient.name, []).append(s.text)
    return Tables.build(
        ignored=[l.text for l in IgnoredLabel.query.all()],
        substances=substances,
        e_numbers={e.code: e.excipient.name for e in ENumber.query.all()},
        premixes={p.name: p.components for p in Premix.query.all()},
    )


def excipient_by_name(name: str) -> Excipient:
    ex = Excipient.query.filter_by(name=name).first()
    if ex is None:
        ex = Excipient(name=name)
        db.session.add(ex)
        db.session.flush()
    return ex


# --- HTTP ---------------------------------------------------------------------------
class Client:
    def __init__(self, delay: float):
        self.delay = delay
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self._last = 0.0

    def _wait(self) -> None:
        pause = self.delay - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()

    RETRY_PAUSES = (5, 15)     # sekundy przed 2. i 3. próbą przy zerwanym połączeniu

    def _retry(self, call):
        for attempt in range(len(self.RETRY_PAUSES) + 1):
            self._wait()
            try:
                return call()
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                if attempt == len(self.RETRY_PAUSES):
                    raise
                log.info("ponawiam po błędzie połączenia: %s", exc)
                time.sleep(self.RETRY_PAUSES[attempt])

    def get(self, url: str, **kw) -> requests.Response:
        return self._retry(lambda: self.http.get(url, timeout=120, **kw))

    def head(self, url: str) -> requests.Response:
        return self._retry(lambda: self.http.head(url, timeout=30, allow_redirects=True))


RE_FILENAME = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", re.I)


def signature(headers) -> str | None:
    """Podpis pliku z nagłówków HTTP; None, gdy serwer nic użytecznego nie podaje."""
    m = RE_FILENAME.search(headers.get("Content-Disposition", ""))
    parts = [m.group(1) if m else "", headers.get("ETag", ""), headers.get("Last-Modified", "")]
    if not any(parts):
        return None
    return "|".join(parts + [headers.get("Content-Length", "")])


def file_name(headers) -> str | None:
    m = RE_FILENAME.search(headers.get("Content-Disposition", ""))
    return m.group(1) if m else None


# --- rejestr (CSV) ----------------------------------------------------------------------
def read_registry_csv(text: str) -> Iterator[dict]:
    """Wiersze leków dla ludzi z pliku CSV RPL (separator ";", pola w cudzysłowach)."""
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";"):
        if row.get(COL_KIND, "Ludzki") == "Ludzki" and row.get(COL_ID, "").strip().isdigit():
            yield row


def download_registry(client: Client) -> str:
    r = client.get(current_app.config["RPL_CSV_URL"])
    r.raise_for_status()
    return r.content.decode("utf-8")


def parse_packages(text: str | None) -> list[tuple[str, bool, str]]:
    """Kolumna "Opakowanie": na zmianę linia "GTIN ¦ kategoria ¦ [Skasowane ¦] id" i opis opakowania.
    Zwraca (GTIN-14, czy skasowane, opis)."""
    lines = (text or "").splitlines()
    out: list[tuple[str, bool, str]] = []
    for i, line in enumerate(lines):
        m = RE_PACKAGE.match(line)
        if not m or not gtin_check_ok(m.group(1)):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) and not RE_PACKAGE.match(lines[i + 1]) else ""
        out.append((to_gtin14(m.group(1)), "Skasowane" in m.group(2), nxt[:200]))
    return out


def set_gtins(product: Product, packages: list[tuple[str, bool, str]]) -> None:
    wanted = {code: (deleted, desc) for code, deleted, desc in packages}
    for g in list(product.gtins):
        if g.code not in wanted:
            product.gtins.remove(g)
    current = {g.code: g for g in product.gtins}
    for code, (deleted, desc) in wanted.items():
        g = current.get(code) or db.session.get(Gtin, code)
        if g is None:
            g = Gtin(code=code)
        g.product, g.deleted, g.package = product, deleted, desc   # kod mógł przejść do innego rekordu
        db.session.add(g)   # w SQLAlchemy 2 przypisanie relacji nie dodaje obiektu do sesji


def first_url(row: dict, cols) -> str | None:
    for col in cols:
        if (row.get(col) or "").strip():
            return row[col].strip()
    return None


def upsert_product(row: dict) -> Product:
    pid = int(row[COL_ID])
    product = db.session.get(Product, pid)
    if product is None:
        product = Product(id=pid, status=PENDING)
        db.session.add(product)
    product.name = row.get(COL_NAME) or ""
    product.common_name = row.get(COL_COMMON) or None
    product.power = row.get(COL_POWER) or None
    product.form = row.get(COL_FORM) or None
    product.holder = (row.get(COL_HOLDER) or "").splitlines()[0] if row.get(COL_HOLDER) else None
    product.registry_number = row.get(COL_REG_NO) or None
    product.atc = row.get(COL_ATC) or None
    product.procedure = row.get(COL_PROCEDURE) or None
    product.chpl_url = first_url(row, [COL_CHPL])
    product.leaflet_url = first_url(row, LEAFLET_COLS)
    m = RE_RPL_ID.search(product.chpl_url or product.leaflet_url or "")
    product.rpl_id = int(m.group(1)) if m else None
    product.group_key = group_key(product.common_name, product.power)
    product.active = True
    set_gtins(product, parse_packages(row.get(COL_PACKAGES)))
    product.eu_numbers = eu_numbers(row.get(COL_PACKAGES) or "")
    if product.source_url is None and not product.manual:
        product.status, product.review_reason, product.excipients = NO_CHPL, None, []
    return product


def load_registry(text: str, limit: int | None = None, mark_missing: bool = True) -> dict:
    """Wczytuje rejestr z tekstu CSV. Leki nieobecne w pełnym pliku są oznaczane jako wycofane."""
    stats = {"leki": 0, "leki_nowe": 0, "leki_wycofane": 0, "leki_bez_chpl": 0}
    seen: set[int] = set()
    for row in read_registry_csv(text):
        is_new = db.session.get(Product, int(row[COL_ID])) is None
        product = upsert_product(row)
        seen.add(product.id)
        stats["leki"] += 1
        stats["leki_nowe"] += is_new
        stats["leki_bez_chpl"] += product.chpl_url is None
        if limit and stats["leki"] >= limit:
            break
    if mark_missing and not limit:
        for product in Product.query.filter(Product.active.is_(True)):
            if product.id not in seen:
                product.active = False
                stats["leki_wycofane"] += 1
    db.session.commit()
    stats["_seen"] = seen
    return stats


# --- skład leku -------------------------------------------------------------------------
def reasons_text(codes: list[str]) -> str:
    return "; ".join(REASON_LABELS.get(c, c) for c in codes)


def apply_composition(product: Product, doc_changed: bool = False) -> None:
    """Przypisuje rekordowi skład z jego ChPL (właściwa charakterystyka i moc)."""
    if product.manual:
        if doc_changed:  # skład ręczny zostaje, ale redaktor musi go potwierdzić
            product.status, product.review_reason = REVIEW, "ChPL zmienił się po ręcznej edycji składu"
        return

    def review(reason: str) -> None:
        product.status, product.review_reason, product.excipients = REVIEW, reason, []

    if product.source_url is None:
        product.status, product.review_reason, product.excipients = NO_CHPL, None, []
        return
    doc = product.chpl
    if doc is None or not doc.parse_result:
        return review("nie udało się pobrać ChPL")
    result = Result.from_dict(doc.parse_result)
    if product.source == "EMA" and product.eu_numbers and result.eu and \
            not {eu_base(e) for e in product.eu_numbers} & {eu_base(e) for e in result.eu}:
        return review("dokument EMA dotyczy innego leku (numery pozwoleń EU się nie zgadzają)")
    part = wybierz_czesc(result, product.eu_numbers or [], product.power)
    if part is None:
        return review("nie udało się wskazać w dokumencie charakterystyki tego leku")
    if part.status != "OK":
        return review(reasons_text(part.powody))
    items = skladniki_dla_mocy(part, product.power)
    if items is None:
        return review(REASON_LABELS["MOC_NIEDOPASOWANA"])
    product.excipients = [excipient_by_name(i.nazwa) for i in items]
    product.status, product.review_reason = OK, None


def store_document(content: bytes, url: str, headers, tables: Tables, kind: str = "chpl") -> ChplDocument:
    sha = hashlib.sha256(content).hexdigest()
    doc = ChplDocument.query.filter_by(sha256=sha).first()
    if doc is not None:
        return doc
    result, _text = parse_pdf(content, tables, kind)
    doc = ChplDocument(sha256=sha, kind=kind, size=len(content), source_url=url, file_name=file_name(headers),
                       raw_section=result.surowy_6_1 or None, parse_status=result.status,
                       parse_result=result.to_dict())
    db.session.add(doc)
    db.session.flush()
    return doc


def check_chpl(product: Product, client: Client, tables: Tables, stats: dict) -> None:
    url = product.source_url
    if not url:
        apply_composition(product)
        return
    head_sig = None
    try:
        head = client.head(url)
        if head.ok:
            head_sig = signature(head.headers)
    except requests.RequestException:
        pass
    if head_sig and product.chpl is not None and head_sig == product.chpl_etag:
        product.chpl_checked_at = now()
        stats["chpl_bez_zmian"] += 1
        return

    r = client.get(url)
    product.chpl_checked_at = now()
    if r.status_code == 404:
        product.chpl = None
        if product.source == "EMA":       # brak polskiej informacji o produkcie pod tym adresem
            product.ema_url = None
        apply_composition(product)
        stats["chpl_bledy"] += 1
        return
    r.raise_for_status()
    old_doc_id = product.chpl_id
    doc = store_document(r.content, url, r.headers, tables, product.document_kind)
    product.chpl = doc
    product.chpl_etag = head_sig or signature(r.headers)
    changed = old_doc_id is not None and old_doc_id != doc.id
    if old_doc_id != doc.id or product.status == PENDING:
        apply_composition(product, doc_changed=changed)
    stats["chpl_zmienione" if changed else ("chpl_nowe" if old_doc_id is None else "chpl_bez_zmian")] += 1


def reparse_documents(only_review: bool = True, mentioning: str | None = None, unknown: str | None = None) -> int:
    """Parsuje ponownie zapisane punkty 6.1 (np. po uzupełnieniu słowników przez redaktora).
    mentioning: tylko dokumenty, których wynik zawiera tę nazwę substancji (po scaleniu substancji);
    unknown: tylko dokumenty z tą nazwą na liście nieznanych (po decyzji redaktora w sprawie tej nazwy)."""
    tables = tables_from_db()
    query = ChplDocument.query.filter(ChplDocument.raw_section.isnot(None))
    if only_review:
        query = query.filter(ChplDocument.parse_status != "OK")
    count = 0
    for doc in query.all():
        result = doc.parse_result or {}
        if unknown is not None and unknown not in result.get("nieznane", []):
            continue
        if mentioning is not None and mentioning not in json.dumps(result, ensure_ascii=False):
            continue
        result = reparse(doc.parse_result, doc.raw_section, tables)
        doc.parse_status, doc.parse_result = result.status, result.to_dict()
        for product in doc.products:
            apply_composition(product)
        count += 1
    db.session.commit()
    return count


def merge_excipients(source: Excipient, target: Excipient) -> int:
    """Scala substancję dodaną omyłkowo jako nową z istniejącą: synonimy, numery E, leki i mieszanki
    przechodzą na docelową, a nazwa scalanej zostaje jej synonimem. Zwraca liczbę zmienionych leków."""
    from .chpl_parser import norm_key
    if source.id == target.id:
        raise ValueError("nie można scalić substancji z nią samą")
    old_name = source.name
    for syn in list(source.synonyms):
        syn.excipient = target
    key = norm_key(old_name)
    if key != norm_key(target.name) and not Synonym.query.filter_by(text=key).first():
        db.session.add(Synonym(text=key, excipient=target))
    for e in ENumber.query.filter_by(excipient_id=source.id):
        e.excipient = target
    changed = 0
    for product in Product.query.filter(Product.excipients.any(Excipient.id == source.id)):
        items = [e for e in product.excipients if e.id != source.id]
        if target not in items:
            items.append(target)
        product.excipients = items                     # także skład wpisany ręcznie
        changed += 1
    for premix in Premix.query.all():
        comps = [target.name if norm_key(c) == key else c for c in premix.components]
        if comps != premix.components:
            premix.components = comps
    db.session.flush()
    db.session.delete(source)
    db.session.commit()
    reparse_documents(only_review=False, mentioning=old_name)   # wyniki parsera z nową nazwą
    return changed


# --- Europejska Agencja Leków ------------------------------------------------------------
def ema_key(name: str) -> str:
    return re.sub(r"[^0-9a-ząćęłńóśźżäöüéèàç]+", " ", (name or "").casefold()).strip()


def ema_index(data: dict) -> dict[str, dict]:
    """Leki dopuszczone centralnie z pliku JSON EMA: klucz nazwy -> adres strony i numer produktu."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("nieoczekiwany format pliku leków EMA")
    index: dict[str, dict] = {}
    for rec in data["data"]:
        if not isinstance(rec, dict) or rec.get("category") != "Human" or rec.get("medicine_status") != "Authorised":
            continue
        slug = (rec.get("medicine_url") or "").rstrip("/").rsplit("/", 1)[-1]
        if slug and rec.get("name_of_medicine"):
            index[ema_key(rec["name_of_medicine"])] = {"slug": slug, "number": rec.get("ema_product_number")}
    return index


def match_ema(name: str, index: dict[str, dict]) -> dict | None:
    """Dokładna nazwa, a w drugiej kolejności najdłuższa nazwa EMA będąca początkiem nazwy z rejestru
    ("Comirnaty" dla "Comirnaty Original/Omicron BA.4-5"). Dopasowanie potwierdzają potem numery EU."""
    words = ema_key(name).split()
    for n in range(len(words), 0, -1):
        hit = index.get(" ".join(words[:n]))
        if hit:
            return hit
    return None


def sync_ema(client: Client, stats: dict) -> None:
    """Dla leków centralnych bez ChPL w rejestrze ustala adres polskiej informacji o produkcie EMA."""
    cfg = current_app.config
    r = client.get(cfg["EMA_MEDICINES_URL"])
    r.raise_for_status()
    index = ema_index(json.loads(r.content.decode("utf-8")))
    stats.update(ema_dopasowane=0, ema_niedopasowane=0)
    for product in Product.query.filter(Product.active.is_(True), Product.chpl_url.is_(None),
                                        Product.procedure == "CEN"):
        hit = match_ema(product.name, index)
        url = cfg["EMA_PRODUCT_INFO_URL"].format(slug=hit["slug"]) if hit else None
        stats["ema_dopasowane" if hit else "ema_niedopasowane"] += 1
        if url == product.ema_url:
            continue
        product.ema_url, product.ema_product_number = url, (hit or {}).get("number")
        product.chpl, product.chpl_etag = None, None          # nowe źródło: dokument pobierzemy od nowa
        if product.manual:
            continue
        if product.source_url is None:
            product.status, product.review_reason, product.excipients = NO_CHPL, None, []
        else:                                                   # EMA albo, gdy dopasowanie zniknęło, ulotka
            product.status = PENDING
    db.session.commit()


# --- przebieg importu ------------------------------------------------------------------
def run_import(limit: int | None = None, with_chpl: bool = True, csv_path: Path | None = None,
               check_all: bool = False, progress: Callable[[str], None] = lambda msg: None) -> ImportRun:
    """Rejestr z CSV (z sieci albo z pliku), potem dokumenty leków.

    Wznawianie: każdy sprawdzony lek jest zapisywany od razu, a kolejne uruchomienie pomija leki sprawdzone
    w ciągu ostatnich CHPL_RECHECK_DAYS dni (domyślnie 6), więc przerwany import kontynuuje od miejsca przerwania,
    a cotygodniowy import i tak sprawdza wszystko. check_all=True sprawdza wszystkie leki bez względu na datę."""
    run = ImportRun(status="trwa", stats={})
    db.session.add(run)
    db.session.commit()
    stats = {k: 0 for k in ("chpl_nowe", "chpl_zmienione", "chpl_bez_zmian", "chpl_bledy")}
    client = Client(current_app.config["RPL_DELAY_SECONDS"])
    try:
        text = csv_path.read_text(encoding="utf-8") if csv_path else download_registry(client)
        reg = load_registry(text, limit=limit)
        seen = reg.pop("_seen")
        stats.update(reg)
        progress(f"Rejestr: {reg['leki']} leków ({reg['leki_nowe']} nowych, {reg['leki_bez_chpl']} bez ChPL)")

        if with_chpl:
            try:
                sync_ema(client, stats)
                progress(f"EMA: {stats['ema_dopasowane']} leków centralnych z informacją o produkcie")
            except (requests.RequestException, ValueError) as exc:   # brak EMA nie zatrzymuje importu
                stats["ema_blad"] = str(exc)[:200]
                db.session.rollback()
            tables = tables_from_db()
            query = Product.query.filter(Product.active.is_(True),
                                         or_(Product.chpl_url.isnot(None), Product.ema_url.isnot(None),
                                             Product.leaflet_url.isnot(None)),
                                         Product.id.in_(seen))
            total = query.count()
            if not check_all:
                cutoff = now() - dt.timedelta(days=current_app.config["CHPL_RECHECK_DAYS"])
                query = query.filter(or_(Product.chpl_checked_at.is_(None), Product.chpl_checked_at < cutoff))
            todo = query.order_by(Product.chpl_checked_at.is_(None).desc(), Product.chpl_checked_at).all()
            stats["chpl_pominiete_niedawno_sprawdzone"] = total - len(todo)
            progress(f"Dokumenty do sprawdzenia: {len(todo)} z {total} "
                     f"(pominięto {total - len(todo)} sprawdzonych w ciągu ostatnich "
                     f"{current_app.config['CHPL_RECHECK_DAYS']} dni)")
            for n, product in enumerate(todo, 1):
                pid = product.id
                try:
                    check_chpl(product, client, tables, stats)
                    db.session.commit()                         # postęp zapisany po każdym leku
                except requests.RequestException as exc:        # sieć: lek zostaje do sprawdzenia przy następnym razie
                    db.session.rollback()
                    stats["chpl_bledy"] += 1
                    log.warning("ChPL %s: %s", pid, exc)
                    progress(f"Błąd pobierania (lek {pid}), spróbuję przy następnym imporcie: {exc}")
                except Exception as exc:                        # nietypowy plik nie zatrzymuje importu
                    db.session.rollback()
                    stats["chpl_bledy"] += 1
                    log.exception("ChPL %s", pid)
                    product = db.session.get(Product, pid)
                    product.chpl_checked_at = now()
                    if not product.manual:
                        product.status, product.review_reason = REVIEW, f"błąd przetwarzania dokumentu: {exc}"[:300]
                    db.session.commit()
                if n % 50 == 0:
                    progress(f"Dokumenty: {n}/{len(todo)} w tym przebiegu")
        run.status = "ok"
    except KeyboardInterrupt:                                   # Ctrl+C: zapisany postęp zostaje
        db.session.rollback()
        run = db.session.get(ImportRun, run.id)
        run.status, run.error = "przerwany", "Przerwano ręcznie; kolejne uruchomienie kontynuuje od tego miejsca."
    except Exception:
        db.session.rollback()
        run = db.session.get(ImportRun, run.id)
        run.status, run.error = "blad", traceback.format_exc()
    run.stats, run.finished_at = stats, now()
    db.session.commit()
    return run
