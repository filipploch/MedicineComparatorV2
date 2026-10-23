"""Przebieg całości bez sieci: import (z udawanym serwerem RPL), strony publiczne, panel redaktora."""
import csv
import io
import json
import re

import pytest

from app import db, importer
from app.chpl_parser import parse_leaflet, parse_text
from app.models import NO_CHPL, OK, REVIEW, Excipient, ImportRun, Product, Synonym, User

from .conftest import FIXTURES

DEMO_CSV = (FIXTURES / "rejestr_demo.csv").read_text(encoding="utf-8")
EMA_TEXT = (FIXTURES / "ema_informacja_o_produkcie_TEST.txt").read_text(encoding="utf-8")
EMA_MEDICINES = {"meta": {"total_records": 2}, "data": [
    {"category": "Human", "name_of_medicine": "Aprovel", "ema_product_number": "EMEA/H/C/000141",
     "medicine_status": "Authorised", "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/aprovel"},
    {"category": "Human", "name_of_medicine": "Withdrawn Example", "ema_product_number": "EMEA/H/C/000001",
     "medicine_status": "Withdrawn", "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/withdrawn"},
]}
IBUPROM, MIG, NUROFEN, APROVEL = 11104, 19752, 9487, "Aprovel"   # id z adresów ChPL w rejestrze
NUROFEN_IR = 46471     # import równoległy: w rejestrze tylko ulotka (id z adresu ulotki)
LEAFLET_TEXT = ("ULOTKA DLA PACJENTA\n" + "tekst ulotki " * 20 + "\n6. Zawartość opakowania i inne informacje\n"
                "Co zawiera lek Nurofen dla dzieci Forte\nSubstancją czynną leku jest ibuprofen.\n"
                "Pozostałe składniki to: maltitol ciekły, glicerol, sodu benzoesan (E 211), kwas cytrynowy,\n"
                "sodu cytrynian, sodu chlorek, sacharyna sodowa, guma ksantanowa, aromat truskawkowy*, woda oczyszczona.\n"
                "* Aromat truskawkowy: glikol propylenowy, maltodekstryna.\n"
                "Lek zawiera 1,95 g maltitolu w 5 ml.\nJak wygląda lek Nurofen dla dzieci Forte i co zawiera opakowanie\n")


def csv_with(rpl_ids=None, change=None):
    """Fragment pliku demonstracyjnego: wybrane wiersze, ewentualnie ze zmienionymi polami."""
    rows = list(csv.DictReader(io.StringIO(DEMO_CSV), delimiter=";"))
    if rpl_ids is not None:
        rows = [r for r in rows if any(f"/{i}/" in r["Charakterystyka"] + r["Ulotka importu równoległego"]
                                       for i in rpl_ids)
                or (APROVEL in rpl_ids and r["Nazwa Produktu Leczniczego"] == APROVEL)]
    for r in rows:
        for key, (rid, value) in (change or {}).items():
            if f"/{rid}/" in r["Charakterystyka"]:
                r[key] = value
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()), delimiter=";", quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def prod(rpl_id) -> Product:
    if rpl_id == APROVEL:
        return Product.query.filter_by(name=APROVEL).one()
    return Product.query.filter_by(rpl_id=rpl_id).one()


def status(rpl_id):
    p = prod(rpl_id)
    return p.status, {e.name for e in p.excipients}


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise importer.requests.HTTPError(self.status_code)


class FakeRPL:
    """Udaje plik CSV rejestru i pliki ChPL; plik ChPL = tekst punktu 6.1 z prawdziwego dokumentu."""

    def __init__(self, sections):
        self.registry = csv_with([IBUPROM, MIG, NUROFEN, APROVEL])
        self.files = {IBUPROM: sections["11104"], MIG: sections["mig_400"], NUROFEN: sections["nurofen_forte"],
                      "aprovel": EMA_TEXT, NUROFEN_IR: LEAFLET_TEXT}
        self.versions = {k: "v1" for k in self.files}
        self.ema = json.loads(json.dumps(EMA_MEDICINES))
        self.downloads = 0

    @staticmethod
    def key(url):
        """Plik ChPL z RPL (.../11104/characteristic) albo informacja o produkcie EMA (.../aprovel-epar-...)."""
        if "/product-information/" in url:
            return url.rsplit("/", 1)[-1].split("-epar-")[0]
        return int(re.search(r"/medicinal-products/(\d+)/", url).group(1))

    def client(self, delay):
        fake = self

        class Client:
            def get(self, url, **kw):
                if url.endswith("get-csv"):
                    return FakeResponse(content=fake.registry.encode("utf-8"))
                if "medicines_json" in url:
                    return FakeResponse(content=json.dumps(fake.ema).encode("utf-8"))
                key = fake.key(url)
                if key not in fake.files:
                    return FakeResponse(status=404)
                fake.downloads += 1
                return FakeResponse(content=fake.files[key].encode("utf-8"), headers=fake.headers(key))

            def head(self, url):
                key = fake.key(url)
                return FakeResponse(headers=fake.headers(key)) if key in fake.files else FakeResponse(status=404)
        return Client()

    def headers(self, pid):
        return {"Content-Disposition": f'attachment; filename="Charakterystyka-{pid}-{self.versions[pid]}.pdf"'}


