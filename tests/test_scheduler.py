"""Test scheduler: nhận lịch, gọi, ghi kết quả, phân phối outbox."""
import unittest
from datetime import datetime, timedelta, timezone

from callback import Alert, Outcome, load_config
from db import CallbackService, LlmLabel, connect
from scheduler import DialResult, Scheduler, SchedulerConfig

VN = timezone(timedelta(hours=7))
PRIMARY = "0900000001"


def at(day, h, m=0, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=VN)


class FakeDialer:
    """Trả lần lượt kết quả đã nạp; cuộc gọi không có kết quả -> ném exception (giả lập agent chết)."""
    def __init__(self, *results, call_fail: int = 0):
        self.queue = list(results)
        self.call_fail = call_fail
        self.calls = 0

    def __call__(self, call, decision):
        self.calls += 1
        if self.call_fail > 0:
            self.call_fail -= 1
            raise RuntimeError("agent chết")
        return self.queue.pop(0)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def new_svc():
    return CallbackService(connect(), load_config(), config_version="sched-test")


def seed(svc, now, approved=False, blocked=True):
    svc.ingest_alert(Alert("A1", "CARD-1", "KH1", 2_000_000, approved=approved,
                           card_blocked=blocked, created_at=now), PRIMARY, now)


class SchedulerTest(unittest.TestCase):
    def test_runs_one_call(self):
        svc = new_svc()
        clock = Clock(at(22, 9))
        seed(svc, at(22, 9))
        s = Scheduler(svc, FakeDialer(DialResult("call-1", "No Answer")),
                      SchedulerConfig(worker_id="w1"), clock=clock)
        self.assertEqual(s.tick(), 1)
        due = svc.conn.execute("SELECT due_at FROM schedule WHERE status='PENDING'").fetchone()["due_at"]
        from db.repo import from_ts
        self.assertEqual(from_ts(due, VN), at(22, 9, 10))     # 10 phút sau cuộc gọi (clock cố định)
        self.assertEqual(s.tick(), 0)       # chưa tới giờ

    def test_sleep_until_next_due(self):
        svc = new_svc()
        clock = Clock(at(22, 9))
        seed(svc, at(22, 9))
        s = Scheduler(svc, FakeDialer(DialResult("c1", "No Answer")),
                      SchedulerConfig(tick_s=10_000), clock=clock)   # tick_s lớn để không che
        s.tick()
        clock.now = at(22, 9, 1, 30)        # còn 8 phút 30 giây tới 09:10
        self.assertAlmostEqual(s.sleep_s(), 8 * 60 + 30, delta=1)
        clock.now = at(22, 10)              # đã quá hạn -> ngủ 0
        self.assertEqual(s.sleep_s(), 0.0)

    def test_dialer_crash_recovered_as_system_error(self):
        svc = new_svc()
        clock = Clock(at(22, 9))
        seed(svc, at(22, 9))
        s = Scheduler(svc, FakeDialer(DialResult("c2", "Completed", answered_seconds=50,
                                                 llm=LlmLabel(Outcome.VERIFIED_CONFIRMED, 0.95)),
                                      call_fail=1), clock=clock)
        s.tick()                             # lần 1: dialer ném -> không ghi kết quả, lịch DIALING
        self.assertEqual(svc.conn.execute("SELECT COUNT(*) FROM call_attempts").fetchone()[0], 0)
        clock.now = at(22, 9, 16)            # 15 phút sau: recover_lost_calls kích hoạt
        s.tick()                             # recover ghi system_error, next_call_at = 09:18
        clock.now = at(22, 9, 18)
        s.tick()                             # nhận lịch mới + gọi thành công
        rows = svc.conn.execute("SELECT outcome FROM call_attempts ORDER BY attempt_id").fetchall()
        self.assertEqual([r["outcome"] for r in rows], ["system_error", "verified_confirmed"])

    def test_outbox_dispatched(self):
        svc = new_svc()
        clock = Clock(at(22, 9))
        seed(svc, at(22, 9), approved=True, blocked=False)       # P1 -> SMS ngay sau lần hụt đầu
        sent = []
        s = Scheduler(svc, FakeDialer(DialResult("c1", "No Answer")), dispatcher=sent.append, clock=clock)
        s.tick()
        kinds = sorted(e["event_type"] for e in sent)
        self.assertIn("SMS", kinds)
        self.assertEqual(svc.pending_outbox(), [])

    def test_outbox_dispatcher_failure_keeps_pending(self):
        svc = new_svc()
        seed(svc, at(22, 9), approved=True, blocked=False)
        def bad(_):
            raise RuntimeError("gateway down")
        s = Scheduler(svc, FakeDialer(DialResult("c1", "No Answer")), dispatcher=bad, clock=Clock(at(22, 9)))
        s.tick()
        remaining = svc.pending_outbox()
        self.assertTrue(any("gateway down" in (e["last_error"] or "") for e in remaining))


if __name__ == "__main__":
    unittest.main()
