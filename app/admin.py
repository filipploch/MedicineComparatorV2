import re
from collections import Counter
from difflib import get_close_matches

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user

from . import db
from .chpl_parser import REASON_LABELS, Result, norm_key
from .importer import apply_composition, excipient_by_name, reparse_documents, tables_from_db
from .models import (OK, REVIEW, ChplDocument, ENumber, Excipient, IgnoredLabel, ImportRun, Premix,
                     Product, Synonym, User)

bp = Blueprint("admin", __name__)


@bp.app_template_filter("reason")
def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code)


# --- logowanie ----------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "")).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user)
            return redirect(request.args.get("next") or url_for("admin.dashboard"))
        flash("Nieprawidłowy login lub hasło.", "error")
    return render_template("admin/login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("public.index"))


def suggest(name: str, existing: list[str]) -> str:
    """Podpowiedź synonimu: najdłuższa nazwa ze słownika zawarta w całości w nieznanej nazwie
    ("guma arabska suszona rozpyłowo" -> "guma arabska"), a w drugiej kolejności bardzo podobna."""
    contained = [e for e in existing if re.search(rf"(?<!\w){re.escape(e.lower())}(?!\w)", name.lower())]
    if contained:
        return max(contained, key=len)
    return (get_close_matches(name, existing, n=1, cutoff=0.85) or [""])[0]


# --- pulpit, kolejka, dziennik -------------------------------------------------------
UNKNOWN_REASONS = {"NIEZNANA_SUBSTANCJA", "NIEZNANA_ETYKIETA", "NIEZNANY_NUMER_E"}


def _review_docs():
    """Dokumenty w weryfikacji z nieznanymi nazwami (bez błędów odczytu PDF, które są w kolejce)."""
    for doc in ChplDocument.query.filter(ChplDocument.parse_status != "OK"):
        result = doc.parse_result or {}
        if UNKNOWN_REASONS & set(result.get("powody", [])):
            yield doc, result


def unknown_names() -> Counter:
    """Nieznane nazwy ze wszystkich dokumentów w weryfikacji: nazwa -> liczba dokumentów."""
    counter: Counter = Counter()
    for _doc, result in _review_docs():
        counter.update(set(result.get("nieznane", [])))
    return counter


def unknown_context(limit: int = 3) -> dict[str, list[dict]]:
    """Gdzie występuje każda nieznana nazwa: dokument, leki i oryginalny tekst z dokumentu."""
    context: dict[str, list[dict]] = {}
    for doc, result in _review_docs():
        originals = {}
        for res in [result] + [c.get("wynik", {}) for c in result.get("czesci", [])]:
            for ex in res.get("substancje", []):
                if not ex.get("znana", True):
                    originals[ex["nazwa"]] = ex.get("oryginal", "")
        for name in set(result.get("nieznane", [])):
            places = context.setdefault(name, [])
            if len(places) < limit:
                places.append({"doc": doc, "original": originals.get(name, ""),
                               "products": [p for p in doc.products if p.active][:3]})
    return context


@bp.route("/")
@login_required
def dashboard():
    counts = dict(db.session.query(Product.status, db.func.count()).filter(Product.active.is_(True))
                  .group_by(Product.status).all())
    runs = ImportRun.query.order_by(ImportRun.started_at.desc()).limit(10).all()
    return render_template("admin/dashboard.html", counts=counts, runs=runs,
                           unknown=unknown_names().most_common(10))


@bp.route("/kolejka")
@login_required
def queue():
    products = (Product.query.filter(Product.status == REVIEW, Product.active.is_(True))
                .order_by(Product.name).all())
    by_doc: dict = {}
    for p in products:
        by_doc.setdefault(p.chpl_id, []).append(p)
    entries = []
    for doc_id, items in by_doc.items():
        doc = db.session.get(ChplDocument, doc_id) if doc_id else None
        entries.append({"doc": doc, "products": items,
                        "unknown": (doc.parse_result or {}).get("nieznane", []) if doc else []})
    return render_template("admin/queue.html", entries=entries)


# --- rozwiązywanie nieznanych nazw ----------------------------------------------------
@bp.route("/nieznane")
@login_required
def unknown():
    names = unknown_names().most_common()
    all_names = [e.name for e in Excipient.query.order_by(Excipient.name)]
    suggestions = {n: suggest(n, all_names) for n, _ in names}
    return render_template("admin/unknown.html", names=names, excipients=all_names, suggestions=suggestions,
                           context=unknown_context())


@bp.route("/nieznane/rozwiaz", methods=["POST"])
@login_required
def resolve():
    raw = request.form.get("name", "")
    action = request.form.get("action")
    is_label = raw.endswith(":")
    name = raw.rstrip(":")
    key = norm_key(name)
    if action == "substance":
        canon = request.form.get("canonical", "").strip() or name
        ex = excipient_by_name(canon)
        if norm_key(canon) != key and not Synonym.query.filter_by(text=key).first():
            db.session.add(Synonym(text=key, excipient=ex))
        flash(f"„{name}” dodano jako substancję „{canon}”.")
    elif action == "synonym":
        target = Excipient.query.filter_by(name=request.form.get("target", "")).first()
        if target is None:
            flash("Wybierz substancję z listy.", "error")
            return redirect(request.referrer or url_for(".unknown"))
        if not Synonym.query.filter_by(text=key).first():
            db.session.add(Synonym(text=key, excipient=target))
        flash(f"„{name}” to teraz synonim „{target.name}”.")
    elif action == "label":
        if not IgnoredLabel.query.filter_by(text=key).first():
            db.session.add(IgnoredLabel(text=key))
        flash(f"„{name}” będzie pomijane jako etykieta.")
    elif action == "premix":
        components = [c.strip() for c in request.form.get("components", "").splitlines() if c.strip()]
        premix = Premix.query.filter_by(name=key).first() or Premix(name=key)
        premix.components = components
        db.session.add(premix)
        flash(f"Mieszanka „{name}”: " + (", ".join(components) or "pomijana (skład podany osobno)"))
    else:
        abort(400)
    if is_label and action != "label":
        flash("To była etykieta z dwukropkiem; parser i tak ją pominie.", "error")
    db.session.commit()
    n = reparse_documents(only_review=True)
    flash(f"Ponownie przetworzono dokumentów: {n}.")
    return redirect(request.referrer or url_for(".unknown"))


# --- dokument i lek ---------------------------------------------------------------------
@bp.route("/dokument/<int:doc_id>")
@login_required
def document(doc_id: int):
    doc = db.session.get(ChplDocument, doc_id) or abort(404)
    result = Result.from_dict(doc.parse_result or {})
    all_names = [e.name for e in Excipient.query.order_by(Excipient.name)]
    suggestions = {n: suggest(n, all_names) for n in result.nieznane}
    return render_template("admin/document.html", doc=doc, result=result, excipients=all_names,
                           suggestions=suggestions)


@bp.route("/dokument/<int:doc_id>/reparse", methods=["POST"])
@login_required
def document_reparse(doc_id: int):
    db.session.get(ChplDocument, doc_id) or abort(404)
    reparse_documents(only_review=False)
    flash("Dokumenty przetworzone ponownie.")
    return redirect(url_for(".document", doc_id=doc_id))


@bp.route("/lek/<int:product_id>", methods=["GET", "POST"])
@login_required
def product(product_id: int):
    p = db.session.get(Product, product_id) or abort(404)
    if request.method == "POST":
        if request.form.get("action") == "auto":
            p.manual = False
            apply_composition(p)
            db.session.commit()
            flash("Przywrócono skład z ChPL.")
            return redirect(url_for(".product", product_id=p.id))
        tables = tables_from_db()
        lines = [l.strip() for l in request.form.get("excipients", "").splitlines() if l.strip()]
        resolved, unknown = [], []
        for line in lines:
            canon = tables.canonical(line) or tables.e_numbers.get(line.upper().replace(" ", ""))
            if canon:
                resolved.append(canon)
            elif request.form.get("create_unknown"):
                resolved.append(line.lower())
            else:
                unknown.append(line)
        if unknown:
            flash("Spoza słownika: " + ", ".join(unknown) + ". Zaznacz „dodaj nowe” albo popraw nazwy.", "error")
            return render_template("admin/product.html", p=p, text="\n".join(lines))
        p.excipients = [excipient_by_name(n) for n in dict.fromkeys(resolved)]
        p.manual, p.status, p.review_reason = True, OK, None
        db.session.commit()
        flash("Zapisano skład. Kolejne importy go nie nadpiszą.")
        return redirect(url_for(".product", product_id=p.id))
    return render_template("admin/product.html", p=p, text="\n".join(e.name for e in p.excipients))


# --- słowniki ----------------------------------------------------------------------------
@bp.route("/slowniki")
@login_required
def dictionaries():
    return render_template("admin/dictionaries.html",
                           excipients=Excipient.query.order_by(Excipient.name).all(),
                           labels=IgnoredLabel.query.order_by(IgnoredLabel.text).all(),
                           premixes=Premix.query.order_by(Premix.name).all(),
                           e_numbers=ENumber.query.order_by(ENumber.code).all())


@bp.route("/slowniki/usun", methods=["POST"])
@login_required
def dictionary_delete():
    model = {"synonym": Synonym, "label": IgnoredLabel, "premix": Premix, "e": ENumber}.get(request.form.get("kind"))
    if model is None:
        abort(400)
    key = request.form.get("id")
    obj = db.session.get(model, key if model is ENumber else int(key))
    if obj:
        db.session.delete(obj)
        db.session.commit()
        flash("Usunięto. Zmiana zadziała przy kolejnym przetworzeniu dokumentów (przycisk lub `flask reparse --wszystkie`).")
    return redirect(url_for(".dictionaries"))


@bp.route("/slowniki/numer-e", methods=["POST"])
@login_required
def add_e_number():
    code = request.form.get("code", "").upper().replace(" ", "")
    target = Excipient.query.filter_by(name=request.form.get("target", "")).first()
    if not code.startswith("E") or target is None:
        flash("Podaj kod w formie E123 i wybierz substancję.", "error")
    else:
        entry = db.session.get(ENumber, code) or ENumber(code=code)
        entry.excipient = target
        db.session.add(entry)
        db.session.commit()
        flash(f"{code} = {target.name}")
    return redirect(url_for(".dictionaries"))


@bp.route("/reparse", methods=["POST"])
@login_required
def reparse_all():
    flash(f"Ponownie przetworzono dokumentów: {reparse_documents(only_review=False)}.")
    return redirect(request.referrer or url_for(".dashboard"))
