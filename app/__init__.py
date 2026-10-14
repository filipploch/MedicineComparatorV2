import os
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-tylko-lokalnie"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'leki.db'}"),
        # Rejestr Produktów Leczniczych: pełny wykaz w CSV (link "Pobierz CSV" na rejestry.ezdrowie.gov.pl/registry/rpl)
        RPL_CSV_URL=os.environ.get(
            "RPL_CSV_URL", "https://rejestry.ezdrowie.gov.pl/api/rpl/medicinal-products/public-pl-report/get-csv"),
        RPL_DELAY_SECONDS=float(os.environ.get("RPL_DELAY_SECONDS", 1.0)),
        # Znaki, które skaner lub użytkownik może dopisać przed kodem GTIN / Data Matrix
        SCAN_PREFIXES=os.environ.get("SCAN_PREFIXES", "@"),
    )
    if config:
        app.config.update(config)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"):
        Path(app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "admin.login"
    login_manager.login_message = "Zaloguj się, aby wejść do panelu."

    from . import models  # noqa: F401  (rejestracja modeli)
    from .public import bp as public_bp
    from .admin import bp as admin_bp
    from .cli import register_cli

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    register_cli(app)
    return app
