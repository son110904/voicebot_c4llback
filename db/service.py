"""Nối DB với rule engine.

    TWO/TWFA cảnh báo ─► ingest_alert() ─► schedule (PENDING)
    scheduler ─► claim_due_calls() ─► start_call() [kiểm tra lại case] ─► agent quay số
    tổng đài + LLM ─► record_call_result() ─► decide() ─► lịch gọi lại / SMS / ĐTV
    outbox ─► hệ thống ngoài (agent của Đạt, SMS gateway, hàng đợi ĐTV, vận hành) đọc rồi đánh dấu đã gửi

Mỗi hàm public chạy trong 1 transaction: hoặc ghi đủ (cuộc gọi + quyết định + lịch + SMS + outbox),
hoặc không ghi gì. `now` luôn do bên gọi truyền vào để test/mô phỏng được.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from callback import (
    OUTCOME_GROUP, Action, Alert, Attempt, Case, CaseStatus, Config, Decision, Event, Outcome,
    apply_decision, compute_urgency, decide, merge_alert, outcome_from_pbx, pre_dial_check,
)

from . import repo


@dataclass(frozen=True)
class ScheduledCall:
    schedule_id: int
    case_id: str
    phone: str
    due_at: datetime
    appointment: bool


@dataclass(frozen=True)
class IngestResult:
    case_id: str
    merged: bool
    duplicate: bool
    decision: Decision | None


@dataclass(frozen=True)
class CallResult:
    decision: Decision
    duplicate: bool

@dataclass(frozen=True)
class LlmLabel:
    """Nhãn LLM cho cuộc gọi có hội thoại (tầng LLM ở đợt 3 sẽ tạo ra cái này)."""
    label: Outcome
    confidence: float
    callback_at: datetime | None = None
    model: str | None = None
    prompt_version: str | None = None
    raw_output: str | None = None


class CallbackService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, config_version: str = "dev",
                 claim_ttl: timedelta = timedelta(minutes=5), dial_timeout: timedelta = timedelta(minutes=15)):
        self.conn = conn
        self.cfg = cfg
        self.tz = cfg.tz
        self.config_version = config_version
        self.claim_ttl = claim_ttl          # worker nhận lịch mà không xử lý trong khoảng này -> nhận lại
        self.dial_timeout = dial_timeout    # đã quay số mà quá khoảng này chưa có kết quả -> coi là lỗi hệ thống

    # ------------------------------------------------------------ cảnh báo

    def ingest_alert(self, alert: Alert, phone: str, now: datetime, phone_sms: str | None = None) -> IngestResult:
        """Nhận cảnh báo: trùng thì bỏ qua, có case mở của cùng thẻ/KH thì gộp, không thì tạo case mới."""
        with repo.transaction(self.conn) as c:
            dup = repo.alert_case(c, alert.alert_id)
            if dup:
                return IngestResult(dup, merged=False, duplicate=True, decision=None)

            open_id = repo.find_open_case(c, alert.card_ref, alert.customer_id)
            if open_id:
                case, version = repo.get_case(c, open_id, self.tz)
                attempts = repo.list_attempts(c, open_id, self.tz)
                r = merge_alert(case, alert, attempts, now, self.cfg, self._phone_history(c, case, now))
                if r is not None:
                    repo.insert_alert(c, alert, open_id, now)
                    in_flight = repo.has_dialing(c, open_id)
                    d = r.decision
                    if in_flight and d.action is Action.SCHEDULE_CALL:
                        d = replace(d, next_call_at=None,
                                    reason=d.reason + "; đang có cuộc gọi dở -> chờ kết quả cuộc đó")
                    self._apply(c, r.case, version, d, "alert", None, now, reschedule=not in_flight)
                    return IngestResult(open_id, merged=True, duplicate=False, decision=d)

            urgency, why = compute_urgency(alert, 1, self.cfg)
            case = Case(case_id=f"CASE-{alert.alert_id}", card_ref=alert.card_ref, customer_id=alert.customer_id,
                        phone=phone, phone_sms=phone_sms, created_at=now, urgency=urgency,
                        card_open=not alert.card_blocked,
                        # KH từng yêu cầu bot không gọi -> case mới cũng không gọi, chuyển ĐTV
                        do_not_call=repo.customer_do_not_call(c, alert.customer_id))
            repo.insert_case(c, case, now)
            repo.insert_alert(c, alert, case.case_id, now)
            d = pre_dial_check(case, [], phone, now, self.cfg, self._phone_history(c, case, now))
            d = replace(d, reason=f"Tạo case mới mức {urgency.value} ({why}); {d.reason}")
            self._apply(c, case, 0, d, "alert", None, now)
            return IngestResult(case.case_id, merged=False, duplicate=False, decision=d)

    # ------------------------------------------------------------ gọi

    def claim_due_calls(self, now: datetime, worker_id: str, limit: int = 10) -> list[ScheduledCall]:
        with repo.transaction(self.conn) as c:
            rows = repo.claim_due(c, now, worker_id, limit, now - self.claim_ttl)
        return [ScheduledCall(r["schedule_id"], r["case_id"], r["phone"], repo.from_ts(r["due_at"], self.tz),
                              bool(r["appointment"])) for r in rows]

    def start_call(self, schedule_id: int, now: datetime) -> Decision:
        """Kiểm tra lại ngay trước khi quay số. DIAL_NOW -> agent gọi số decision.target_phone."""
        with repo.transaction(self.conn) as c:
            s = repo.get_schedule(c, schedule_id)
            if s is None or s["status"] != "CLAIMED":
                raise ValueError(f"Lịch {schedule_id} không ở trạng thái CLAIMED")
            case, version = repo.get_case(c, s["case_id"], self.tz)
            attempts = repo.list_attempts(c, case.case_id, self.tz)
            d = pre_dial_check(case, attempts, s["phone"], now, self.cfg, self._phone_history(c, case, now),
                               appointment=bool(s["appointment"]))
            if d.action is Action.DIAL_NOW:
                repo.mark_dialing(c, schedule_id, d.target_phone, now)
                self._apply(c, case, version, d, "pre_dial", None, now, reschedule=False)
            else:
                self._apply(c, case, version, d, "pre_dial", None, now)
            return d

    def record_call_result(self, case_id: str, call_id: str, phone: str, started_at: datetime, now: datetime,
                           pbx_status: str, *, sip_code: int | None = None, answered_seconds: float | None = None,
                           machine_detected: bool = False, verified: bool = False,
                           llm: LlmLabel | None = None) -> CallResult:
        """Ghi kết quả cuộc gọi + quyết định bước tiếp. Cùng call_id gửi lại -> trả quyết định cũ, không ghi gì."""
        with repo.transaction(self.conn) as c:
            prev = repo.attempt_by_call_id(c, call_id)
            if prev is not None:
                return CallResult(repo.decision_for_attempt(c, prev["attempt_id"], self.tz), duplicate=True)
            got = repo.get_case(c, case_id, self.tz)
            if got is None:
                raise KeyError(f"Không có case {case_id}")
            case, version = got
            attempts = repo.list_attempts(c, case_id, self.tz)

            # nhãn LLM (cuộc có hội thoại) ưu tiên hơn trạng thái tổng đài
            outcome = llm.label if llm else outcome_from_pbx(pbx_status, sip_code, answered_seconds, machine_detected)
            if outcome is None:  # có hội thoại nhưng không có nhãn LLM -> không đoán, để ĐTV xem
                outcome = Outcome.UNCLEAR
            event = Event(started_at=started_at, phone=phone, outcome=outcome, verified=verified,
                          callback_at=llm.callback_at if llm else None,
                          llm_confidence=llm.confidence if llm else None)
            d = decide(case, attempts, event, now, self.cfg, self._phone_history(c, case, now))

            used = d.outcome or outcome  # d.outcome trống khi case đã đóng (G1)
            counted = d.counts_attempt if d.group else OUTCOME_GROUP[used][1]
            attempt_id = repo.insert_attempt(c, case_id, call_id, Attempt(started_at, phone, used, counted), ended_at=now, pbx_status=pbx_status,
                                             sip_code=sip_code, answered_seconds=answered_seconds, verified=verified)
            if llm:
                repo.insert_llm_label(c, attempt_id, llm.label, llm.confidence, llm.callback_at, llm.model,
                                      llm.prompt_version, llm.raw_output, now)
            repo.finish_dialing(c, case_id, now)
            self._apply(c, case, version, d, "after_call", attempt_id, now)
            return CallResult(d, duplicate=False)

    def recover_lost_calls(self, now: datetime) -> list[CallResult]:
        """Cuộc đã quay (DIALING) quá dial_timeout chưa có kết quả -> ghi là lỗi hệ thống để còn gọi lại."""
        rows = repo.stale_dialing(self.conn, now - self.dial_timeout)
        return [self.record_call_result(r["case_id"], f"lost-{r['schedule_id']}", r["phone"],
                                        repo.from_ts(r["updated_at"], self.tz), now, "Failed")
                for r in rows]

    def close_case(self, case_id: str, now: datetime, reason: str) -> None:
        """Đóng từ bên ngoài (ĐTV đã xử lý, TWO đóng cảnh báo...). Lịch chưa gọi bị hủy."""
        with repo.transaction(self.conn) as c:
            case, version = repo.get_case(c, case_id, self.tz)
            if case.status is not CaseStatus.OPEN:
                return
            repo.update_case(c, replace(case, status=CaseStatus.CLOSED), version, now)
            d = Decision(Action.CLOSE, "X1", f"Case đóng từ bên ngoài: {reason}")
            repo.insert_decision(c, case_id, None, "external", d, self.config_version, now)
            repo.cancel_waiting(c, case_id, now)

    # ------------------------------------------------------------ đọc / outbox

    def next_due_at(self) -> datetime | None:
        return repo.from_ts(repo.next_due(self.conn), self.tz)

    def timeline(self, case_id: str) -> list[dict]:
        return [{**dict(r), "decided_at": repo.from_ts(r["decided_at"], self.tz),
                 "next_call_at": repo.from_ts(r["next_call_at"], self.tz)}
                for r in repo.timeline(self.conn, case_id)]

    def pending_outbox(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in repo.pending_outbox(self.conn, limit)]

    def mark_outbox(self, event_id: int, now: datetime, ok: bool = True, error: str | None = None) -> None:
        with repo.transaction(self.conn) as c:
            repo.mark_outbox(c, event_id, now, ok, error)

    # ------------------------------------------------------------ nội bộ

    def _phone_history(self, c, case: Case, now: datetime):
        phones = [p for p in (case.phone, case.phone_sms) if p]
        return repo.phone_history(c, phones, case.case_id, now - self.cfg.rolling_window, self.tz)

    def _apply(self, c, case: Case, version: int, d: Decision, kind: str, attempt_id: int | None,
               now: datetime, reschedule: bool = True) -> int:
        """Ghi hệ quả của 1 quyết định: case, log quyết định, lịch, SMS, outbox."""
        new = apply_decision(case, d)
        repo.update_case(c, new, version, now)
        did = repo.insert_decision(c, case.case_id, attempt_id, kind, d, self.config_version, now)

        if reschedule:
            repo.cancel_waiting(c, case.case_id, now)
            # DIAL_NOW lúc nhận cảnh báo = "gọi ngay" -> xếp lịch tới hạn luôn cho scheduler nhận
            if d.action is Action.SCHEDULE_CALL or (d.action is Action.DIAL_NOW and kind != "pre_dial"):
                repo.insert_schedule(c, case.case_id, d.next_call_at or now, d.target_phone, d.appointment, did, now)

        if d.sms_template:
            body = self.cfg.sms_templates[d.sms_template]
            repo.insert_sms(c, case.case_id, did, d.sms_to, d.sms_template, body, now)
            repo.insert_outbox(c, case.case_id, did, "SMS", {"to": d.sms_to, "template": d.sms_template,
                                                              "body": body}, now)
        if d.action is Action.HAND_TO_DTV or d.dtv_priority:
            repo.insert_outbox(c, case.case_id, did, "DTV_HANDOFF",
                               {"priority": d.dtv_priority or "NORMAL", "ticket": d.ticket,
                                "rule_id": d.rule_id, "reason": d.reason}, now)
        if d.ops_alert:
            repo.insert_outbox(c, case.case_id, did, "OPS_ALERT", {"message": d.ops_alert}, now)
        if case.status is CaseStatus.OPEN and new.status is not CaseStatus.OPEN:
            repo.insert_outbox(c, case.case_id, did, "CASE_FINAL",
                               {"status": new.status.value, "ticket": d.ticket, "rule_id": d.rule_id,
                                "reason": d.reason}, now)
        return did
