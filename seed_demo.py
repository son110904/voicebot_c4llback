"""Tạo DB demo với các kịch bản điển hình để mentor xem trên dashboard.

Cần Postgres sẵn (docker compose up postgres), đọc URL từ CALLBACK_DB_URL.
Chạy: python seed_demo.py && python app.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from callback import Alert, Outcome, load_config
from db import CallbackService, LlmLabel, connect, reset
from scheduler import DialResult, Scheduler, SchedulerConfig

VN = timezone(timedelta(hours=7))
PRIMARY = "0901234567"
ALT = "0987654321"
DB_URL = os.environ.get("CALLBACK_DB_URL", "postgresql://postgres:cb@localhost:5432/callback")


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
    cfg = load_config()
    conn = connect(DB_URL)
    reset(conn)
    svc = CallbackService(conn, cfg, config_version="demo-v1")

    # 1. LLM tin cậy thấp, chuyển cho ĐTV, nhãn kỳ vọng unclear
    run_case(svc, Clock(at(21, 10)),
         Alert("A006", "CARD-F", "KH-F", 4_000_000, approved=False, card_blocked=True,
               created_at=at(21, 10)),
         Scripted(DialResult("c6", "Completed", answered_seconds=30,
                             llm=label(Outcome.VERIFIED_CONFIRMED, confidence=0.45))))

    # 2. Khách yêu cầu không liên hệ, dừng bot, kỳ vọng case dừng lại, không gọi lại
    run_case(svc, Clock(at(21, 11)),
         Alert("A007", "CARD-G", "KH-G", 1_200_000, approved=False, card_blocked=True,
               created_at=at(21, 11)),
         Scripted(DialResult("c7", "Completed", answered_seconds=25,
                             llm=label(Outcome.DO_NOT_CALL))))

    # 3. gọi số chính không được, thử số dự phòng, người nhà nghe, chủ thẻ confirm, kỳ vọng ng nhà nghe, bot lên lịch
    run_case(svc, Clock(at(21, 12)),
         Alert("A008", "CARD-H", "KH-H", 3_000_000, approved=False, card_blocked=True,
               created_at=at(21, 12)),
         Scripted(DialResult("c8a", "No Answer"),
                  DialResult("c8b", "No Answer"),
                  DialResult("c8c", "Completed", answered_seconds=20,
                             llm=label(Outcome.RELATIVE_ANSWERED)),
                  DialResult("c8d", "Completed", answered_seconds=40,
                             llm=label(Outcome.VERIFIED_CONFIRMED))))

    # 4. Khách hẹn quá số lần cho phép, chuyển ĐTV, sau hai lần hẹn, yêu cầu hẹn lần thứ 3 được chuyển ĐTV theo giới hạn cấu hình
    run_case(svc, Clock(at(21, 20)),
         Alert("A009", "CARD-I", "KH-I", 2_000_000, approved=False, card_blocked=True,
               created_at=at(21, 20)),
         Scripted(DialResult("c9a", "Completed", answered_seconds=20,
                             llm=label(Outcome.CALLBACK_REQUESTED, callback_at=at(21, 21, 30))),
                  DialResult("c9b", "Completed", answered_seconds=20,
                             llm=label(Outcome.CALLBACK_REQUESTED, callback_at=at(21, 21, 40))),
                  DialResult("c9c", "Completed", answered_seconds=20,
                             llm=label(Outcome.CALLBACK_REQUESTED, callback_at=at(21, 21, 50)))))

    # 5. Lỗi hệ thống liên tục, báo động và ĐTV gọi thủ công
    run_case(svc, Clock(at(21, 13)),
         Alert("A010", "CARD-J", "KH-J", 60_000_000, approved=True, card_blocked=False,
               created_at=at(21, 13)),
         Scripted(DialResult("c10a", "Failed", sip_code=503),
                  DialResult("c10b", "Failed", sip_code=503),
                  DialResult("c10c", "Failed", sip_code=503),
                  DialResult("c10d", "Failed", sip_code=503)))

    conn.commit()
    print(f"Đã nạp dữ liệu demo vào {DB_URL}")
    print("Chạy tiếp:  python app.py")


if __name__ == "__main__":
    main()