@pytest.fixture()
def rpl(app, sections, monkeypatch):
    fake = FakeRPL(sections)
    monkeypatch.setattr(importer, "Client", fake.client)
    wrap = "6.1 Wykaz substancji pomocniczych\n{}\n6.2 Niezgodności farmaceutyczne\n" + "x" * 300

    def fake_parse(content, tables, kind="chpl"):
        text = content.decode("utf-8")
        if kind == "ulotka":
            return parse_leaflet(text, tables), ""
        return parse_text(text if text.startswith("ANEKS I") else wrap.format(text), tables), ""
    monkeypatch.setattr(importer, "parse_pdf", fake_parse)
    return fake


def test_import_and_change_detection(rpl):
    run = importer.run_import()
    assert run.status == "ok", run.error
    assert run.stats["leki"] == 4 and run.stats["leki_bez_chpl"] == 1 and run.stats["chpl_nowe"] == 4
    assert run.stats["ema_dopasowane"] == 1
    assert status(IBUPROM)[0] == OK and "sacharoza" in status(IBUPROM)[1]
    assert status(MIG)[0] == OK
    assert status(NUROFEN)[0] == REVIEW          # Nurofen: "tusz Opacode ..." spoza słownika
    # Aprovel 150 mg: brak ChPL w rejestrze, skład z informacji o produkcie EMA, część wskazana numerami EU
    assert prod(APROVEL).source == "EMA" and prod(APROVEL).ema_product_number == "EMEA/H/C/000141"
    assert status(APROVEL)[0] == OK and "krzemionka koloidalna bezwodna" in status(APROVEL)[1]
    assert "hypromeloza" not in status(APROVEL)[1]
    assert prod(IBUPROM).group_key == prod(NUROFEN).group_key
    assert {g.code for g in prod(MIG).gtins} == {"05909990657964", "05909990657971", "05909990657988"}

    downloads = rpl.downloads
    run = importer.run_import(check_all=True)                 # nic się nie zmieniło: same zapytania HEAD
    assert rpl.downloads == downloads and run.stats["chpl_bez_zmian"] == 4

    rpl.versions[MIG] = "v2"                    # nowa wersja ChPL MIG
    rpl.files[MIG] += "\nSacharoza"
    importer.run_import(check_all=True)
    assert "sacharoza" in status(MIG)[1]

    rpl.registry = csv_with([MIG, NUROFEN, APROVEL])   # Ibuprom Max wycofany z rejestru
    run = importer.run_import(check_all=True)
    assert run.stats["leki_wycofane"] == 1 and not prod(IBUPROM).active


def test_import_from_local_csv(app, tmp_path):
    path = tmp_path / "rpl.csv"
    path.write_text(DEMO_CSV, encoding="utf-8")
    run = importer.run_import(csv_path=path, with_chpl=False)
    assert run.status == "ok" and run.stats["leki"] == 17 and run.stats["leki_bez_chpl"] == 2


def test_manual_composition_survives_import(rpl, client):
    importer.run_import()
    login(client)
    nurofen = prod(NUROFEN).id
    r = client.post(f"/admin/lek/{nurofen}", data={"excipients": "Talk\nE 171\nNieznany składnik"})
    assert "Spoza słownika" in r.get_data(as_text=True)
    client.post(f"/admin/lek/{nurofen}", data={"excipients": "Talk\nE 171"})
    assert status(NUROFEN) == (OK, {"talk", "tytanu dwutlenek"})

    importer.run_import(check_all=True)                        # bez zmian w ChPL: skład ręczny zostaje
    assert status(NUROFEN) == (OK, {"talk", "tytanu dwutlenek"})
    rpl.versions[NUROFEN] = "v2"
    rpl.files[NUROFEN] += "\nTalk"
    importer.run_import(check_all=True)                        # ChPL się zmienił: skład zostaje, ale wraca do kolejki
    assert status(NUROFEN) == (REVIEW, {"talk", "tytanu dwutlenek"})


