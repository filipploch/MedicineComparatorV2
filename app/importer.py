"""Cotygodniowy import: rejestr leków z API RPL + punkt 6.1 z ChPL.

Wykrywanie zmian ChPL (rozwinięcie pomysłu ze starego repo, które porównywało Content-Length):
  1. zapytanie HEAD -> "podpis" z nazwy pliku (Content-Disposition), rozmiaru, ETag i Last-Modified;
     podpis bez zmian = plik bez zmian, nic nie pobieramy;
  2. inaczej pobieramy PDF i liczymy SHA-256; ten sam plik pod kilkoma rekordami (moce) parsujemy raz.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import traceback
from typing import Callable, Iterator

import requests
from flask import current_app

from . import db
from .chpl_parser import (NO_SECTION_END, REASON_LABELS, Result, Tables, parse_pdf,
                          parse_section, skladniki_dla_mocy)
from .models import (OK, PENDING, REVIEW, ChplDocument, ENumber, Excipient, IgnoredLabel,
                     ImportRun, Premix, Product, Synonym, now)
from .power import group_key

log = logging.getLogger(__name__)
USER_AGENT = "porownywarka-substancji-pomocniczych/1.0 (import cotygodniowy)"


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
        return self.http.get(url, timeout=60, **kw)

    def head(self, url: str) -> requests.Response:
        self._wait()
        return self.http.head(url, timeout=30, allow_redirects=True)


def signature(headers) -> str | None:
    """Podpis pliku z nagłówków HTTP; None, gdy serwer nic użytecznego nie podaje."""
    disposition = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
    parts = [m.group(1) if m else "", headers.get("ETag", ""), headers.get("Last-Modified", "")]
    if not any(parts):
        return None
    return "|".join(parts + [headers.get("Content-Length", "")])


def file_name(headers) -> str | None:
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", headers.get("Content-Disposition", ""), re.I)
    return m.group(1) if m else None


# --- rejestr --------------------------------------------------------------------------
def fetch_registry(client: Client, limit: int | None = None) -> Iterator[dict]:
    cfg = current_app.config
    page, seen = 0, 0
    while True:
        params = {"page": page, "size": cfg["RPL_PAGE_SIZE"], "specimenTypeEnum": "L", "isAdvancedSearch": "false"}
        r = client.get(cfg["RPL_SEARCH_URL"], params=params)
        r.raise_for_status()
        data = json.loads(r.content.decode("utf-8"))  # serwer nie podaje kodowania
        for item in data.get("content", []):
            if item.get("specimenType", "Ludzki") != "Ludzki":
                continue
            yield item
            seen += 1
            if limit and seen >= limit:
                return
        if data.get("last", True):
            return
        page += 1


def upsert_product(item: dict) -> Product:
    product = db.session.get(Product, item["id"])
    if product is None:
        product = Product(id=item["id"], status=PENDING)
        db.session.add(product)
    product.name = item.get("medicinalProductName") or ""
    product.common_name = item.get("commonName")
    product.power = item.get("medicinalProductPower")
    product.form = item.get("pharmaceuticalFormName")
    product.holder = item.get("subjectMedicinalProductName")
    product.registry_number = item.get("registryNumber")
    product.atc = item.get("atcCode") or None
    product.group_key = group_key(product.common_name, product.power)
    product.active = True
    return product


# --- skład leku -------------------------------------------------------------------------
def reasons_text(codes: list[str]) -> str:
    return "; ".join(REASON_LABELS.get(c, c) for c in codes)


def apply_composition(product: Product, doc_changed: bool = False) -> None:
    """Przypisuje rekordowi skład z jego ChPL (z uwzględnieniem mocy)."""
    if product.manual:
        if doc_changed:  # skład ręczny zostaje, ale redaktor musi go potwierdzić
            product.status, product.review_reason = REVIEW, "ChPL zmienił się po ręcznej edycji składu"
        return
    doc = product.chpl
    if doc is None or not doc.parse_result:
        product.status, product.review_reason = REVIEW, "brak ChPL"
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
    url = current_app.config["RPL_CHPL_URL"].format(id=product.id)
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
        stats["chpl_brak"] += 1
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
def run_import(limit: int | None = None, with_chpl: bool = True,
               progress: Callable[[str], None] = lambda msg: None) -> ImportRun:
    run = ImportRun(status="trwa", stats={})
    db.session.add(run)
    db.session.commit()
    stats = {k: 0 for k in ("leki", "leki_nowe", "leki_wycofane", "chpl_nowe", "chpl_zmienione",
                            "chpl_bez_zmian", "chpl_brak", "chpl_bledy")}
    client = Client(current_app.config["RPL_DELAY_SECONDS"])
    try:
        seen: set[int] = set()
        for item in fetch_registry(client, limit):
            is_new = db.session.get(Product, item["id"]) is None
            upsert_product(item)
            seen.add(item["id"])
            stats["leki"] += 1
            stats["leki_nowe"] += is_new
        if limit is None:  # pełna lista: czego nie ma w rejestrze, oznaczamy jako wycofane
            for product in Product.query.filter(Product.active.is_(True)):
                if product.id not in seen:
                    product.active = False
                    stats["leki_wycofane"] += 1
        db.session.commit()
        progress(f"Rejestr: {stats['leki']} leków ({stats['leki_nowe']} nowych)")

        if with_chpl:
            tables = tables_from_db()
            todo = (Product.query.filter(Product.active.is_(True), Product.id.in_(seen))
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
