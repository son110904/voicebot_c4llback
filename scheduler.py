"""Worker gọi lại khách hàng.

Vòng lặp:
    1. Khôi phục các cuộc gọi mất kết quả (agent chết) -> ghi system_error, tạo lịch mới.
    2. Nhận các lịch tới hạn (claim_due_calls) - ưu tiên P1.
    3. Với mỗi lịch: pre_dial_check; nếu DIAL_NOW thì để `dialer` quay số và trả kết quả về
       `record_call_result`.
    4. Đẩy outbox (ZNS/ĐTV handoff/ops alert) cho các dispatcher ngoài.
    5. Ngủ tới lịch kế tiếp hoặc tối đa `tick_s` giây.

`dialer` là callable thật/giả: nhận ScheduledCall + Decision, gọi tổng đài, trả về
(call_id, pbx_status, sip_code, answered_seconds, llm_label). Trong demo ta dùng dialer giả.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from callback import Action
from db.service import CallbackService, LlmLabel, ScheduledCall

log = logging.getLogger("scheduler")


@dataclass(frozen=True)
class DialResult:
    call_id: str
    pbx_status: str                       # Completed / No Answer / Busy / Failed / Transferred / Incomplete
    sip_code: int | None = None
    answered_seconds: float | None = None
    machine_detected: bool = False
    verified: bool = False
    llm: LlmLabel | None = None


class Dialer(Protocol):
    def __call__(self, call: ScheduledCall, decision) -> DialResult: ...


class OutboxDispatcher(Protocol):
    """Gửi 1 event outbox đi (ZNS gateway / agent của Đạt / hàng đợi ĐTV / báo động vận hành)."""
    def __call__(self, event: dict) -> None: ...


@dataclass
class SchedulerConfig:
    worker_id: str = "worker-1"
    tick_s: float = 10.0                  # chu kỳ tối đa khi không có lịch
    batch: int = 10                       # số cuộc gọi mỗi vòng
    outbox_batch: int = 50


class Scheduler:
    def __init__(self, svc: CallbackService, dialer: Dialer, cfg: SchedulerConfig | None = None,
                 dispatcher: OutboxDispatcher | None = None, clock: Callable[[], datetime] | None = None,
                 dtv_counter: Callable[[], int] | None = None):
        self.svc = svc
        self.dial = dialer
        self.cfg = cfg or SchedulerConfig()
        self.dispatcher = dispatcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dtv_counter = dtv_counter

    def tick(self) -> int:
        """Chạy 1 vòng. Trả về số cuộc đã quay. Dùng để test và nhịp vòng ngoài."""
        now = self.clock()
        self.svc.recover_lost_calls(now)
        n = 0
        for call in self.svc.claim_due_calls(now, self.cfg.worker_id, self.cfg.batch):
            try:
                dtv = self.dtv_counter() if self.dtv_counter else None
                d = self.svc.start_call(call.schedule_id, self.clock(), dtv_online_count=dtv)
            except Exception:
                log.exception("start_call lỗi, bỏ qua schedule %s", call.schedule_id)
                continue
            if d.action is not Action.DIAL_NOW:
                continue                   # pre_dial đã tự dời lịch / dừng
            try:
                r = self.dial(call, d)
                self.svc.record_call_result(
                    call.case_id, r.call_id, d.target_phone, self.clock(), self.clock(),
                    r.pbx_status, sip_code=r.sip_code, answered_seconds=r.answered_seconds,
                    machine_detected=r.machine_detected, verified=r.verified, llm=r.llm,
                )
            except Exception:
                log.exception("dial/record lỗi, chờ recover_lost_calls xử lý")
            n += 1
        if self.dispatcher is not None:
            self._dispatch(now)
        return n

    def _dispatch(self, now: datetime) -> None:
        for ev in self.svc.pending_outbox(self.cfg.outbox_batch):
            try:
                self.dispatcher(ev)
                self.svc.mark_outbox(ev["event_id"], now, ok=True)
            except Exception as e:
                log.exception("gửi outbox lỗi")
                self.svc.mark_outbox(ev["event_id"], now, ok=False, error=str(e)[:500])

    def sleep_s(self) -> float:
        """Thời gian ngủ tới lịch kế tiếp (giới hạn bởi tick_s)."""
        due = self.svc.next_due_at()
        if due is None:
            return self.cfg.tick_s
        return max(0.0, min(self.cfg.tick_s, (due - self.clock()).total_seconds()))

    def run_forever(self) -> None:
        while True:
            self.tick()
            time.sleep(self.sleep_s())


def new_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:12]}"