def login(client):
    user = User(username="redaktor")
    user.set_password("tajne-haslo")
    db.session.add(user)
    db.session.commit()
    client.post("/admin/login", data={"username": "redaktor", "password": "tajne-haslo"})


def test_public_pages(rpl, client):
    importer.run_import()
    page = client.get("/?q=ibu").get_data(as_text=True)
    assert "Ibuprom Max" in page
    page = client.get(f"/lek/{prod(IBUPROM).id}").get_data(as_text=True)
    assert "sacharoza" in page and "MIG" in page and "Nurofen Forte" in page
    page = client.get(f"/lek/{prod(IBUPROM).id}/porownanie").get_data(as_text=True)
    assert 'class="differs"' in page             # np. sacharoza jest w Ibuprom Max, a nie w MIG
    assert "skład w trakcie weryfikacji" in page   # Nurofen jeszcze w kolejce
    page = client.get(f"/lek/{prod(APROVEL).id}").get_data(as_text=True)
    assert "Europejską Agencję Leków" in page and "aprovel-epar-product-information_pl.pdf" in page
    page = client.get(f"/lek/{prod(IBUPROM).id}").get_data(as_text=True)
    assert "Źródło składu" in page and "Rejestr Produktów Leczniczych" in page
    compare = client.get(f"/lek/{prod(IBUPROM).id}/porownanie").get_data(as_text=True)
    assert "Źródło składu" in compare
    assert client.get("/lek/1").status_code == 404


def test_admin_requires_login_and_resolves_unknown(rpl, client):
    importer.run_import()
    assert client.get("/admin/").status_code == 302
    login(client)
    assert "do weryfikacji" in client.get("/admin/").get_data(as_text=True)
    assert "Aprovel" not in client.get("/admin/kolejka").get_data(as_text=True)   # brak ChPL to nie praca dla redaktora
    page = client.get("/admin/nieznane").get_data(as_text=True)
    assert "tusz opacode s-1-15094" in page

    client.post("/admin/nieznane/rozwiaz", data={"name": "guma arabska suszona rozpyłowo",
                                                 "action": "synonym", "target": "guma arabska"})
    client.post("/admin/nieznane/rozwiaz", data={"name": "tusz opacode s-1-15094", "action": "premix",
                                                 "components": ""})
    st, names = status(NUROFEN)
    assert st == OK and "guma arabska" in names and not any("opacode" in n for n in names)
    assert ImportRun.query.count() == 1


def test_synonym_suggestions_are_conservative():
    from app.admin import suggest
    existing = ["guma arabska", "woda oczyszczona", "żółcień pomarańczowa", "glicerol"]
    assert suggest("guma arabska suszona rozpyłowo", existing) == "guma arabska"
    assert suggest("olejek pomarańczowy", existing) == ""       # podobne słowo to nie synonim
    assert suggest("glicerolu monokaprylokapronian", existing) == ""


def load_demo(app):
    importer.load_registry(DEMO_CSV, mark_missing=False)


@pytest.mark.parametrize("q", ["5909990657971", "@05909990657971",
                               "0105909990657971211234567890\x1d17281231\x1d10LOT1"])
def test_search_by_gtin_and_datamatrix(app, client, q):
    load_demo(app)
    r = client.get("/", query_string={"q": q})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/lek/{prod(MIG).id}")


def test_unknown_gtin(app, client):
    load_demo(app)
    r = client.get("/", query_string={"q": "5909990000000"})
    assert r.status_code == 404 and "Nie znaleziono leku o kodzie GTIN" in r.get_data(as_text=True)


