"""Test DB + luồng xử lý (SQLite trong RAM). Dữ liệu giả.

Chạy: python -m unittest discover -s tests -t . -v
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from callback import Action, Alert, CaseStatus, Outcome, load_config
from db import CallbackService, ConcurrentUpdateError, LlmLabel, connect
from db import repo

VN = timezone(timedelta(hours=7))
CFG = load_config()
PRIMARY = "0900000001"


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=VN)


def alert(aid="A1", **kw):
    base = dict(alert_id=aid, card_ref="CARD-1", customer_id="KH1", amount_vnd=2_000_000,
                approved=False, card_blocked=True, created_at=at(21, 9))  # thẻ đã khóa -> P2
    base.update(kw)
    return Alert(**base)


def p1_alert(aid="A1", **kw):
    return alert(aid, approved=True, card_blocked=False, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = connect()
        self.svc = CallbackService(self.conn, CFG, config_version="test-v1")

    def tearDown(self):
        self.conn.close()

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def case(self, case_id="CASE-A1"):
        return repo.get_case(self.conn, case_id, VN)[0]

    def dial(self, now):
        """Scheduler nhận đúng 1 lịch tới hạn và kiểm tra trước khi quay số."""
        calls = self.svc.claim_due_calls(now, "w1")
        self.assertEqual(len(calls), 1)
        return calls[0], self.svc.start_call(calls[0].schedule_id, now)

    def call(self, call_id, start, status, case_id="CASE-A1", **kw):
        return self.svc.record_call_result(case_id, call_id, PRIMARY, start, start + timedelta(minutes=1),
                                           status, **kw)


class AlertTest(Base):
    def test_new_alert_creates_case_and_due_call(self):
        r = self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.assertFalse(r.merged)
        self.assertEqual(r.decision.action, Action.DIAL_NOW)
        self.assertIn("Tạo case mới mức P2", r.decision.reason)
        self.assertEqual(self.case().urgency.value, "P2")
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'PENDING'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM decisions WHERE config_version = 'test-v1'"), 1)

    def test_duplicate_alert_ignored(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        r = self.svc.ingest_alert(alert(), PRIMARY, at(21, 9, 1))
        self.assertTrue(r.duplicate)
        for table in ("cases", "alerts", "decisions", "schedule"):
            self.assertEqual(self.count(f"SELECT COUNT(*) FROM {table}"), 1, table)

    def test_merge_upgrades_urgency_without_reset(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.call("c1", at(21, 9), "No Answer")
        r = self.svc.ingest_alert(alert("A2", created_at=at(21, 9, 5)), PRIMARY, at(21, 9, 5))
        self.assertTrue(r.merged)
        case = self.case()
        self.assertEqual((case.alert_count, case.urgency.value), (2, "P1"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM call_attempts"), 1)
        rows = self.conn.execute("SELECT due_at FROM schedule WHERE status = 'PENDING'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(repo.from_ts(rows[0]["due_at"], VN), at(21, 9, 10))  # gọi sớm hơn 9:11

    def test_merge_while_dialing_waits_for_result(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        r = self.svc.ingest_alert(alert("A2"), PRIMARY, at(21, 9, 1))
        self.assertTrue(r.merged)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'PENDING'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'DIALING'"), 1)

    def test_do_not_call_customer_new_alert_goes_to_dtv(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        d = self.call("c1", at(21, 9), "Completed", answered_seconds=40,
                      llm=LlmLabel(Outcome.DO_NOT_CALL, 0.9)).decision
        self.assertEqual(d.action, Action.STOP)
        r = self.svc.ingest_alert(alert("A2", card_ref="CARD-2"), PRIMARY, at(21, 10))
        self.assertFalse(r.merged)
        self.assertEqual(r.decision.rule_id, "G2")
        self.assertEqual(self.case(r.case_id).status, CaseStatus.HUMAN)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE case_id = ? AND status = 'PENDING'",
                                    r.case_id), 0)


class SchedulerTest(Base):
    def test_claim_only_once_and_only_when_due(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.assertEqual(self.svc.claim_due_calls(at(21, 8, 59), "w1"), [])
        self.assertEqual(len(self.svc.claim_due_calls(at(21, 9), "w1")), 1)
        self.assertEqual(self.svc.claim_due_calls(at(21, 9), "w2"), [])       # không gọi 2 lần

    def test_stale_claim_is_reclaimed(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.svc.claim_due_calls(at(21, 9), "w1")                              # w1 nhận rồi chết
        self.assertEqual(len(self.svc.claim_due_calls(at(21, 9, 6), "w2")), 1)

    def test_start_call_twice_rejected(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        call, d = self.dial(at(21, 9))
        self.assertEqual(d.action, Action.DIAL_NOW)
        with self.assertRaises(ValueError):
            self.svc.start_call(call.schedule_id, at(21, 9))

    def test_closed_externally_is_not_called(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.svc.close_case("CASE-A1", at(21, 9), "ĐTV đã xử lý")
        self.assertEqual(self.case().status, CaseStatus.CLOSED)
        self.assertEqual(self.svc.claim_due_calls(at(21, 9), "w1"), [])

    def test_result_after_external_close_keeps_case_closed(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.svc.close_case("CASE-A1", at(21, 9, 0), "TWO đóng cảnh báo")
        d = self.call("c1", at(21, 9), "No Answer").decision
        self.assertEqual(d.rule_id, "G1")
        self.assertEqual(self.case().status, CaseStatus.CLOSED)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'PENDING'"), 0)

    def test_lost_call_recovered_as_system_error(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.assertEqual(self.svc.recover_lost_calls(at(21, 9, 10)), [])
        [r] = self.svc.recover_lost_calls(at(21, 9, 16))
        self.assertEqual(r.decision.outcome, Outcome.SYSTEM_ERROR)
        self.assertEqual(r.decision.next_call_at, at(21, 9, 18))
        self.assertEqual(self.count("SELECT COUNT(*) FROM call_attempts WHERE counted = 0"), 1)


class CallResultTest(Base):
    def test_no_answer_schedules_retry(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        r = self.call("c1", at(21, 9), "No Answer")
        self.assertEqual(r.decision.next_call_at, at(21, 9, 11))
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'DONE'"), 1)
        self.assertEqual(self.svc.claim_due_calls(at(21, 9, 10), "w1"), [])
        self.assertEqual(len(self.svc.claim_due_calls(at(21, 9, 11), "w1")), 1)

    def test_duplicate_call_result_is_idempotent(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        first = self.call("c1", at(21, 9), "No Answer")
        again = self.call("c1", at(21, 9), "No Answer")
        self.assertTrue(again.duplicate)
        self.assertEqual(again.decision, first.decision)
        self.assertEqual(self.count("SELECT COUNT(*) FROM call_attempts"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'PENDING'"), 1)

    def test_llm_confirmed_closes_case(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        d = self.call("c1", at(21, 9), "Completed", answered_seconds=60,
                      llm=LlmLabel(Outcome.VERIFIED_CONFIRMED, 0.95, model="gpt-test", prompt_version="v1")).decision
        self.assertEqual(d.action, Action.CLOSE)
        self.assertEqual(self.case().status, CaseStatus.DONE)
        self.assertEqual(self.count("SELECT COUNT(*) FROM llm_labels WHERE prompt_version = 'v1'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status IN ('PENDING','CLAIMED')"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM outbox WHERE event_type = 'CASE_FINAL'"), 1)

    def test_conversation_without_label_goes_to_dtv(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        d = self.call("c1", at(21, 9), "Completed", answered_seconds=60).decision
        self.assertEqual(d.outcome, Outcome.UNCLEAR)
        self.assertEqual(self.case().status, CaseStatus.HUMAN)
        self.assertEqual(self.count("SELECT COUNT(*) FROM outbox WHERE event_type = 'DTV_HANDOFF'"), 1)

    def test_p1_sms_logged_without_transaction_details(self):
        self.svc.ingest_alert(p1_alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.call("c1", at(21, 9), "No Answer")
        self.assertEqual(self.count("SELECT COUNT(*) FROM sms_log"), 1)
        self.assertEqual(self.case().sms_sent, 1)
        [ev] = [e for e in self.svc.pending_outbox() if e["event_type"] == "SMS"]
        body = json.loads(ev["payload"])["body"]
        self.assertIn("1900558868", body)
        self.assertNotIn("2000000", body)

    def test_bad_status_rolls_back_everything(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        with self.assertRaises(ValueError):
            self.call("c1", at(21, 9), "Ringing")
        self.assertEqual(self.count("SELECT COUNT(*) FROM call_attempts"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM schedule WHERE status = 'DIALING'"), 1)

    def test_timeline_records_every_step(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.call("c1", at(21, 9), "No Answer")
        self.assertEqual([t["kind"] for t in self.svc.timeline("CASE-A1")], ["alert", "pre_dial", "after_call"])


class RepoTest(Base):
    def test_phone_history_counts_other_cases_only(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.call("c1", at(21, 9), "No Answer")
        self.svc.ingest_alert(alert("B1", card_ref="CARD-9", customer_id="KH9"), PRIMARY, at(21, 9, 1))
        hist = repo.phone_history(self.conn, [PRIMARY], "CASE-B1", at(20, 9), VN)
        self.assertEqual(hist[PRIMARY], [at(21, 9)])
        self.assertEqual(repo.phone_history(self.conn, [PRIMARY], "CASE-A1", at(20, 9), VN)[PRIMARY], [])

    def test_concurrent_update_detected(self):
        self.svc.ingest_alert(alert(), PRIMARY, at(21, 9))
        case, version = repo.get_case(self.conn, "CASE-A1", VN)
        repo.update_case(self.conn, case, version, at(21, 9))
        with self.assertRaises(ConcurrentUpdateError):
            repo.update_case(self.conn, case, version, at(21, 9))

    def test_outbox_mark_sent(self):
        self.svc.ingest_alert(p1_alert(), PRIMARY, at(21, 9))
        self.dial(at(21, 9))
        self.call("c1", at(21, 9), "No Answer")
        for ev in self.svc.pending_outbox():
            self.svc.mark_outbox(ev["event_id"], at(21, 9, 2))
        self.assertEqual(self.svc.pending_outbox(), [])


if __name__ == "__main__":
    unittest.main()
