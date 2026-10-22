"""Kết nối Postgres cho voicebot_callback.

URL lấy từ:
    - tham số `connect(url)` nếu được truyền
    - biến môi trường CALLBACK_DB_URL
Phải là `postgresql://user:pass@host:port/dbname` (hoặc `postgres://`).
Không hỗ trợ driver nào khác để tránh lệch hành vi giữa dev và production.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_SCHEMA = Path(__file__).with_name("schema_postgres.sql")

_PG_PREFIXES = ("postgresql://", "postgres://")


class Conn:
    """Bọc psycopg Connection: thống nhất placeholder, lastrowid, transaction."""

    def __init__(self, raw):
        self._raw = raw

    @property
    def driver(self) -> str:
        return "postgres"

    def _adapt(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        return self._raw.execute(self._adapt(sql), params)

    def insert(self, sql: str, params, pk: str):
        """INSERT trả về id của hàng vừa chèn qua RETURNING."""
        cur = self._raw.execute(self._adapt(sql) + f" RETURNING {pk}", params)
        row = cur.fetchone()
        return row[pk] if isinstance(row, dict) else row[0]

    def begin(self) -> None:
        self._raw.execute("BEGIN")

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def connect(url: str = "") -> Conn:
    url = url or os.environ.get("CALLBACK_DB_URL", "")
    if not url.startswith(_PG_PREFIXES):
        raise RuntimeError(
            "Cần URL Postgres (postgresql://...) qua tham số connect() "
            "hoặc biến môi trường CALLBACK_DB_URL. "
            "Chạy `docker compose up` để dựng Postgres sẵn."
        )

    import psycopg
    from psycopg.rows import dict_row

    # Container PG mới lên có thể chưa accept connect dù pg_isready đã OK -> retry
    last = None
    for _ in range(20):
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
        cur.execute(_SCHEMA.read_text(encoding="utf-8"))
    raw.commit()
    return Conn(raw)


def reset(conn: Conn) -> None:
    """Xóa sạch dữ liệu demo/test, không drop bảng."""
    for t in ("outbox", "sms_log", "schedule", "decisions", "llm_labels",
              "call_attempts", "alerts", "cases"):
        conn.execute(f"DELETE FROM {t}", ())
    conn.commit()
