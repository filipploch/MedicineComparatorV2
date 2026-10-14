from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from sqlalchemy import case

from . import db
from .codes import classify
from .models import OK, Gtin, Product

bp = Blueprint("public", __name__)
SUGGEST_MIN_CHARS = 3
SUGGEST_LIMIT = 12


@bp.route("/")
def index():
    """Jedno pole: nazwa, GTIN (także z prefiksem, np. "@") albo zeskanowany kod Data Matrix."""
    raw = request.args.get("q", "")
    query = classify(raw, current_app.config["SCAN_PREFIXES"])
    if query.kind == "gtin":
        gtin = db.session.get(Gtin, query.value)
        if gtin is not None:
            return redirect(url_for(".product", product_id=gtin.product_id))
        return render_template("index.html", q=raw, query=query, products=[]), 404
    products = []
    if query.kind == "name" and len(query.value) >= 2:
        products = (Product.query.filter(Product.active.is_(True), Product.name.ilike(f"%{query.value}%"))
                    .order_by(Product.name, Product.power).limit(100).all())
    return render_template("index.html", q=raw, query=query, products=products)


@bp.route("/api/podpowiedzi")
def suggestions():
    """Nazwy leków pasujące do wpisanego tekstu (od 3 znaków); najpierw te, które się od niego zaczynają."""
    q = request.args.get("q", "").strip()
    if len(q) < SUGGEST_MIN_CHARS or classify(q, current_app.config["SCAN_PREFIXES"]).kind != "name":
        return jsonify([])
    starts = case((Product.name.ilike(f"{q}%"), 0), else_=1)
    rows = (db.session.query(Product.name, starts.label("rank"))
            .filter(Product.active.is_(True), Product.name.ilike(f"%{q}%"))
            .group_by(Product.name).order_by("rank", Product.name).limit(SUGGEST_LIMIT).all())
    return jsonify([r.name for r in rows])


@bp.route("/lek/<int:product_id>")
def product(product_id: int):
    p = db.session.get(Product, product_id) or abort(404)
    equivalents = [e for e in p.equivalents() if e.id != p.id]
    return render_template("product.html", p=p, equivalents=equivalents)


@bp.route("/lek/<int:product_id>/porownanie")
def compare(product_id: int):
    """Tabela: wiersze = substancje pomocnicze, kolumny = odpowiedniki."""
    p = db.session.get(Product, product_id) or abort(404)
    group = p.equivalents()
    if p not in group:
        group.insert(0, p)
    forms = sorted({g.form for g in group if g.form})
    form = request.args.get("forma")
    selected = [g for g in group if g.id == p.id or not form or g.form == form]
    selected.sort(key=lambda g: (g.id != p.id, g.name))

    known = [g for g in selected if g.status == OK]
    names = sorted({e.name for g in known for e in g.excipients})
    has = {g.id: {e.name for e in g.excipients} for g in known}
    rows = []
    for name in names:
        count = sum(name in has[g.id] for g in known)
        rows.append({"name": name, "differs": 0 < count < len(known), "has": has})
    return render_template("compare.html", p=p, products=selected, rows=rows, forms=forms, form=form,
                           only_diff=request.args.get("roznice") == "1")
