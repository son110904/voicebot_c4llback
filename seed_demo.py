"""Tạo DB demo với 5 kịch bản điển hình để mentor xem trên dashboard.

Chạy: python seed_demo.py  -> sinh file demo.db
Sau đó: python app.py       -> mở dashboard
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from callback import Alert, Outcome, load_config
from db import CallbackService, LlmLabel, connect
from scheduler import DialResult, Scheduler, SchedulerConfig

VN = timezone(timedelta(hours=7))
PRIMARY = "0901234567"
ALT = "0987654321"
DB_PATH = os.environ.get("CALLBACK_DB", "demo.db")


def at(day, h, m=0, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=VN)


def label(outcome, confidence=0.95, callback_at=None):
    return LlmLabel(outcome, confidence, callback_at=callback_at,
                    model="demo", prompt_version="v1")


class Scripted:
    """Dialer giả: trả kết quả đã nạp sẵn theo thứ tự."""
    def __init__(self, *results):
        self.queue = list(results)

    def __call__(self, call, decision):
        return self.queue.pop(0)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def run_case(svc, clock, alert_in, dialer):
    """Nạp cảnh báo, cho scheduler chạy cho đến khi dialer hết kết quả."""
    svc.ingest_alert(alert_in, PRIMARY, clock.now, phone_sms=ALT)
    s = Scheduler(svc, dialer, SchedulerConfig(tick_s=10_000), clock=clock)
    for _ in range(20):
        due = svc.next_due_at()
        if due is None or not dialer.queue:
            break
        clock.now = max(clock.now, due)
        s.tick()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    cfg = load_config()
    conn = connect(DB_PATH)
    svc = CallbackService(conn, cfg, config_version="demo-v1")

    # 1. P1 - KH xác nhận ngay (happy path)
    run_case(svc, Clock(at(21, 10)),
             Alert("A001", "CARD-A", "KH-A", 15_000_000, approved=True, card_blocked=False,
                   created_at=at(21, 10)),
             Scripted(DialResult("c1", "Completed", answered_seconds=55,
                                 llm=label(Outcome.VERIFIED_CONFIRMED), verified=True)))

    # 2. P2 - không nghe máy nhiều lần, cuối cùng KH xác nhận
    run_case(svc, Clock(at(21, 14)),
             Alert("A002", "CARD-B", "KH-B", 2_500_000, approved=False, card_blocked=True,
                   created_at=at(21, 14)),
             Scripted(DialResult("c2a", "No Answer"),
                      DialResult("c2b", "No Answer"),
                      DialResult("c2c", "Completed", answered_seconds=42,
                                 llm=label(Outcome.VERIFIED_CONFIRMED))))

    # 3. P3 - KH phủ nhận giao dịch -> chuyển ĐTV (nghi gian lận thật)
    run_case(svc, Clock(at(21, 15)),
             Alert("A003", "CARD-C", "KH-C", 8_000_000, approved=False, card_blocked=False,
                   created_at=at(21, 15)),
             Scripted(DialResult("c3", "Completed", answered_seconds=38,
                                 llm=label(Outcome.VERIFIED_DENIED))))

    # 4. P1 - lỗi hệ thống rồi KH hẹn gọi lại
    run_case(svc, Clock(at(21, 16)),
             Alert("A004", "CARD-D", "KH-D", 60_000_000, approved=True, card_blocked=False,
                   created_at=at(21, 16)),
             Scripted(DialResult("c4a", "Failed", sip_code=503),       # lỗi SIP trunk
                      DialResult("c4b", "Completed", answered_seconds=25,
                                 llm=label(Outcome.CALLBACK_REQUESTED,
                                           callback_at=at(21, 16, 45))),
                      DialResult("c4c", "Completed", answered_seconds=50,
                                 llm=label(Outcome.VERIFIED_CONFIRMED))))

    # 5. P2 - nhầm số rồi hết lượt
    clock = Clock(at(21, 8))
    svc.ingest_alert(Alert("A005", "CARD-E", "KH-E", 3_000_000, approved=False, card_blocked=True,
                           created_at=at(21, 8)), PRIMARY, clock.now)  # không có số dự phòng
    s = Scheduler(svc, Scripted(
        DialResult("c5a", "Completed", answered_seconds=15, llm=label(Outcome.WRONG_NUMBER)),
    ), SchedulerConfig(tick_s=10_000), clock=clock)
    s.tick()

    print(f"Đã tạo {DB_PATH} với 5 case demo.")
    print("Chạy tiếp:  python app.py")


if __name__ == "__main__":
    main()
