"""SQLite local del edge (Peewee), igual que en los edge de referencia: una sola base compartida por los contextos."""
from __future__ import annotations

from pathlib import Path

from peewee import SqliteDatabase

db = SqliteDatabase(None)


def init_db(path: str | Path, models: list[type]) -> None:
    """Abre la base y crea las tablas que falten. `path` ':memory:' sirve para pruebas."""
    if not db.is_closed():
        db.close()
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db.init(str(path), pragmas={"journal_mode": "wal", "foreign_keys": 1})
    db.connect(reuse_if_open=True)
    db.create_tables(models)


def close_db() -> None:
    if not db.is_closed():
        db.close()
