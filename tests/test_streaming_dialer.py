"""Test orchestration của StreamingDialer: không cần gevent, không cần DB.

ESL / outcome store / context sink đều là fake. Kiểm tra: originate hỏng, agent báo kết quả,
lưới an toàn (KH cúp trước, timeout), và map nhãn lạ -> unclear.
"""
import unittest
from datetime import datetime, timedelta, timezone

from callback.models import Outcome
from dialer.streaming_dialer import (
    OriginateResult, StreamingDialer, StreamingDialerConfig, map_label,
)

VN = timezone(timedelta(hours=7))


class FakeCall:
    def __init__(self, case_id="CASE-1", phone="0901000001"):
        self.case_id = case_id
        self.phone = phone


class FakeDecision:
    def __init__(self, target_phone="0901000001"):
        self.target_phone = target_phone


class FakeEsl:
    def __init__(self, result, hung_after=None, billsec=None, sip=None):
        self.result = result
        self.hung_after = hung_after
        self.billsec = billsec
        self.sip = sip
        self.polls = 0
        self.killed = False
        self.originated = None

    def originate(self, call_id, phone, cfg):
        self.originated = (call_id, phone)
        return self.result

    def is_hungup(self, uuid):
        self.polls += 1
        if self.hung_after is not None and self.polls >= self.hung_after:
            return True, self.billsec, self.sip
        return False, None, None

    def kill(self, uuid):
        self.killed = True


class FakeStore:
    """Trả None cho tới sau `after_reads` lần đọc thì trả `value`."""
    def __init__(self, value=None, after_reads=0):
        self.value = value
        self.after = after_reads
        self.reads = 0

    def read(self, call_id):
        self.reads += 1
        if self.value is not None and self.reads > self.after:
            return self.value
        return None


class FakeSink:
    def __init__(self):
        self.registered = {}

    def register(self, call_id, context):
        self.registered[call_id] = context


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def ctx_provider(case_id):
    return {"case_id": case_id, "customer_id": "KH1", "campaign": "card_fraud", "verify": {}}


def build(esl, store, sink=None, cfg=None):
    sink = sink or FakeSink()
    clk = Clock()
    cfg = cfg or StreamingDialerConfig(call_timeout=5, poll_interval_s=1, hangup_grace_s=2)
    dialer = StreamingDialer(cfg, esl, store, sink, ctx_provider, clock=clk.now, sleep=clk.sleep)
    return dialer, sink, clk


ANSWERED = OriginateResult(True, uuid="uuid-1")


class StreamingDialerTest(unittest.TestCase):
    def test_originate_busy_skips_pipeline(self):
        esl = FakeEsl(OriginateResult(False, status="Busy"))
        dialer, sink, _ = build(esl, FakeStore())
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.pbx_status, "Busy")
        self.assertIsNone(r.llm)
        self.assertFalse(esl.killed)
        self.assertIn(r.call_id, sink.registered)              # vẫn đăng ký ngữ cảnh trước khi originate

    def test_context_posted_before_originate(self):
        esl = FakeEsl(ANSWERED)
        store = FakeStore({"status": "completed", "label": "verified_confirmed", "confidence": 0.9})
        dialer, sink, _ = build(esl, store)
        r = dialer(FakeCall("CASE-9"), FakeDecision("0909"))
        ctx = sink.registered[r.call_id]
        self.assertEqual(ctx["call_id"], r.call_id)
        self.assertEqual(ctx["case_id"], "CASE-9")
        self.assertEqual(esl.originated, (r.call_id, "0909"))

    def test_agent_confirmed(self):
        esl = FakeEsl(ANSWERED)
        store = FakeStore({"status": "completed", "label": "verified_confirmed",
                           "confidence": 0.93, "verified": True})
        dialer, _, _ = build(esl, store)
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.pbx_status, "Completed")
        self.assertEqual(r.llm.label, Outcome.VERIFIED_CONFIRMED)
        self.assertAlmostEqual(r.llm.confidence, 0.93)
        self.assertTrue(r.verified)
        self.assertTrue(esl.killed)                            # Controller cúp máy sau khi có kết quả

    def test_agent_callback_with_time(self):
        esl = FakeEsl(ANSWERED)
        store = FakeStore({"status": "completed", "label": "callback",
                           "confidence": 0.8, "callback_at": "2026-11-20T15:00:00+07:00"})
        dialer, _, _ = build(esl, store)
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.llm.label, Outcome.CALLBACK_REQUESTED)
        self.assertEqual(r.llm.callback_at, datetime(2026, 11, 20, 15, 0, tzinfo=VN))

    def test_outcome_arrives_after_some_polls(self):
        esl = FakeEsl(ANSWERED)                                # không hung -> chỉ chờ outcome
        store = FakeStore({"status": "completed", "label": "need_human"}, after_reads=3)
        dialer, _, _ = build(esl, store)
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.llm.label, Outcome.NEED_HUMAN)
        self.assertGreater(store.reads, 3)

    def test_transferred(self):
        esl = FakeEsl(ANSWERED)
        store = FakeStore({"status": "transferred", "label": "transferred"})
        dialer, _, _ = build(esl, store)
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.pbx_status, "Transferred")
        self.assertEqual(r.llm.label, Outcome.TRANSFERRED)

    def test_unknown_label_is_unclear(self):
        esl = FakeEsl(ANSWERED)
        store = FakeStore({"status": "completed", "label": "chuyen_gi_do_la"})
        dialer, _, _ = build(esl, store)
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.llm.label, Outcome.UNCLEAR)

    def test_hangup_before_outcome_goes_unclear(self):
        # KH cúp ngay (hung_after=1), agent không bao giờ báo -> 'Completed' không nhãn
        esl = FakeEsl(ANSWERED, hung_after=1, billsec=42)
        dialer, _, _ = build(esl, FakeStore())
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.pbx_status, "Completed")
        self.assertIsNone(r.llm)                               # không nhãn -> rule engine map thành unclear -> ĐTV
        self.assertEqual(r.answered_seconds, 42)

    def test_hangup_but_outcome_arrives_in_grace(self):
        # KH cúp, nhưng agent POST kết quả muộn trong cửa sổ grace -> vẫn dùng kết quả agent
        esl = FakeEsl(ANSWERED, hung_after=1, billsec=50)
        store = FakeStore({"status": "completed", "label": "verified_denied"}, after_reads=1)
        dialer, _, _ = build(esl, store)
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.llm.label, Outcome.VERIFIED_DENIED)

    def test_timeout_is_system_error(self):
        esl = FakeEsl(ANSWERED)                                # không hung, không outcome
        dialer, _, _ = build(esl, FakeStore())
        r = dialer(FakeCall(), FakeDecision())
        self.assertEqual(r.pbx_status, "Failed")               # không mã SIP -> system_error
        self.assertIsNone(r.sip_code)
        self.assertTrue(esl.killed)                            # uuid_kill cắt kênh treo


class MapLabelTest(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(map_label("verified_confirmed"), Outcome.VERIFIED_CONFIRMED)
        self.assertEqual(map_label("CALLBACK"), Outcome.CALLBACK_REQUESTED)
        self.assertEqual(map_label(None), Outcome.UNCLEAR)
        self.assertEqual(map_label("khong_biet"), Outcome.UNCLEAR)


if __name__ == "__main__":
    unittest.main()