def test_packages_from_registry(app):
    load_demo(app)
    gt = {g.code: g for g in prod(IBUPROM).gtins}
    assert len(gt) == 6 and gt["05909990970025"].package == "12 tabl."
    importer.load_registry(csv_with([MIG], change={"Opakowanie": (
        MIG, "05909990657971 ¦ OTC ¦ 44969\n20 tabl.\n05909990657988 ¦ OTC ¦ Skasowane ¦ 44970\n50 tabl.")}),
        mark_missing=False)
    gt = {g.code: g for g in prod(MIG).gtins}
    assert set(gt) == {"05909990657971", "05909990657988"}          # opakowanie 10 tabl. zniknęło z rejestru
    assert gt["05909990657988"].deleted and not gt["05909990657971"].deleted


def test_parse_packages_ignores_other_lines():
    text = ("05909990805518 ¦ Rp ¦ 25\n10 amp. 10 ml\n1 amp.-strzyk. 1 ml ¦ 1 igła \n"
            "Rpz ¦ Skasowane ¦ EU/1/08/496/009 ¦ 122162\n5909990657971 ¦ OTC ¦ 1\n20 tabl.")
    assert importer.parse_packages(text) == [("05909990805518", False, "10 amp. 10 ml"),
                                             ("05909990657971", False, "20 tabl.")]


def test_suggestions(app, client):
    load_demo(app)
    assert client.get("/api/podpowiedzi?q=ib").get_json() == []                  # mniej niż 3 znaki
    assert client.get("/api/podpowiedzi?q=ibu").get_json() == ["Ibuprom Max"]
    assert client.get("/api/podpowiedzi?q=ram").get_json() == ["Ramidilan HCT"]   # nazwy bez powtórzeń
    assert client.get("/api/podpowiedzi?q=5909990").get_json() == []              # kod, nie nazwa
    names = client.get("/api/podpowiedzi?q=nur").get_json()
    assert names[0] == "Nurofen dla dzieci o smaku truskawkowym" or names[0] == "Nurofen Forte"
    assert all("nur" in n.lower() for n in names)


def test_ema_not_matched_or_wrong_document(rpl):
    rpl.ema["data"] = []                                    # brak leku w EMA: zostaje "brak ChPL"
    importer.run_import()
    assert status(APROVEL)[0] == NO_CHPL and prod(APROVEL).ema_url is None

    rpl.ema = json.loads(json.dumps(EMA_MEDICINES))         # dokument EMA z numerami innego leku
    rpl.files["aprovel"] = EMA_TEXT.replace("EU/1/97/046", "EU/1/99/999")
    importer.run_import()
    st, _ = status(APROVEL)
    assert st == REVIEW and "numery pozwoleń EU" in prod(APROVEL).review_reason


def test_ema_failure_does_not_stop_import(rpl, monkeypatch):
    rpl.ema = "to nie jest JSON"
    run = importer.run_import()
    assert run.status == "ok" and "ema_blad" in run.stats and status(IBUPROM)[0] == OK


def test_unmatched_central_and_parallel_import_pages(app, client):
    load_demo(app)
    ir = Product.query.filter_by(procedure="IR").first()
    assert ir.source == "ULOTKA" and ir.status != NO_CHPL      # import równoległy: skład z ulotki
    page = client.get(f"/lek/{ir.id}").get_data(as_text=True)
    assert "ulotka dla pacjenta" in page and "import równoległy" in page
    page = client.get(f"/lek/{prod(APROVEL).id}").get_data(as_text=True)
    assert "Europejską Agencję Leków" in page and "ema.europa.eu" in page


def test_parallel_import_composition_from_leaflet(rpl, client):
    rpl.registry = csv_with([IBUPROM, NUROFEN_IR])
    run = importer.run_import()
    assert run.status == "ok", run.error
    st, names = status(NUROFEN_IR)
    assert st == REVIEW and prod(NUROFEN_IR).chpl.kind == "ulotka"   # "maltitol ciekły", "guma ksantanowa" spoza słownika
    doc = prod(NUROFEN_IR).chpl
    assert {"glicerol", "sodu benzoesan", "glikol propylenowy", "maltodekstryna"} <= {
        e["nazwa"] for e in doc.parse_result["substancje"]}
    login(client)
    for name in ("maltitol ciekły", "guma ksantanowa"):
        client.post("/admin/nieznane/rozwiaz", data={"name": name, "action": "substance", "canonical": name})
    st, names = status(NUROFEN_IR)
    assert st == OK and "maltitol ciekły" in names and "aromat truskawkowy" not in names
    page = client.get(f"/lek/{prod(NUROFEN_IR).id}").get_data(as_text=True)
    assert "ulotka dla pacjenta" in page



