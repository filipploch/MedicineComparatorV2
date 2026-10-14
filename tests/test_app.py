"""Przebieg całości bez sieci: import (z udawanym API), strony publiczne, panel redaktora."""
import json

import pytest

from app import db, importer
from app.chpl_parser import parse_text
from app.models import OK, REVIEW, ImportRun, Product, User

from .conftest import FIXTURES

REGISTRY = [i for i in json.loads((FIXTURES / "rejestr_demo.json").read_text(encoding="utf-8"))["content"]
            if i["id"] in (11104, 19752, 9487)]


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None, payload=None):
        self.status_code, self.content, self.headers = status, content, headers or {}
        if payload is not None:
            self.content = json.dumps(payload).encode("utf-8")

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise importer.requests.HTTPError(self.status_code)


class FakeRPL:
    """Udaje API rejestru i pliki ChPL; plik ChPL = tekst punktu 6.1 z prawdziwego dokumentu."""

    def __init__(self, sections):
        self.registry = list(REGISTRY)
        self.files = {11104: sections["11104"], 19752: sections["mig_400"], 9487: sections["nurofen_forte"]}
        self.versions = {k: "v1" for k in self.files}
        self.downloads = 0

    def client(self, delay):
        fake = self

        class Client:
            def get(self, url, params=None):
                if params is not None:
                    return FakeResponse(payload={"content": fake.registry, "last": True})
                fake.downloads += 1
                pid = int(url.split("/")[-2])
                return FakeResponse(content=fake.files[pid].encode("utf-8"), headers=fake.headers(pid))

            def head(self, url):
                return FakeResponse(headers=fake.headers(int(url.split("/")[-2])))
        return Client()

    def headers(self, pid):
        return {"Content-Disposition": f'attachment; filename="Charakterystyka-{pid}-{self.versions[pid]}.pdf"'}


@pytest.fixture()
def rpl(app, sections, monkeypatch):
    fake = FakeRPL(sections)
    monkeypatch.setattr(importer, "Client", fake.client)
    wrap = "6.1 Wykaz substancji pomocniczych\n{}\n6.2 Niezgodności farmaceutyczne\n" + "x" * 300
    monkeypatch.setattr(importer, "parse_pdf",
                        lambda content, tables: (parse_text(wrap.format(content.decode("utf-8")), tables), ""))
    return fake


def status(pid):
    p = db.session.get(Product, pid)
    return p.status, {e.name for e in p.excipients}


def test_import_and_change_detection(rpl):
    run = importer.run_import()
    assert run.status == "ok", run.error
    assert run.stats["leki"] == 3 and run.stats["chpl_nowe"] == 3
    assert status(11104)[0] == OK and "sacharoza" in status(11104)[1]
    assert status(19752)[0] == OK
    assert status(9487)[0] == REVIEW          # Nurofen: "tusz Opacode ..." spoza słownika
    assert db.session.get(Product, 11104).group_key == db.session.get(Product, 9487).group_key  # 400 mg == 0,4 g

    downloads = rpl.downloads
    run = importer.run_import()                 # nic się nie zmieniło: same zapytania HEAD
    assert rpl.downloads == downloads and run.stats["chpl_bez_zmian"] == 3

    rpl.versions[19752] = "v2"                 # nowa wersja ChPL MIG
    rpl.files[19752] += "\nSacharoza"
    importer.run_import()
    assert "sacharoza" in status(19752)[1]

    rpl.registry = [i for i in rpl.registry if i["id"] != 11104]   # lek wycofany z rejestru
    run = importer.run_import()
    assert run.stats["leki_wycofane"] == 1 and not db.session.get(Product, 11104).active


