import re
from pathlib import Path

import click
from flask import Flask

from . import BASE_DIR, db
from .chpl_parser import norm_key, read_seed
from .models import ENumber, Excipient, IgnoredLabel, Premix, Product, Synonym, User


def seed_dictionaries(folder: Path) -> dict:
    """Wczytuje tabele startowe (bez nadpisywania tego, co już jest w bazie)."""
    ignored, substances, e_numbers, premixes = read_seed(folder)
    added = {"substancje": 0, "synonimy": 0, "numery_e": 0, "etykiety": 0, "mieszanki": 0}

    def excipient(name: str) -> Excipient:
        ex = Excipient.query.filter_by(name=name).first()
        if ex is None:
            ex = Excipient(name=name)
            db.session.add(ex)
            db.session.flush()
            added["substancje"] += 1
        return ex

    for canon, synonyms in substances.items():
        ex = excipient(canon)
        for syn in synonyms:
            key = norm_key(syn)
            if key != norm_key(canon) and not Synonym.query.filter_by(text=key).first():
                db.session.add(Synonym(text=key, excipient=ex))
                added["synonimy"] += 1
    for code, name in e_numbers.items():
        if not db.session.get(ENumber, code.upper()):
            db.session.add(ENumber(code=code.upper(), excipient=excipient(name)))
            added["numery_e"] += 1
    for label in ignored:
        key = norm_key(label)
        if not IgnoredLabel.query.filter_by(text=key).first():
            db.session.add(IgnoredLabel(text=key))
            added["etykiety"] += 1
    for name, components in premixes.items():
        if not Premix.query.filter_by(name=norm_key(name)).first():
            db.session.add(Premix(name=norm_key(name), components=components))
            added["mieszanki"] += 1
    db.session.commit()
    return added


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    @click.option("--seed", "seed_dir", type=click.Path(path_type=Path), default=BASE_DIR / "seed",
                  show_default=True, help="katalog z tabelami startowymi")
    def init_db(seed_dir: Path):
        """Tworzy tabele i wczytuje słowniki startowe."""
        db.create_all()
        click.echo(f"Tabele gotowe. Dodano: {seed_dictionaries(seed_dir)}")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username: str, password: str):
        """Zakłada konto redaktora albo zmienia mu hasło."""
        user = User.query.filter_by(username=username).first() or User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Konto {username} gotowe.")

    @app.cli.command("import-data")
    @click.option("--limit", type=int, help="tylko pierwsze N leków (do testów)")
    @click.option("--bez-chpl", is_flag=True, help="tylko lista leków, bez pobierania ChPL")
    def import_data(limit, bez_chpl):
        """Pobiera rejestr i ChPL (uruchamiane z crona raz w tygodniu)."""
        from .importer import run_import
        run = run_import(limit=limit, with_chpl=not bez_chpl, progress=click.echo)
        click.echo(f"Import {run.status}: {run.stats}")
        if run.error:
            click.echo(run.error, err=True)
            raise SystemExit(1)

    @app.cli.command("reparse")
    @click.option("--wszystkie", is_flag=True, help="także dokumenty bez zastrzeżeń")
    def reparse(wszystkie):
        """Parsuje ponownie zapisane punkty 6.1 po zmianach w słownikach."""
        from .importer import reparse_documents
        click.echo(f"Przetworzono dokumentów: {reparse_documents(only_review=not wszystkie)}")

    @app.cli.command("load-registry")
    @click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    def load_registry(file: Path):
        """Wczytuje leki z pliku JSON w formacie odpowiedzi API RPL (bez sieci, do testów)."""
        import json
        from .importer import upsert_product
        data = json.loads(file.read_text(encoding="utf-8"))
        items = data["content"] if isinstance(data, dict) else data
        for item in items:
            upsert_product(item)
        db.session.commit()
        click.echo(f"Wczytano {len(items)} leków.")

    @app.cli.command("load-chpl")
    @click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
    def load_chpl(folder: Path):
        """Wczytuje lokalne pliki Charakterystyka-<id>-....pdf (bez sieci, do testów)."""
        from .importer import apply_composition, store_document, tables_from_db
        tables, n = tables_from_db(), 0
        for pdf in sorted(folder.glob("*.pdf")):
            m = re.match(r"Charakterystyka-(\d+)-", pdf.name)
            product = db.session.get(Product, int(m.group(1))) if m else None
            if product is None:
                click.echo(f"pominięto {pdf.name} (brak leku w bazie)")
                continue
            doc = store_document(pdf.read_bytes(), app.config["RPL_CHPL_URL"].format(id=product.id),
                                 {"Content-Disposition": f'attachment; filename="{pdf.name}"'}, tables)
            product.chpl = doc
            apply_composition(product)
            n += 1
        db.session.commit()
        click.echo(f"Wczytano {n} plików.")