def counting_client(rpl, fail_on=None):
    """Klient liczący zapytania; fail_on(url, liczba_pobrań) może rzucić wyjątek."""
    calls = {"get": [], "head": []}
    orig = rpl.client

    def client(delay):
        c = orig(delay)
        g, h = c.get, c.head

        def get(url, **kw):
            if "get-csv" not in url and "medicines_json" not in url:
                calls["get"].append(url)
                if fail_on:
                    fail_on(url, len(calls["get"]))
            return g(url, **kw)

        def head(url):
            calls["head"].append(url)
            return h(url)
        c.get, c.head = get, head
        return c
    return client, calls


def test_interrupted_import_resumes_where_it_stopped(rpl, monkeypatch):
    def stop_on_third(url, n):
        if n == 3:
            raise KeyboardInterrupt
    client, calls = counting_client(rpl, stop_on_third)
    monkeypatch.setattr(importer, "Client", client)
    run = importer.run_import()
    assert run.status == "przerwany"
    done = {p.id for p in Product.query.filter(Product.chpl_checked_at.isnot(None))}
    assert len(done) == 2                                        # dwa leki zapisane przed przerwaniem

    calls["get"].clear(); calls["head"].clear()
    client2, calls2 = counting_client(rpl)
    monkeypatch.setattr(importer, "Client", client2)
    run = importer.run_import()
    assert run.status == "ok" and run.stats["chpl_pominiete_niedawno_sprawdzone"] == 2
    assert len(calls2["head"]) == 2 and len(calls2["get"]) == 2  # tylko dwa niesprawdzone dokumenty
    assert Product.query.filter(Product.chpl_checked_at.is_(None), Product.status != NO_CHPL).count() == 0


def test_one_broken_document_does_not_stop_import(rpl, monkeypatch):
    real = importer.parse_pdf

    def broken_for_mig(content, tables, kind="chpl"):
        if content.decode("utf-8") == rpl.files[MIG]:
            raise RuntimeError("uszkodzony plik")
        return real(content, tables, kind)
    monkeypatch.setattr(importer, "parse_pdf", broken_for_mig)
    run = importer.run_import()
    assert run.status == "ok" and run.stats["chpl_bledy"] == 1
    assert status(IBUPROM)[0] == OK
    assert status(MIG)[0] == REVIEW and "błąd przetwarzania" in prod(MIG).review_reason


def test_network_error_leaves_product_for_next_run(rpl, monkeypatch):
    def fail_mig(url, n):
        if f"/{MIG}/" in url:
            raise importer.requests.ConnectionError("zerwane połączenie")
    client, _ = counting_client(rpl, fail_mig)
    monkeypatch.setattr(importer, "Client", client)
    run = importer.run_import()
    assert run.status == "ok" and run.stats["chpl_bledy"] == 1
    assert prod(MIG).chpl_checked_at is None                     # przy następnym imporcie pierwszy w kolejce
    client2, calls2 = counting_client(rpl)
    monkeypatch.setattr(importer, "Client", client2)
    importer.run_import()
    assert [u for u in calls2["get"]] and all(f"/{MIG}/" in u for u in calls2["get"])
    assert status(MIG)[0] == OK


def test_client_retries_dropped_connection(monkeypatch):
    c = importer.Client(0)
    monkeypatch.setattr(importer.Client, "RETRY_PAUSES", (0, 0))
    attempts = []

    class Resp:
        status_code = 200

    def flaky_get(url, timeout, **kw):
        attempts.append(url)
        if len(attempts) < 3:
            raise importer.requests.exceptions.ChunkedEncodingError("IncompleteRead")
        return Resp()
    monkeypatch.setattr(c.http, "get", flaky_get)
    assert c.get("https://example.invalid/plik.pdf").status_code == 200 and len(attempts) == 3
    attempts.clear()
    monkeypatch.setattr(c.http, "get", lambda url, timeout, **kw: (_ for _ in ()).throw(
        importer.requests.ConnectionError("stale brak sieci")))
    with pytest.raises(importer.requests.ConnectionError):
        c.get("https://example.invalid/plik.pdf")