def test_manual_composition_survives_import(rpl, client):
    importer.run_import()
    login(client)
    r = client.post("/admin/lek/9487", data={"excipients": "Talk\nE 171\nNieznany składnik"})
    assert "Spoza słownika" in r.get_data(as_text=True)
    client.post("/admin/lek/9487", data={"excipients": "Talk\nE 171"})
    assert status(9487) == (OK, {"talk", "tytanu dwutlenek"})

    importer.run_import()                        # bez zmian w ChPL: skład ręczny zostaje
    assert status(9487) == (OK, {"talk", "tytanu dwutlenek"})
    rpl.versions[9487] = "v2"
    rpl.files[9487] += "\nTalk"
    importer.run_import()                        # ChPL się zmienił: skład zostaje, ale wraca do kolejki
    assert status(9487) == (REVIEW, {"talk", "tytanu dwutlenek"})


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
    page = client.get("/lek/11104").get_data(as_text=True)
    assert "sacharoza" in page and "MIG 400" in page and "Nurofen Forte" in page
    page = client.get("/lek/11104/porownanie").get_data(as_text=True)
    assert 'class="differs"' in page             # np. sacharoza jest w Ibuprom Max, a nie w MIG
    assert "skład w trakcie weryfikacji" in page   # Nurofen jeszcze w kolejce
    assert client.get("/lek/1").status_code == 404


def test_admin_requires_login_and_resolves_unknown(rpl, client):
    importer.run_import()
    assert client.get("/admin/").status_code == 302
    login(client)
    assert "do weryfikacji" in client.get("/admin/").get_data(as_text=True)
    page = client.get("/admin/nieznane").get_data(as_text=True)
    assert "tusz opacode s-1-15094" in page

    client.post("/admin/nieznane/rozwiaz", data={"name": "guma arabska suszona rozpyłowo",
                                                 "action": "synonym", "target": "guma arabska"})
    client.post("/admin/nieznane/rozwiaz", data={"name": "tusz opacode s-1-15094", "action": "premix",
                                                 "components": ""})
    st, names = status(9487)
    assert st == OK and "guma arabska" in names and not any("opacode" in n for n in names)
    assert ImportRun.query.count() == 1


def test_synonym_suggestions_are_conservative():
    from app.admin import suggest
    existing = ["guma arabska", "woda oczyszczona", "żółcień pomarańczowa", "glicerol"]
    assert suggest("guma arabska suszona rozpyłowo", existing) == "guma arabska"
    assert suggest("olejek pomarańczowy", existing) == ""       # podobne słowo to nie synonim
    assert suggest("glicerolu monokaprylokapronian", existing) == ""


def load_demo(app):
    from app.importer import upsert_product
    for item in json.loads((FIXTURES / "rejestr_demo.json").read_text(encoding="utf-8"))["content"]:
        upsert_product(item)
    db.session.commit()


@pytest.mark.parametrize("q", ["5909990657971", "@05909990657971",
                               "0105909990657971211234567890\x1d17281231\x1d10LOT1"])
def test_search_by_gtin_and_datamatrix(app, client, q):
    load_demo(app)
    r = client.get("/", query_string={"q": q})
    assert r.status_code == 302 and r.headers["Location"].endswith("/lek/19752")


def test_unknown_gtin(app, client):
    load_demo(app)
    r = client.get("/", query_string={"q": "5909990000000"})
    assert r.status_code == 404 and "Nie znaleziono leku o kodzie GTIN" in r.get_data(as_text=True)


def test_gtins_follow_registry(app):
    from app.importer import upsert_product
    load_demo(app)
    item = next(i for i in json.loads((FIXTURES / "rejestr_demo.json").read_text(encoding="utf-8"))["content"]
                if i["id"] == 19752)
    upsert_product(dict(item, gtin="05909990657971"))      # dwa opakowania zniknęły z rejestru
    db.session.commit()
    assert [g.code for g in db.session.get(Product, 19752).gtins] == ["05909990657971"]


def test_suggestions(app, client):
    load_demo(app)
    assert client.get("/api/podpowiedzi?q=ib").get_json() == []                  # mniej niż 3 znaki
    assert client.get("/api/podpowiedzi?q=ibu").get_json() == ["Ibuprom Max"]
    names = client.get("/api/podpowiedzi?q=ram").get_json()
    assert names == ["Ramidilan HCT"]                                             # nazwy bez powtórzeń
    assert client.get("/api/podpowiedzi?q=5909990").get_json() == []              # kod, nie nazwa
    starts_first = client.get("/api/podpowiedzi?q=ine").get_json()
    assert starts_first and all("ine" in n.lower() for n in starts_first)
