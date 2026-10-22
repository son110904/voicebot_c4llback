"""Test tầng LLM: che PII, parse JSON, fallback khi lỗi."""
import json
import unittest

from callback import Outcome
from datetime import timedelta, timezone
from llm import LlmLabeler, mask_pii
from llm.labeler import LabelerConfig
VN = timezone(timedelta(hours=7))


class FakeClient:
    """LLM giả: trả đúng cái mình đã nạp sẵn. Nếu `raises` được set thì ném exception."""
    cfg = LabelerConfig(model="fake")

    def __init__(self, reply: str = "", raises: Exception | None = None):
        self.reply = reply
        self.raises = raises
        self.last_user: str | None = None

    def complete(self, system, user):
        self.last_user = user
        if self.raises:
            raise self.raises
        return self.reply


def ok(outcome="verified_confirmed", confidence=0.9, callback_at=None, evidence="KH xác nhận"):
    return json.dumps({"outcome": outcome, "confidence": confidence,
                       "callback_at": callback_at, "evidence": evidence})


class PiiTest(unittest.TestCase):
    def test_masks_phone_card_id_and_amount(self):
        text = "Khách 0912345678 giao dịch 2.000.000 đồng, thẻ 4111 1111 1111 1111, CCCD 012345678901, OTP 123456"
        out = mask_pii(text)
        self.assertNotIn("0912345678", out)
        self.assertNotIn("4111", out)
        self.assertNotIn("012345678901", out)
        self.assertNotIn("123456", out)
        self.assertNotIn("2.000.000", out)
        for token in ("<PHONE>", "<CARD>", "<ID>", "<CODE>", "<AMOUNT>"):
            self.assertIn(token, out)

    def test_keeps_short_numbers(self):
        self.assertIn("3 lần", mask_pii("gọi 3 lần"))


class ParseTest(unittest.TestCase):
    def test_valid_label(self):
        c = FakeClient(ok(confidence=0.88))
        r = LlmLabeler(c).label("KH xác nhận")
        self.assertEqual(r.label, Outcome.VERIFIED_CONFIRMED)
        self.assertAlmostEqual(r.confidence, 0.88)
        self.assertEqual(r.model, "fake")
        self.assertEqual(r.prompt_version, "v1")

    def test_strips_markdown_fence(self):
        r = LlmLabeler(FakeClient("```json\n" + ok() + "\n```")).label("x")
        self.assertEqual(r.label, Outcome.VERIFIED_CONFIRMED)

    def test_pii_masked_before_call(self):
        c = FakeClient(ok())
        LlmLabeler(c).label("thẻ 4111 1111 1111 1111 và SĐT 0912345678")
        self.assertNotIn("0912345678", c.last_user)
        self.assertNotIn("4111 1111", c.last_user)

    def test_transcript_truncated(self):
        c = FakeClient(ok())
        LlmLabeler(c, LabelerConfig(max_transcript_chars=50)).label("x" * 200)
        self.assertEqual(len(c.last_user), 50)

    def test_parses_callback_at(self):
        r = LlmLabeler(FakeClient(ok("callback_requested", callback_at="2026-10-22T15:00:00+07:00")),
                        tz=VN).label("x")
        self.assertEqual(r.callback_at.hour, 15)
        self.assertIsNotNone(r.callback_at.utcoffset())


class FallbackTest(unittest.TestCase):
    def _unclear(self, reply: str):
        r = LlmLabeler(FakeClient(reply)).label("x")
        self.assertEqual(r.label, Outcome.UNCLEAR)
        self.assertEqual(r.confidence, 0.0)
        return r

    def test_bad_json(self):
        self._unclear("not json at all")

    def test_unknown_label(self):
        self._unclear(ok(outcome="chua_biet"))

    def test_bad_confidence(self):
        self._unclear(ok(confidence="high"))

    def test_bad_callback_at(self):
        self._unclear(ok("callback_requested", callback_at="tomorrow 3pm"))

    def test_client_error_becomes_unclear(self):
        r = LlmLabeler(FakeClient(raises=TimeoutError("quá hạn"))).label("x")
        self.assertEqual(r.label, Outcome.UNCLEAR)
        self.assertIn("TimeoutError", r.raw_output)

    def test_confidence_clamped(self):
        self.assertEqual(LlmLabeler(FakeClient(ok(confidence=1.5))).label("x").confidence, 1.0)
        self.assertEqual(LlmLabeler(FakeClient(ok(confidence=-0.3))).label("x").confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
