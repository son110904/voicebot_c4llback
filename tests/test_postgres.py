"""Chạy lại 1 bộ test đại diện trên Postgres thật.

Mặc định bỏ qua. Để chạy:
    export CALLBACK_DB_URL=postgresql://user:pass@host:5432/dbname
    python -m unittest tests.test_postgres -v

Có thể dựng Postgres nhanh bằng Docker:
    docker run --rm -d --name pg-cb -e POSTGRES_PASSWORD=cb -p 5432:5432 postgres:16
    export CALLBACK_DB_URL=postgresql://postgres:cb@localhost:5432/postgres
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

from callback import Alert, Outcome, load_config
from db import CallbackService, LlmLabel, connect

URL = os.environ.get("CALLBACK_DB_URL", "")
if not URL.startswith(("postgres://", "postgresql://")):
    raise unittest.SkipTest("đặt CALLBACK_DB_URL=postgresql://... để chạy test này")

VN = timezone(timedelta(hours=7))


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=VN)


def _reset(conn):
    """Xóa dữ liệu cũ ở DB chung (không drop table, chỉ TRUNCATE)."""
    for t in ("outbox", "sms_log", "schedule", "decisions", "llm_labels",
              "call_attempts", "alerts", "cases"):
        conn.execute(f"DELETE FROM {t}", ())


class PgSmokeTest(unittest.TestCase):
    """Luồng ingest → dial → record cho cả 3 kịch bản điển hình."""

    @classmethod
    def setUpClass(cls):
        cls.conn = connect(URL)
        _reset(cls.conn)

    @classmethod
    def tearDownClass(cls):
        _reset(cls.conn)
        cls.conn.close()

    def setUp(self):
        _reset(self.conn)
        self.svc = CallbackService(self.conn, load_config(), config_version="pg-test")

    def _call(self, case_id, cid, start, status, **kw):
        return self.svc.record_call_result(case_id, cid, "0901111111", start,
                                           start + timedelta(minutes=1), status, **kw)

    def test_create_merge_and_close(self):
        a1 = Alert("P-A1", "CARD-X", "KH-X", 20_000_000, approved=True,
                   card_blocked=False, created_at=at(21, 9))
        self.svc.ingest_alert(a1, "0901111111", at(21, 9), phone_sms="0902222222")
        calls = self.svc.claim_due_calls(at(21, 9), "pg-worker")
        self.assertEqual(len(calls), 1)
        self.svc.start_call(calls[0].schedule_id, at(21, 9))

        # cảnh báo trùng -> bỏ qua
        self.assertTrue(self.svc.ingest_alert(a1, "0901111111", at(21, 9)).duplicate)

        # KH xác nhận
        r = self._call("CASE-P-A1", "pc1", at(21, 9), "Completed", answered_seconds=50,
                       llm=LlmLabel(Outcome.VERIFIED_CONFIRMED, 0.95, model="pg", prompt_version="v1"))
        self.assertEqual(r.decision.action.value, "CLOSE")

        # outbox CASE_FINAL
        events = self.svc.pending_outbox()
        self.assertTrue(any(e["event_type"] == "CASE_FINAL" for e in events))

    def test_rolling_limit_and_phone_history(self):
        """Giới hạn 24h trượt chạy đúng trên Postgres (so sánh chuỗi ISO UTC)."""
        from db import repo
        a = Alert("P-A2", "CARD-Y", "KH-Y", 2_000_000, approved=False,
                  card_blocked=True, created_at=at(21, 9))
        self.svc.ingest_alert(a, "0901111111", at(21, 9))
        calls = self.svc.claim_due_calls(at(21, 9), "pg-w")
        self.svc.start_call(calls[0].schedule_id, at(21, 9))
        for i in range(3):
            self._call("CASE-P-A2", f"pc-{i}", at(21, 9, i * 11), "No Answer")
            new_calls = self.svc.claim_due_calls(at(21, 9, (i + 1) * 11), "pg-w")
            if new_calls:
                self.svc.start_call(new_calls[0].schedule_id, at(21, 9, (i + 1) * 11))
        hist = repo.phone_history(self.conn, ["0901111111"], "OTHER", at(20, 9), VN)
        self.assertGreaterEqual(len(hist["0901111111"]), 3)


if __name__ == "__main__":
    unittest.main()
