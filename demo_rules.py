"""Mô phỏng vài case từ đầu đến cuối
Dữ liệu hoàn toàn giả
Chạy: python demo_rules.py
"""
import sys
from datetime import datetime, timedelta, timezone

from callback import (
    Action, Attempt, Case, Event, Outcome, Urgency, apply_decision, decide, load_config, pre_dial_check,
)

VN = timezone(timedelta(hours=7))
PRIMARY, ALT = "0900000001", "0900000002"


def simulate(title, case, script, cfg):
    """script: danh sách Outcome, hoặc (Outcome, {"callback_after": phút}) cho cuộc KH hẹn gọi lại."""
    print(f"\n=== {title}")
    print(f"    mức {case.urgency.value}, tạo lúc {case.created_at:%d/%m %H:%M}")
    attempts: list[Attempt] = []
    t, phone, appointment = case.created_at, case.phone, False
    for item in script:
        outcome, extra = item if isinstance(item, tuple) else (item, {})
        check = pre_dial_check(case, attempts, phone, t, cfg, appointment=appointment)
        while check.action is Action.SCHEDULE_CALL:  # chưa tới giờ được gọi -> dời
            t = check.next_call_at
            check = pre_dial_check(case, attempts, phone, t, cfg, appointment=appointment)
        if check.action is not Action.DIAL_NOW:
            print(f"    {t:%d/%m %H:%M} trước khi gọi: {check.action.value} - {check.reason}")
            return
        phone = check.target_phone
        callback_at = t + timedelta(minutes=extra["callback_after"]) if "callback_after" in extra else None
        event = Event(started_at=t, phone=phone, outcome=outcome, callback_at=callback_at,
                      llm_confidence=0.9 if callback_at else None)
        d = decide(case, attempts, event, t + timedelta(minutes=1), cfg)
        line = f"{t:%d/%m %H:%M} số {'chính' if phone == case.phone else 'ZNS  '} {outcome.value:<20} -> {d.action.value}"
        if d.next_call_at:
            line += f" lúc {d.next_call_at:%d/%m %H:%M}"
        if d.zns_template:
            line += f" +ZNS[{d.zns_template}]"
        print(f"    {line}")
        print(f"        [{d.rule_id}] {d.reason}")
        attempts.append(Attempt(t, phone, d.outcome, d.counts_attempt))
        case = apply_decision(case, d)
        if d.action is not Action.SCHEDULE_CALL:
            return
        t, phone, appointment = d.next_call_at, d.target_phone, d.appointment


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config()

    def case(urgency, created, **kw):
        return Case(case_id="DEMO", card_ref="CARD-TOKEN-1", customer_id="KH-DEMO", phone=PRIMARY,
                    created_at=created, urgency=urgency, **kw)

    simulate("P2 thẻ đã khóa: không nghe, lỗi hệ thống, đổi số, Khách hẹn, xác nhận",
             case(Urgency.P2, datetime(2026, 10, 21, 20, 30, tzinfo=VN), phone_sms=ALT, card_open=False),
             [Outcome.BUSY, Outcome.SYSTEM_ERROR, Outcome.NO_ANSWER,
              (Outcome.CALLBACK_REQUESTED, {"callback_after": 120}), Outcome.VERIFIED_CONFIRMED], cfg)

    simulate("P1 thẻ còn mở, GD đã cấp phép: Khách không nghe máy cả ngày",
             case(Urgency.P1, datetime(2026, 10, 21, 9, 0, tzinfo=VN), phone_sms=ALT),
             [Outcome.NO_ANSWER] * 8, cfg)

    simulate("P3 GD bị từ chối: nhầm số, gọi số ZNS thì Khách phủ nhận giao dịch",
             case(Urgency.P3, datetime(2026, 10, 21, 7, 45, tzinfo=VN), phone_sms=ALT),
             [Outcome.WRONG_NUMBER, Outcome.VERIFIED_DENIED], cfg)


if __name__ == "__main__":
    main()
