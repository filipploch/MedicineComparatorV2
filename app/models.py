from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, login_manager


def now() -> datetime:
    return datetime.now(timezone.utc)


# Statusy składu leku
PENDING = "oczekuje"            # ChPL jeszcze nie przetworzony
OK = "ok"                       # skład ustalony (z parsera albo wpisany przez redaktora)
REVIEW = "weryfikacja"          # wymaga ingerencji redaktora
NO_CHPL = "brak_chpl"           # rejestr nie udostępnia ChPL (leki dopuszczone przez EMA, import równoległy)

product_excipient = db.Table(
    "product_excipient",
    db.Column("product_id", db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True),
    db.Column("excipient_id", db.ForeignKey("excipient.id", ondelete="CASCADE"), primary_key=True),
)


class Product(db.Model):
    """Rekord z Rejestru Produktów Leczniczych (jedna moc = jeden rekord)."""
    id = db.Column(db.Integer, primary_key=True)                  # "Identyfikator Produktu Leczniczego" z CSV
    rpl_id = db.Column(db.Integer, index=True)                    # id w adresach API (np. .../11104/characteristic)
    name = db.Column(db.String(300), nullable=False, index=True)
    common_name = db.Column(db.String(500))                       # substancje czynne (łac.)
    power = db.Column(db.String(200))                             # moc jak w RPL
    form = db.Column(db.String(200))
    holder = db.Column(db.String(300))
    registry_number = db.Column(db.String(50))
    atc = db.Column(db.String(20))
    procedure = db.Column(db.String(10))                          # NAR, DCP, MRP, CEN (EMA), IR (import równoległy)
    chpl_url = db.Column(db.String(300))                          # ChPL z rejestru; brak = rejestr go nie udostępnia
    leaflet_url = db.Column(db.String(300))
    eu_numbers = db.Column(db.JSON, default=list)                 # numery pozwoleń EU opakowań (leki centralne)
    ema_url = db.Column(db.String(300))                           # informacja o produkcie EMA (PL), gdy brak ChPL w RPL
    ema_product_number = db.Column(db.String(30))                 # np. EMEA/H/C/000141
    group_key = db.Column(db.String(600), index=True)             # klucz odpowiedników
    active = db.Column(db.Boolean, default=True, nullable=False)  # znika z RPL -> False

    chpl_id = db.Column(db.ForeignKey("chpl_document.id"))
    chpl = db.relationship("ChplDocument", back_populates="products")
    excipients = db.relationship("Excipient", secondary=product_excipient, order_by="Excipient.name")
    status = db.Column(db.String(20), default=PENDING, nullable=False, index=True)
    review_reason = db.Column(db.String(300))
    manual = db.Column(db.Boolean, default=False, nullable=False)  # skład wpisany ręcznie: import nie nadpisuje
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now)

    # Ostatnio widziane nagłówki ChPL (do wykrywania zmian bez pobierania pliku)
    chpl_etag = db.Column(db.String(300))
    chpl_checked_at = db.Column(db.DateTime(timezone=True))

    gtins = db.relationship("Gtin", back_populates="product", cascade="all, delete-orphan", order_by="Gtin.code")

    @property
    def source_url(self) -> str | None:
        """Dokument, z którego pochodzi skład: ChPL z rejestru, potem informacja o produkcie EMA,
        a na końcu ulotka (punkt 6), gdy rejestr nie udostępnia ChPL."""
        return self.chpl_url or self.ema_url or self.leaflet_url

    @property
    def source(self) -> str | None:
        if self.chpl_url:
            return "RPL"
        if self.ema_url:
            return "EMA"
        return "ULOTKA" if self.leaflet_url else None

    @property
    def document_kind(self) -> str:
        return "ulotka" if self.source == "ULOTKA" else "chpl"

    def equivalents(self):
        if not self.group_key:
            return []
        return (Product.query.filter(Product.group_key == self.group_key, Product.active.is_(True))
                .order_by(Product.name).all())


class Gtin(db.Model):
    """Kod GTIN opakowania (14 cyfr; EAN-13 z pudełka jest dopełniany zerem z przodu)."""
    code = db.Column(db.String(14), primary_key=True)
    product_id = db.Column(db.ForeignKey("product.id", ondelete="CASCADE"), nullable=False, index=True)
    package = db.Column(db.String(200))                   # opis opakowania, np. "20 tabl."
    deleted = db.Column(db.Boolean, default=False)        # opakowanie skasowane w rejestrze (może jeszcze być w aptece)
    product = db.relationship("Product", back_populates="gtins")


class ChplDocument(db.Model):
    """Jeden plik ChPL; kilka rekordów (moce) może wskazywać ten sam plik."""
    id = db.Column(db.Integer, primary_key=True)
    sha256 = db.Column(db.String(64), unique=True, nullable=False)
    kind = db.Column(db.String(10), default="chpl")     # "chpl" (także informacja o produkcie EMA) albo "ulotka"
    file_name = db.Column(db.String(300))         # z nagłówka Content-Disposition, jeśli jest
    size = db.Column(db.Integer)
    source_url = db.Column(db.String(500))        # link do PDF dla redaktora
    fetched_at = db.Column(db.DateTime(timezone=True), default=now)
    raw_section = db.Column(db.Text)              # surowy punkt 6.1
    parse_status = db.Column(db.String(30))
    parse_result = db.Column(db.JSON)             # wynik parsera (substancje, dawki, powody)
    products = db.relationship("Product", back_populates="chpl")


class Excipient(db.Model):
    """Substancja pomocnicza pod nazwą kanoniczną."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    synonyms = db.relationship("Synonym", back_populates="excipient", cascade="all, delete-orphan",
                               order_by="Synonym.text")


class Synonym(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(300), unique=True, nullable=False)   # klucz znormalizowany
    excipient_id = db.Column(db.ForeignKey("excipient.id", ondelete="CASCADE"), nullable=False)
    excipient = db.relationship("Excipient", back_populates="synonyms")


class ENumber(db.Model):
    code = db.Column(db.String(10), primary_key=True)   # "E171"
    excipient_id = db.Column(db.ForeignKey("excipient.id", ondelete="CASCADE"), nullable=False)
    excipient = db.relationship("Excipient")


class IgnoredLabel(db.Model):
    """Etykiety części leku pomijane przez parser ("Rdzeń tabletki", "Korpus")."""
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(200), unique=True, nullable=False)


class Premix(db.Model):
    """Mieszanka podana w ChPL bez składu, np. "Opadry II 85F18422".
    Pusta lista składników = pomiń (skład podany w ChPL osobno)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(300), unique=True, nullable=False)
    components = db.Column(db.JSON, nullable=False, default=list)


class ImportRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime(timezone=True), default=now)
    finished_at = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.String(20), default="trwa")     # trwa | ok | blad
    stats = db.Column(db.JSON, default=dict)
    error = db.Column(db.Text)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))
