import json
from pathlib import Path

import pytest

from app import BASE_DIR, create_app, db
from app.chpl_parser import Tables

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def tables() -> Tables:
    return Tables.from_seed(BASE_DIR / "seed")


@pytest.fixture(scope="session")
def sections() -> dict[str, str]:
    """Surowe punkty 6.1 z 21 prawdziwych ChPL."""
    return json.loads((FIXTURES / "sekcje_6_1.json").read_text(encoding="utf-8"))


@pytest.fixture()
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "WTF_CSRF_ENABLED": False,
                      "RPL_DELAY_SECONDS": 0})
    with app.app_context():
        db.create_all()
        from app.cli import seed_dictionaries
        seed_dictionaries(BASE_DIR / "seed")
        yield app
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()
