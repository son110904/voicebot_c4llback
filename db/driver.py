"""Lớp mỏng cho phép repo.py chạy trên cả SQLite (dev/demo) lẫn Postgres (UAT/prod).

Chọn driver qua URL:
    sqlite:///demo.db          hoặc đường dẫn trần: /path/to.db, :memory:
    postgresql://user:pass@host:5432/dbname

Mặc định: đọc biến môi trường CALLBACK_DB_URL, fallback về SQLite in-memory.
"""
from __future__ import annotations

import os   
import sqlite3
from pathlib import Path

_SQLITE_SCHEMA = Path(__file__).with_name("schema_sqlite.sql")
_PG_SCHEMA = Path(__file__).with_name("schema_postgres.sql")

DEFAULT_URL = ":memory:"


class Conn:
    """Giao diện tối giản: execute(sql, params), insert(sql, params, pk), begin/commit/rollback, close."""

    def __init__(self, raw, placeholder: str, use_returning: bool):
        self._raw = raw
        self._ph = placeholder
        self._returning = use_returning

    @property
    def driver(self) -> str:
        return "postgres" if self._ph == "%s" else "sqlite"

    def _adapt(self, sql: str) -> str:
        return sql.replace("?", self._ph) if self._ph != "?" else sql

    def execute(self, sql, params=()):
        return self._raw.execute(self._adapt(sql), params)

    def executescript(self, sql: str) -> None:
        """Nhiều statement trong 1 string (chỉ dùng khi khởi tạo schema)."""
        if self._ph == "?":
            self._raw.executescript(sql)
        else:
            with self._raw.cursor() as cur:
                cur.execute(sql)

    def insert(self, sql: str, params, pk: str):
        """INSERT và trả về id của hàng vừa chèn (dùng RETURNING hoặc lastrowid)."""
        if self._returning:
            sql2 = self._adapt(sql) + f" RETURNING {pk}"
            cur = self._raw.execute(sql2, params)
            row = cur.fetchone()
            return row[pk] if isinstance(row, dict) else row[0]
        cur = self._raw.execute(sql, params)
        return cur.lastrowid

    def begin(self) -> None:
        self._raw.execute("BEGIN IMMEDIATE" if self._ph == "?" else "BEGIN")

    def commit(self) -> None:
        if self._ph == "?":
            self._raw.execute("COMMIT")
        else:
            self._raw.commit()

    def rollback(self) -> None:
        if self._ph == "?":
            self._raw.execute("ROLLBACK")
        else:
            self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def connect(url: str = "") -> Conn:
    url = url or os.environ.get("CALLBACK_DB_URL") or DEFAULT_URL
    if url.startswith(("postgresql://", "postgres://")):
        return _postgres(url)
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///"):] or DEFAULT_URL
    elif url.startswith("sqlite://"):
        url = url[len("sqlite://"):] or DEFAULT_URL
    return _sqlite(url)


def _sqlite(path: str) -> Conn:
    raw = sqlite3.connect(path, isolation_level=None)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        raw.execute("PRAGMA journal_mode = WAL")
    raw.executescript(_SQLITE_SCHEMA.read_text(encoding="utf-8"))
    return Conn(raw, placeholder="?", use_returning=False)


def _postgres(url: str) -> Conn:
    import time
    import psycopg
    from psycopg.rows import dict_row
    last = None
    for i in range(20):
        try:
            raw = psycopg.connect(url, autocommit=False, row_factory=dict_row,
                                  connect_timeout=5)
            break
        except (psycopg.OperationalError, psycopg.errors.ConnectionTimeout) as e:
            last = e
            time.sleep(1)
    else:
        raise RuntimeError(f"Không connect được Postgres sau 20 lần thử: {last}")

    with raw.cursor() as cur:
        cur.execute(_PG_SCHEMA.read_text(encoding="utf-8"))
    raw.commit()
    return Conn(raw, placeholder="%s", use_returning=True)
