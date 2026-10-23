"""Prosta aktualizacja schematu bazy bez narzędzia do migracji.

Gdy nowa wersja kodu dodaje tabele albo kolumny, `flask upgrade-db` (wywoływane też przez `flask init-db`)
tworzy brakujące tabele i dopisuje brakujące kolumny. Istniejące dane zostają. Nie obsługuje zmiany typu
ani usuwania kolumn; gdyby kiedyś były potrzebne, trzeba będzie dodać prawdziwe migracje (Alembic).
"""
from __future__ import annotations

import sqlalchemy as sa

from . import db


def missing_columns() -> list[tuple[sa.Table, sa.Column]]:
    inspector = sa.inspect(db.engine)
    tables = set(inspector.get_table_names())
    out = []
    for table in db.metadata.sorted_tables:
        if table.name not in tables:
            continue                      # całą tabelę utworzy create_all
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        out.extend((table, col) for col in table.columns if col.name not in existing)
    return out


def needs_upgrade() -> bool:
    """Czy istniejąca baza aplikacji jest starsza niż kod (pusta, nowa baza się nie liczy)."""
    inspector = sa.inspect(db.engine)
    tables = set(inspector.get_table_names())
    ours = {t.name for t in db.metadata.sorted_tables}
    if not tables & ours:
        return False
    return bool(missing_columns()) or bool(ours - tables)


def _column_sql(col: sa.Column) -> str:
    dialect = db.engine.dialect
    sql = f'"{col.name}" {col.type.compile(dialect=dialect)}'
    default = col.default.arg if col.default is not None and col.default.is_scalar else None
    if default is not None:
        literal = sa.literal(default, col.type).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        sql += f" DEFAULT {literal}"
        if not col.nullable:
            sql += " NOT NULL"
    # kolumna NOT NULL bez wartości domyślnej jest dodawana jako dopuszczająca NULL (inaczej SQLite odmówi)
    return sql


def upgrade_schema() -> list[str]:
    """Tworzy brakujące tabele i dodaje brakujące kolumny. Zwraca listę dodanych elementów."""
    inspector = sa.inspect(db.engine)
    before = set(inspector.get_table_names())
    added = [f"tabela {t.name}" for t in db.metadata.sorted_tables if t.name not in before]
    db.create_all()
    for table, col in missing_columns():
        if col.primary_key:
            raise RuntimeError(f"Nie da się dodać klucza głównego {table.name}.{col.name}; utwórz bazę od nowa.")
        with db.engine.begin() as conn:
            conn.execute(sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN {_column_sql(col)}'))
        added.append(f"{table.name}.{col.name}")
    return added
