from flask import Blueprint, abort, render_template, request

from . import db
from .models import OK, Product

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    products = []
    if len(q) >= 2:
        products = (Product.query.filter(Product.active.is_(True), Product.name.ilike(f"%{q}%"))
                    .order_by(Product.name, Product.power).limit(100).all())
    return render_template("index.html", q=q, products=products)


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
