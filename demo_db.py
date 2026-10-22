"""Mô phỏng cả luồng: cảnh báo -> gọi -> kết quả -> quyết định -> lịch mới, lưu đầy đủ trong SQLite.

Dữ liệu giả. In timeline của case và nội dung outbox (việc chờ đẩy ra agent của Đạt/SMS gateway/ĐTV).
Chạy: python demo_db.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from callback import Action, Alert, Outcome, load_config
from db import CallbackService, LlmLabel, connect

VN = timezone(timedelta(hours=7))
PRIMARY, ALT = "0900000001", "0900000002"


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=VN)


def label(outcome):
    return LlmLabel(outcome, 0.95, model="demo", prompt_version="v1",
                    raw_output=json.dumps({"outcome": outcome.value}, ensure_ascii=False))


def tick(svc, now):
    """Scheduler nhận lịch tới hạn, kiểm tra trước khi quay số. Trả về (schedule, decision) nếu được gọi."""
    for call in svc.claim_due_calls(now, "demo-worker"):
        d = svc.start_call(call.schedule_id, now)
        if d.action is Action.DIAL_NOW:
            yield call, d


def print_timeline(svc, case_id):
    print(f"    Timeline case {case_id}:")
    for step in svc.timeline(case_id):
        when = step["decided_at"].strftime("%d/%m %H:%M")
        nxt = step["next_call_at"].strftime(" -> gọi %d/%m %H:%M") if step["next_call_at"] else ""
        print(f"      {when} [{step['kind']:<10}] {step['action']:<14} ({step['rule_id']}) {step['reason']}{nxt}")


def print_outbox(svc):
    events = svc.pending_outbox()
    if not events:
        return
    print("    Outbox (chờ hệ thống ngoài đọc):")
    for ev in events:
        payload = json.loads(ev["payload"])
        head = payload.get("priority") or payload.get("template") or payload.get("status") or ""
        print(f"      {ev['event_type']:<12} {head:<10} case={ev['case_id']}")


def demo_merge():
    print("\n=== Case P2 (thẻ đã khóa) rồi thêm cảnh báo thứ 2 -> nâng P1, gọi sớm hơn")
    svc = CallbackService(connect(), load_config(), config_version="demo-v1")
    svc.ingest_alert(Alert("A1", "CARD-1", "KH1", 2_000_000, approved=False, card_blocked=True,
                           created_at=at(21, 9)), PRIMARY, at(21, 9), phone_sms=ALT)
    for call, _ in tick(svc, at(21, 9)):
        svc.record_call_result(call.case_id, "c1", call.phone, at(21, 9), at(21, 9, 1), "No Answer")
    # 5 phút sau cảnh báo thứ 2 tới
    r = svc.ingest_alert(Alert("A2", "CARD-1", "KH1", 2_000_000, approved=True, card_blocked=False,
                               created_at=at(21, 9, 5)), PRIMARY, at(21, 9, 5))
    print(f"    Gộp cảnh báo: urgency = {r.decision.outcome or ''} -> {r.decision.action.value}, "
          f"lịch mới {r.decision.next_call_at:%d/%m %H:%M}")
    print_timeline(svc, r.case_id)


def demo_scenarios():
    print("\n=== Case P1: không nghe, lỗi hệ thống, SMS, KH xác nhận")
    svc = CallbackService(connect(), load_config(), config_version="demo-v1")
    svc.ingest_alert(Alert("A1", "CARD-1", "KH1", 2_000_000, approved=True, card_blocked=False,
                           created_at=at(21, 9)), PRIMARY, at(21, 9), phone_sms=ALT)

    for call, _ in tick(svc, at(21, 9)):
        svc.record_call_result(call.case_id, "c1", call.phone, at(21, 9), at(21, 9, 1), "No Answer")
    for call, _ in tick(svc, at(21, 9, 11)):
        svc.record_call_result(call.case_id, "c2", call.phone, at(21, 9, 11), at(21, 9, 12), "Failed", sip_code=503)
    for call, _ in tick(svc, at(21, 9, 13)):
        svc.record_call_result(call.case_id, "c3", call.phone, at(21, 9, 13), at(21, 9, 14), "Completed",
                               answered_seconds=55, llm=label(Outcome.VERIFIED_CONFIRMED))
    print_timeline(svc, "CASE-A1")
    print_outbox(svc)


def demo_dedup_and_recovery():
    print("\n=== Cảnh báo trùng + cuộc gọi mất kết quả (lost -> system_error)")
    svc = CallbackService(connect(), load_config(), config_version="demo-v1")
    svc.ingest_alert(Alert("A1", "CARD-1", "KH1", 2_000_000, approved=False, card_blocked=True,
                           created_at=at(21, 9)), PRIMARY, at(21, 9))
    r = svc.ingest_alert(Alert("A1", "CARD-1", "KH1", 2_000_000, approved=False, card_blocked=True,
                               created_at=at(21, 9)), PRIMARY, at(21, 9, 0))
    print(f"    Cảnh báo trùng A1: duplicate = {r.duplicate}")

    for call, _ in tick(svc, at(21, 9)):
        pass  # không gọi record_call_result -> xem như agent chết giữa chừng
    lost = svc.recover_lost_calls(at(21, 9, 16))
    for r in lost:
        print(f"    Khôi phục cuộc mất: outcome = {r.decision.outcome.value}, "
              f"gọi lại {r.decision.next_call_at:%d/%m %H:%M}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    demo_scenarios()
    demo_merge()
    demo_dedup_and_recovery()


if __name__ == "__main__":
    main()