def test_pdf_read_error_is_not_an_unknown_name(app, client, tables):
    from app.chpl_parser import PDF_ERROR, parse_pdf
    r, _ = parse_pdf(b"to nie jest PDF", tables)
    assert PDF_ERROR in r.powody and r.nieznane == [] and r.blad
    from app.models import ChplDocument
    db.session.add(ChplDocument(sha256="x" * 64, parse_status="WYMAGA_WERYFIKACJI",       # zapis ze starej wersji
                                parse_result={"status": "WYMAGA_WERYFIKACJI", "powody": [PDF_ERROR],
                                              "nieznane": ["PdfminerException"]}))
    db.session.commit()
    login(client)
    assert "PdfminerException" not in client.get("/admin/nieznane").get_data(as_text=True)


def test_unknown_names_page_shows_where_and_clear_actions(rpl, client):
    importer.run_import()
    login(client)
    page = client.get("/admin/nieznane").get_data(as_text=True)
    assert "guma arabska suszona rozpyłowo" in page and "Nurofen Forte" in page     # gdzie występuje
    for action in ("Zapisz jako synonim", "Dodaj substancję", "Pomijaj ten tekst", "Zapisz mieszankę"):
        assert action in page
    assert "Podpowiedź: to może być <strong>guma arabska</strong>" in page


def test_merge_substance_added_by_mistake(rpl, client):
    importer.run_import()
    login(client)
    wrong = "guma arabska suszona rozpyłowo"                      # omyłkowo jako nowa substancja
    client.post("/admin/nieznane/rozwiaz", data={"name": wrong, "action": "substance", "canonical": wrong})
    client.post("/admin/nieznane/rozwiaz", data={"name": "tusz opacode s-1-15094", "action": "premix", "components": ""})
    assert status(NUROFEN)[0] == OK and wrong in status(NUROFEN)[1]

    source = Excipient.query.filter_by(name=wrong).one()
    page = client.get("/admin/slowniki").get_data(as_text=True)
    assert f'id="sub-{source.id}"' in page and "scal z…" in page
    r = client.post("/admin/slowniki/scal", data={"source_id": source.id, "target": "guma arabska"},
                    follow_redirects=True)
    assert "scalono" in r.get_data(as_text=True)
    st, names = status(NUROFEN)
    assert st == OK and "guma arabska" in names and wrong not in names
    assert Excipient.query.filter_by(name=wrong).first() is None
    assert Synonym.query.filter_by(text=wrong).one().excipient.name == "guma arabska"
    doc_names = {e["nazwa"] for e in prod(NUROFEN).chpl.parse_result["substancje"]}
    assert "guma arabska" in doc_names and wrong not in doc_names  # wynik parsera też z nową nazwą
    # scalenie z samą sobą jest odrzucane
    ga = Excipient.query.filter_by(name="guma arabska").one()
    r = client.post("/admin/slowniki/scal", data={"source_id": ga.id, "target": "guma arabska"}, follow_redirects=True)
    assert "Wybierz z listy inną" in r.get_data(as_text=True)


def test_merge_keeps_manual_composition(rpl, client):
    importer.run_import()
    login(client)
    client.post(f"/admin/lek/{prod(NUROFEN).id}", data={"excipients": "Talk\nsubstancja testowa",
                                                        "create_unknown": "1"})
    assert status(NUROFEN) == (OK, {"talk", "substancja testowa"})
    src = Excipient.query.filter_by(name="substancja testowa").one()
    client.post("/admin/slowniki/scal", data={"source_id": src.id, "target": "sacharoza"})
    assert status(NUROFEN) == (OK, {"talk", "sacharoza"}) and prod(NUROFEN).manual


def test_upgrade_adds_columns_to_old_database(app):
    from sqlalchemy import inspect, text
    from app.models import ChplDocument
    from app.schema import needs_upgrade, upgrade_schema
    load_demo(app)
    with db.engine.begin() as conn:                              # baza sprzed wersji z ulotkami i EMA
        conn.execute(text('ALTER TABLE chpl_document DROP COLUMN kind'))
        conn.execute(text('ALTER TABLE product DROP COLUMN ema_url'))
        conn.execute(text('ALTER TABLE product DROP COLUMN manual'))
    db.session.expire_all()
    assert needs_upgrade()
    added = upgrade_schema()
    assert {"chpl_document.kind", "product.ema_url", "product.manual"} <= set(added)
    assert not needs_upgrade() and upgrade_schema() == []        # drugi raz nic do zrobienia
    assert Product.query.count() == 17 and ChplDocument.query.count() == 0   # dane zostały
    assert prod(IBUPROM).manual is False                         # NOT NULL z wartością domyślną
