"""Przebieg całości bez sieci: import (z udawanym API), strony publiczne, panel redaktora."""
import json

import pytest

from app import db, importer
from app.chpl_parser import parse_text
from app.models import OK, REVIEW, ImportRun, Product, User

from .conftest import FIXTURES

REGISTRY = [i for i in json.loads((FIXTURES / "rejestr_demo.json").read_text(encoding="utf-8"))["content"]
            if i["id"] in (11104, 900001, 900002)]


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
        self.files = {11104: sections["11104"], 900001: sections["mig_400"], 900002: sections["nurofen_forte"]}
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
    assert status(900001)[0] == OK
    assert status(900002)[0] == REVIEW          # Nurofen: "tusz Opacode ..." spoza słownika
    assert db.session.get(Product, 11104).group_key == db.session.get(Product, 900002).group_key  # 400 mg == 0,4 g

    downloads = rpl.downloads
    run = importer.run_import()                 # nic się nie zmieniło: same zapytania HEAD
    assert rpl.downloads == downloads and run.stats["chpl_bez_zmian"] == 3

    rpl.versions[900001] = "v2"                 # nowa wersja ChPL MIG
    rpl.files[900001] += "\nSacharoza"
    importer.run_import()
    assert "sacharoza" in status(900001)[1]

    rpl.registry = [i for i in rpl.registry if i["id"] != 11104]   # lek wycofany z rejestru
    run = importer.run_import()
    assert run.stats["leki_wycofane"] == 1 and not db.session.get(Product, 11104).active


def test_manual_composition_survives_import(rpl, client):
    importer.run_import()
    login(client)
    r = client.post("/admin/lek/900002", data={"excipients": "Talk\nE 171\nNieznany składnik"})
    assert "Spoza słownika" in r.get_data(as_text=True)
    client.post("/admin/lek/900002", data={"excipients": "Talk\nE 171"})
    assert status(900002) == (OK, {"talk", "tytanu dwutlenek"})

    importer.run_import()                        # bez zmian w ChPL: skład ręczny zostaje
    assert status(900002) == (OK, {"talk", "tytanu dwutlenek"})
    rpl.versions[900002] = "v2"
    rpl.files[900002] += "\nTalk"
    importer.run_import()                        # ChPL się zmienił: skład zostaje, ale wraca do kolejki
    assert status(900002) == (REVIEW, {"talk", "tytanu dwutlenek"})


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
    st, names = status(900002)
    assert st == OK and "guma arabska" in names and not any("opacode" in n for n in names)
    assert ImportRun.query.count() == 1


def test_synonym_suggestions_are_conservative():
    from app.admin import suggest
    existing = ["guma arabska", "woda oczyszczona", "żółcień pomarańczowa", "glicerol"]
    assert suggest("guma arabska suszona rozpyłowo", existing) == "guma arabska"
    assert suggest("olejek pomarańczowy", existing) == ""       # podobne słowo to nie synonim
    assert suggest("glicerolu monokaprylokapronian", existing) == ""
