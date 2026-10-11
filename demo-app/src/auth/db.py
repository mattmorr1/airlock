"""SQLite helpers for the demo auth app.

The target application uses fake credentials and an isolated test database; it
never touches Atlas or any real secret (spec section 4). DB path and log path
come from the environment so the trusted runner can point them at disposable
locations and inspect final state.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def db_path() -> str:
    return os.environ.get("APP_DB", "/tmp/demo-app/auth.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str) -> str:
    return hashlib.sha256(("demo-salt::" + password).encode("utf-8")).hexdigest()


def migrate(conn: sqlite3.Connection) -> None:
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(sql_file.read_text())
    conn.commit()


def seed(conn: sqlite3.Connection) -> None:
    """One known user so valid/invalid login scenarios are deterministic."""
    conn.execute(
        "INSERT OR IGNORE INTO users (email, password_hash) VALUES (?, ?)",
        ("alice@example.com", hash_password("correct-horse-battery")),
    )
    conn.commit()


def init_db() -> None:
    conn = connect()
    try:
        migrate(conn)
        seed(conn)
    finally:
        conn.close()
