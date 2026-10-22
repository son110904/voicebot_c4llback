"""Đọc/ghi SQLite: chỉ có SQL và chuyển đổi kiểu, không có luật nghiệp vụ."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timezone, tzinfo
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from callback.models import Action, Alert, Attempt, Case, CaseStatus, Decision, Group, Outcome, Urgency

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class ConcurrentUpdateError(RuntimeError):
    """Case đã bị tiến trình khác sửa giữa lúc đọc và lúc ghi."""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # tự quản lý transaction bằng BEGIN IMMEDIATE
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE giữ khóa ghi từ đầu -> 2 worker không cùng đọc-rồi-ghi 1 case."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.utcoffset() is None:
        raise ValueError("Mọi thời điểm phải có múi giờ (timezone-aware datetime)")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_ts(s: str | None, tz: tzinfo) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s).astimezone(tz)


def _marks(n: int) -> str:
    return ", ".join("?" * n)


# ------------------------------------------------------------------ cases

_CASE_COLS = ("case_id", "card_ref", "customer_id", "phone", "phone_sms", "urgency", "status", "card_open",
              "alert_count", "sms_sent", "appointments", "do_not_call", "blocked_phones", "created_at")


def _case_values(c: Case) -> tuple:
    return (c.case_id, c.card_ref, c.customer_id, c.phone, c.phone_sms, c.urgency.value, c.status.value,
            int(c.card_open), c.alert_count, c.sms_sent, c.appointments, int(c.do_not_call),
            json.dumps(sorted(c.blocked_phones)), ts(c.created_at))


def insert_case(conn, case: Case, now: datetime) -> None:
    conn.execute(f"INSERT INTO cases ({', '.join(_CASE_COLS)}, updated_at) VALUES ({_marks(len(_CASE_COLS) + 1)})",
                 (*_case_values(case), ts(now)))


def get_case(conn, case_id: str, tz: tzinfo) -> tuple[Case, int] | None:
    """Trả về (case, version); version dùng cho update_case."""
    r = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if r is None:
        return None
    case = Case(
        case_id=r["case_id"], card_ref=r["card_ref"], customer_id=r["customer_id"], phone=r["phone"],
        created_at=from_ts(r["created_at"], tz), urgency=Urgency(r["urgency"]), phone_sms=r["phone_sms"],
        status=CaseStatus(r["status"]), card_open=bool(r["card_open"]), alert_count=r["alert_count"],
        sms_sent=r["sms_sent"], appointments=r["appointments"], do_not_call=bool(r["do_not_call"]),
        blocked_phones=frozenset(json.loads(r["blocked_phones"])),
    )
    return case, r["version"]


def update_case(conn, case: Case, version: int, now: datetime) -> None:
    sets = ", ".join(f"{c} = ?" for c in _CASE_COLS[1:])
    cur = conn.execute(f"UPDATE cases SET {sets}, updated_at = ?, version = version + 1 "
                       f"WHERE case_id = ? AND version = ?",
                       (*_case_values(case)[1:], ts(now), case.case_id, version))
    if cur.rowcount != 1:
        raise ConcurrentUpdateError(f"Case {case.case_id} đã bị sửa bởi tiến trình khác")


def find_open_case(conn, card_ref: str, customer_id: str) -> str | None:
    """Case đang mở của cùng thẻ (ưu tiên) hoặc cùng KH."""
    r = conn.execute("SELECT case_id FROM cases WHERE status = 'OPEN' AND (card_ref = ? OR customer_id = ?) "
                     "ORDER BY card_ref = ? DESC, created_at LIMIT 1",
                     (card_ref, customer_id, card_ref)).fetchone()
    return r["case_id"] if r else None


def customer_do_not_call(conn, customer_id: str) -> bool:
    return conn.execute("SELECT 1 FROM cases WHERE customer_id = ? AND do_not_call = 1 LIMIT 1",
                        (customer_id,)).fetchone() is not None


# ------------------------------------------------------------------ alerts

def alert_case(conn, alert_id: str) -> str | None:
    r = conn.execute("SELECT case_id FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    return r["case_id"] if r else None


def insert_alert(conn, a: Alert, case_id: str, now: datetime) -> None:
    conn.execute("INSERT INTO alerts (alert_id, case_id, card_ref, customer_id, amount_vnd, approved, "
                 f"card_blocked, created_at, received_at) VALUES ({_marks(9)})",
                 (a.alert_id, case_id, a.card_ref, a.customer_id, a.amount_vnd, int(a.approved),
                  int(a.card_blocked), ts(a.created_at), ts(now)))


# ------------------------------------------------------------------ call_attempts / llm_labels

def attempt_by_call_id(conn, call_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM call_attempts WHERE call_id = ?", (call_id,)).fetchone()


def insert_attempt(conn, case_id: str, call_id: str, a: Attempt, *, ended_at: datetime, pbx_status: str,
                   sip_code: int | None, answered_seconds: float | None, verified: bool) -> int:
    cur = conn.execute("INSERT INTO call_attempts (call_id, case_id, phone, started_at, ended_at, pbx_status, "
                       f"sip_code, answered_seconds, verified, outcome, counted) VALUES ({_marks(11)})",
                       (call_id, case_id, a.phone, ts(a.started_at), ts(ended_at), pbx_status, sip_code,
                        answered_seconds, int(verified), a.outcome.value, int(a.counted)))
    return cur.lastrowid


def list_attempts(conn, case_id: str, tz: tzinfo) -> list[Attempt]:
    rows = conn.execute("SELECT started_at, phone, outcome, counted FROM call_attempts WHERE case_id = ? "
                        "ORDER BY started_at, attempt_id", (case_id,))
    return [Attempt(from_ts(r["started_at"], tz), r["phone"], Outcome(r["outcome"]), bool(r["counted"]))
            for r in rows]


def phone_history(conn, phones: Iterable[str], exclude_case_id: str, since: datetime,
                  tz: tzinfo) -> dict[str, list[datetime]]:
    """Các cuộc có tính lượt tới các số này từ case KHÁC, sau thời điểm since."""
    phones = list(phones)
    out: dict[str, list[datetime]] = {p: [] for p in phones}
    if not phones:
        return out
    rows = conn.execute("SELECT phone, started_at FROM call_attempts WHERE counted = 1 AND case_id != ? "
                        f"AND started_at > ? AND phone IN ({_marks(len(phones))}) ORDER BY started_at",
                        (exclude_case_id, ts(since), *phones))
    for r in rows:
        out[r["phone"]].append(from_ts(r["started_at"], tz))
    return out


def insert_llm_label(conn, attempt_id: int, label: Outcome, confidence: float | None,
                     callback_at: datetime | None, model: str | None, prompt_version: str | None,
                     raw_output: str | None, now: datetime) -> None:
    conn.execute("INSERT INTO llm_labels (attempt_id, label, confidence, callback_at, model, prompt_version, "
                 f"raw_output, created_at) VALUES ({_marks(8)})",
                 (attempt_id, label.value, confidence, ts(callback_at), model, prompt_version, raw_output, ts(now)))


# ------------------------------------------------------------------ decisions

def decision_to_json(d: Decision) -> str:
    out: dict[str, Any] = {}
    for f in fields(d):
        v = getattr(d, f.name)
        if isinstance(v, Enum):
            v = v.value
        elif isinstance(v, datetime):
            v = ts(v)
        out[f.name] = v
    return json.dumps(out, ensure_ascii=False)


def decision_from_json(s: str, tz: tzinfo) -> Decision:
    raw = json.loads(s)
    raw["action"] = Action(raw["action"])
    raw["group"] = Group(raw["group"]) if raw.get("group") else None
    raw["outcome"] = Outcome(raw["outcome"]) if raw.get("outcome") else None
    raw["next_call_at"] = from_ts(raw.get("next_call_at"), tz)
    return Decision(**raw)


def insert_decision(conn, case_id: str, attempt_id: int | None, kind: str, d: Decision,
                    config_version: str | None, now: datetime) -> int:
    cur = conn.execute("INSERT INTO decisions (case_id, attempt_id, kind, action, rule_id, reason, next_call_at, "
                       f"config_version, decided_at, detail) VALUES ({_marks(10)})",
                       (case_id, attempt_id, kind, d.action.value, d.rule_id, d.reason, ts(d.next_call_at),
                        config_version, ts(now), decision_to_json(d)))
    return cur.lastrowid


def decision_for_attempt(conn, attempt_id: int, tz: tzinfo) -> Decision | None:
    r = conn.execute("SELECT detail FROM decisions WHERE attempt_id = ? AND kind = 'after_call' "
                     "ORDER BY decision_id DESC LIMIT 1", (attempt_id,)).fetchone()
    return decision_from_json(r["detail"], tz) if r else None


def timeline(conn, case_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT decided_at, kind, action, rule_id, reason, next_call_at FROM decisions "
                        "WHERE case_id = ? ORDER BY decision_id", (case_id,)).fetchall()


# ------------------------------------------------------------------ schedule

def insert_schedule(conn, case_id: str, due_at: datetime, phone: str, appointment: bool,
                    decision_id: int, now: datetime) -> int:
    cur = conn.execute("INSERT INTO schedule (case_id, due_at, phone, appointment, status, decision_id, "
                       f"created_at, updated_at) VALUES ({_marks(8)})",
                       (case_id, ts(due_at), phone, int(appointment), "PENDING", decision_id, ts(now), ts(now)))
    return cur.lastrowid


def cancel_waiting(conn, case_id: str, now: datetime) -> None:
    """Hủy lịch chưa quay số (PENDING/CLAIMED). Cuộc DIALING thì để chờ kết quả."""
    conn.execute("UPDATE schedule SET status = 'CANCELLED', updated_at = ? "
                 "WHERE case_id = ? AND status IN ('PENDING', 'CLAIMED')", (ts(now), case_id))


def has_dialing(conn, case_id: str) -> bool:
    return conn.execute("SELECT 1 FROM schedule WHERE case_id = ? AND status = 'DIALING'",
                        (case_id,)).fetchone() is not None


def get_schedule(conn, schedule_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM schedule WHERE schedule_id = ?", (schedule_id,)).fetchone()


def mark_dialing(conn, schedule_id: int, phone: str, now: datetime) -> None:
    conn.execute("UPDATE schedule SET status = 'DIALING', phone = ?, updated_at = ? WHERE schedule_id = ?",
                 (phone, ts(now), schedule_id))


def finish_dialing(conn, case_id: str, now: datetime) -> None:
    conn.execute("UPDATE schedule SET status = 'DONE', updated_at = ? WHERE case_id = ? AND status = 'DIALING'",
                 (ts(now), case_id))


def claim_due(conn, now: datetime, worker: str, limit: int, stale_before: datetime) -> list[sqlite3.Row]:
    """Nhận các lịch tới giờ (P1 trước). Lịch CLAIMED quá lâu (worker chết) được nhận lại."""
    rows = conn.execute(
        "SELECT s.schedule_id FROM schedule s JOIN cases c ON c.case_id = s.case_id "
        "WHERE s.due_at <= ? AND (s.status = 'PENDING' OR (s.status = 'CLAIMED' AND s.claimed_at <= ?)) "
        "ORDER BY c.urgency, s.due_at LIMIT ?", (ts(now), ts(stale_before), limit)).fetchall()
    ids = [r["schedule_id"] for r in rows]
    for i in ids:
        conn.execute("UPDATE schedule SET status = 'CLAIMED', claimed_by = ?, claimed_at = ?, updated_at = ? "
                     "WHERE schedule_id = ?", (worker, ts(now), ts(now), i))
    return [get_schedule(conn, i) for i in ids]


def stale_dialing(conn, before: datetime) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM schedule WHERE status = 'DIALING' AND updated_at <= ? ORDER BY schedule_id",
                        (ts(before),)).fetchall()


def next_due(conn) -> str | None:
    r = conn.execute("SELECT MIN(due_at) AS t FROM schedule WHERE status = 'PENDING'").fetchone()
    return r["t"]


# ------------------------------------------------------------------ sms_log / outbox

def insert_sms(conn, case_id: str, decision_id: int, phone: str, template: str, body: str, now: datetime) -> None:
    conn.execute(f"INSERT INTO sms_log (case_id, decision_id, phone, template, body, created_at) "
                 f"VALUES ({_marks(6)})", (case_id, decision_id, phone, template, body, ts(now)))


def insert_outbox(conn, case_id: str, decision_id: int | None, event_type: str, payload: dict,
                  now: datetime) -> None:
    conn.execute(f"INSERT INTO outbox (case_id, decision_id, event_type, payload, created_at) VALUES ({_marks(5)})",
                 (case_id, decision_id, event_type, json.dumps(payload, ensure_ascii=False), ts(now)))


def pending_outbox(conn, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outbox WHERE status = 'PENDING' ORDER BY event_id LIMIT ?",
                        (limit,)).fetchall()


def mark_outbox(conn, event_id: int, now: datetime, ok: bool, error: str | None) -> None:
    if ok:
        conn.execute("UPDATE outbox SET status = 'SENT', tries = tries + 1, sent_at = ? WHERE event_id = ?",
                     (ts(now), event_id))
    else:
        conn.execute("UPDATE outbox SET tries = tries + 1, last_error = ? WHERE event_id = ?", (error, event_id))
