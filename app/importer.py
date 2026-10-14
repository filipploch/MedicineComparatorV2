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
import hashlib
import io
import logging
import re
import time
import traceback
from pathlib import Path
from typing import Callable, Iterator

import requests
from flask import current_app

from . import db
from .chpl_parser import (NO_SECTION_END, REASON_LABELS, Result, Tables, parse_pdf,
                          parse_section, skladniki_dla_mocy)
from .codes import gtin_check_ok, to_gtin14
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

    def get(self, url: str, **kw) -> requests.Response:
        self._wait()
        return self.http.get(url, timeout=120, **kw)

    def head(self, url: str) -> requests.Response:
        self._wait()
        return self.http.head(url, timeout=30, allow_redirects=True)


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
    if product.chpl_url is None and not product.manual:
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
    """Przypisuje rekordowi skład z jego ChPL (z uwzględnieniem mocy)."""
    if product.manual:
        if doc_changed:  # skład ręczny zostaje, ale redaktor musi go potwierdzić
            product.status, product.review_reason = REVIEW, "ChPL zmienił się po ręcznej edycji składu"
        return
    if product.chpl_url is None:
        product.status, product.review_reason, product.excipients = NO_CHPL, None, []
        return
    doc = product.chpl
    if doc is None or not doc.parse_result:
        product.status, product.review_reason = REVIEW, "nie udało się pobrać ChPL"
        product.excipients = []
        return
    result = Result.from_dict(doc.parse_result)
    if result.status != "OK":
        product.status, product.review_reason = REVIEW, reasons_text(result.powody)
        product.excipients = []
        return
    items = skladniki_dla_mocy(result, product.power)
    if items is None:
        product.status, product.review_reason = REVIEW, REASON_LABELS["MOC_NIEDOPASOWANA"]
        product.excipients = []
        return
    product.excipients = [excipient_by_name(i.nazwa) for i in items]
    product.status, product.review_reason = OK, None


def store_document(content: bytes, url: str, headers, tables: Tables) -> ChplDocument:
    sha = hashlib.sha256(content).hexdigest()
    doc = ChplDocument.query.filter_by(sha256=sha).first()
    if doc is not None:
        return doc
    result, _text = parse_pdf(content, tables)
    doc = ChplDocument(sha256=sha, size=len(content), source_url=url, file_name=file_name(headers),
                       raw_section=result.surowy_6_1 or None, parse_status=result.status,
                       parse_result=result.to_dict())
    db.session.add(doc)
    db.session.flush()
    return doc


def check_chpl(product: Product, client: Client, tables: Tables, stats: dict) -> None:
    url = product.chpl_url
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
        apply_composition(product)
        stats["chpl_bledy"] += 1
        return
    r.raise_for_status()
    old_doc_id = product.chpl_id
    doc = store_document(r.content, url, r.headers, tables)
    product.chpl = doc
    product.chpl_etag = head_sig or signature(r.headers)
    changed = old_doc_id is not None and old_doc_id != doc.id
    if old_doc_id != doc.id or product.status == PENDING:
        apply_composition(product, doc_changed=changed)
    stats["chpl_zmienione" if changed else ("chpl_nowe" if old_doc_id is None else "chpl_bez_zmian")] += 1


def reparse_documents(only_review: bool = True) -> int:
    """Parsuje ponownie zapisane punkty 6.1 (np. po uzupełnieniu słowników przez redaktora)."""
    tables = tables_from_db()
    query = ChplDocument.query.filter(ChplDocument.raw_section.isnot(None))
    if only_review:
        query = query.filter(ChplDocument.parse_status != "OK")
    count = 0
    for doc in query.all():
        previous = Result.from_dict(doc.parse_result or {})
        result = parse_section(doc.raw_section, tables)
        if NO_SECTION_END in previous.powody:
            result.flag(NO_SECTION_END)
        doc.parse_status, doc.parse_result = result.status, result.to_dict()
        for product in doc.products:
            apply_composition(product)
        count += 1
    db.session.commit()
    return count


# --- przebieg importu ------------------------------------------------------------------
def run_import(limit: int | None = None, with_chpl: bool = True, csv_path: Path | None = None,
               progress: Callable[[str], None] = lambda msg: None) -> ImportRun:
    """Rejestr z CSV (z sieci albo z pliku), potem ChPL od leków sprawdzanych najdawniej,
    więc przerwany import kontynuuje przy następnym uruchomieniu."""
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
            tables = tables_from_db()
            todo = (Product.query.filter(Product.active.is_(True), Product.chpl_url.isnot(None),
                                         Product.id.in_(seen))
                    .order_by(Product.chpl_checked_at.is_(None).desc(), Product.chpl_checked_at).all())
            for n, product in enumerate(todo, 1):
                try:
                    check_chpl(product, client, tables, stats)
                except requests.RequestException as exc:
                    stats["chpl_bledy"] += 1
                    log.warning("ChPL %s: %s", product.id, exc)
                if n % 50 == 0:
                    db.session.commit()
                    progress(f"ChPL: {n}/{len(todo)}")
            db.session.commit()
        run.status = "ok"
    except Exception:
        db.session.rollback()
        run = db.session.get(ImportRun, run.id)
        run.status, run.error = "blad", traceback.format_exc()
    run.stats, run.finished_at = stats, now()
    db.session.commit()
    return run
