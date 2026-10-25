"""Test campaign rules theo từng tình huống. Chạy: python -m unittest discover -s tests -t . -v
"""
import copy
import unittest
from datetime import datetime, timedelta, timezone

import yaml

from callback import (
    OUTCOME_GROUP, Action, Alert, Attempt, Case, CaseStatus, ConfigError, Event, Group, Outcome, Urgency,
    apply_decision, compute_urgency, decide, load_config, merge_alert, outcome_from_pbx, parse_config,
    pre_dial_check,
)
from callback.config import DEFAULT_PATH

VN = timezone(timedelta(hours=7))
CFG = load_config()
PRIMARY = "0900000001"
ALT = "0900000002"
O = Outcome


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=VN)


def make_case(urgency=Urgency.P2, created=None, **kw):
    return Case(case_id="C1", card_ref="CARD-TOKEN-1", customer_id="KH1", phone=PRIMARY,
                created_at=created or at(21, 9), urgency=urgency, **kw)


def att(outcome, start, phone=PRIMARY):
    return Attempt(start, phone, outcome, OUTCOME_GROUP[outcome][1])


def run(case, attempts, outcome, start, phone=PRIMARY, phone_history=None, **kw):
    event = Event(started_at=start, phone=phone, outcome=outcome, **kw)
    return decide(case, attempts, event, start + timedelta(minutes=1), CFG, phone_history)


class RetryCustomerTest(unittest.TestCase):
    def test_no_answer_escalates_10_30_60(self):
        case = make_case(Urgency.P1)
        d1 = run(case, [], O.NO_ANSWER, at(21, 9))
        self.assertEqual(d1.action, Action.SCHEDULE_CALL)
        self.assertEqual(d1.group, Group.RETRY_CUSTOMER)
        self.assertTrue(d1.counts_attempt)
        self.assertEqual(d1.next_call_at, at(21, 9, 11))
        d2 = run(case, [att(O.NO_ANSWER, at(21, 9))], O.NO_ANSWER, at(21, 9, 11))
        self.assertEqual(d2.next_call_at, at(21, 9, 42))
        d3 = run(case, [att(O.NO_ANSWER, at(21, 9)), att(O.NO_ANSWER, at(21, 9, 11))],
                 O.NO_ANSWER, at(21, 9, 42))
        self.assertEqual(d3.next_call_at, at(21, 10, 43))

    def test_busy_10_minutes(self):
        d = run(make_case(), [], O.BUSY, at(21, 9))
        self.assertEqual(d.next_call_at, at(21, 9, 11))

    def test_p3_min_gap_30_minutes(self):
        d = run(make_case(Urgency.P3), [], O.BUSY, at(21, 9))
        self.assertEqual(d.next_call_at, at(21, 9, 30))
        self.assertIn("30 phút", d.reason)

    def test_relative_answered_keeps_primary_number(self):
        case = make_case(phone_sms=ALT)
        d = run(case, [att(O.NO_ANSWER, at(21, 8))], O.RELATIVE_ANSWERED, at(21, 9))
        self.assertEqual(d.next_call_at, at(21, 9, 31))
        self.assertEqual(d.target_phone, PRIMARY)
        self.assertTrue(d.counts_attempt)

    def test_dropped_before_verification_retries(self):
        d = run(make_case(), [], O.DROPPED, at(21, 9))
        self.assertEqual(d.action, Action.SCHEDULE_CALL)
        self.assertEqual(d.next_call_at, at(21, 9, 11))

    def test_dropped_after_verification_closes(self):
        d = run(make_case(), [], O.DROPPED, at(21, 9), verified=True)
        self.assertEqual(d.action, Action.CLOSE)
        self.assertEqual(d.group, Group.DONE)

    def test_rejected_sends_zns_first(self):
        d = run(make_case(), [], O.REJECTED, at(21, 9))
        self.assertEqual(d.next_call_at, at(21, 9, 16))
        self.assertEqual(d.zns_template, "contact")

    def test_short_hangup_sends_zns_immediately(self):
        """KH tắt máy sớm (nghi số lạ) -> ZNS NGAY, không chờ hết lượt."""
        d = run(make_case(), [], O.SHORT_HANGUP, at(21, 9))
        self.assertEqual(d.action, Action.SCHEDULE_CALL)
        self.assertEqual(d.zns_template, "contact")

    def test_short_hangup_too_many_stops_and_switches_to_zns(self):
        """Tắt máy sớm quá n lần -> ngừng gọi, chuyển ZNS + ĐTV."""
        hist = [att(O.SHORT_HANGUP, at(21, 9)), att(O.SHORT_HANGUP, at(21, 9, 11))]
        d = run(make_case(), hist, O.SHORT_HANGUP, at(21, 9, 22))
        self.assertEqual(d.action, Action.EXHAUSTED)
        self.assertEqual(d.rule_id, "R5")
        self.assertEqual(d.zns_template, "final")

    def test_scam_hangup_retried_only_once(self):
        case = make_case()
        d1 = run(case, [], O.SCAM_SUSPECTED_HANGUP, at(21, 9))
        self.assertEqual(d1.next_call_at, at(21, 10, 1))
        self.assertEqual(d1.zns_template, "contact")
        d2 = run(case, [att(O.SCAM_SUSPECTED_HANGUP, at(21, 9))], O.SCAM_SUSPECTED_HANGUP, at(21, 10, 1))
        self.assertEqual(d2.action, Action.EXHAUSTED)
        self.assertEqual(d2.zns_template, "final")


class LimitTest(unittest.TestCase):
    def test_rolling_24h_limit(self):
        hist = [att(O.NO_ANSWER, at(21, 9)), att(O.NO_ANSWER, at(21, 9, 11))]
        d = run(make_case(), hist, O.NO_ANSWER, at(21, 9, 42))
        self.assertEqual(d.next_call_at, at(22, 9))
        self.assertIn("đủ 3 cuộc", d.reason)

    def test_limit_does_not_reset_at_midnight(self):
        case = make_case(created=at(21, 19))
        hist = [att(O.NO_ANSWER, at(21, 19)), att(O.NO_ANSWER, at(21, 19, 30))]
        d = run(case, hist, O.NO_ANSWER, at(21, 20, 30))
        self.assertEqual(d.next_call_at, at(22, 19))  # không phải 07:00 sáng hôm sau

    def test_case_deadline_exhausted(self):
        case = make_case(Urgency.P3, created=at(21, 8))
        hist = [att(O.NO_ANSWER, at(21, 8, 30)), att(O.NO_ANSWER, at(21, 9, 10))]
        d = run(case, hist, O.NO_ANSWER, at(21, 9, 50))
        self.assertEqual(d.action, Action.EXHAUSTED)
        self.assertEqual(d.ticket, "KHONG_LIEN_HE_DUOC")
        self.assertEqual(d.zns_template, "final")
        self.assertIsNone(d.dtv_priority)

    def test_p1_exhausted_goes_to_dtv_with_card_lock_note(self):
        case = make_case(Urgency.P1, created=at(21, 8, 55))
        hist = [att(O.NO_ANSWER, t) for t in (at(21, 9), at(21, 9, 11), at(21, 9, 42), at(21, 10, 43))]
        d = run(case, hist, O.NO_ANSWER, at(21, 11, 44))
        self.assertEqual(d.action, Action.EXHAUSTED)
        self.assertEqual(d.dtv_priority, "HIGH")
        self.assertIn("khóa thẻ", d.reason)

    def test_next_call_exactly_at_deadline_is_exhausted(self):
        case = make_case(Urgency.P1, created=at(21, 9))
        hist = [att(O.NO_ANSWER, t) for t in (at(21, 9), at(21, 9, 11), at(21, 9, 42), at(21, 10, 43))]
        d = run(case, hist, O.NO_ANSWER, at(21, 11, 44))  # lần sớm nhất 22/10 09:00 = đúng hạn
        self.assertEqual(d.action, Action.EXHAUSTED)

    def test_per_phone_cap_across_cases(self):
        others = {PRIMARY: [at(21, 8), at(21, 8, 15), at(21, 8, 30), at(21, 8, 45)]}
        d = run(make_case(), [], O.NO_ANSWER, at(21, 9), phone_history=others)
        self.assertEqual(d.next_call_at, at(22, 8))
        self.assertIn("số ĐT đã nhận đủ", d.reason)

    def test_outside_window_deferred_to_morning(self):
        d = run(make_case(created=at(21, 21)), [], O.NO_ANSWER, at(21, 21, 55))
        self.assertEqual(d.next_call_at, at(22, 7))

    def test_p1_calls_24_7(self):
        d = run(make_case(Urgency.P1, created=at(21, 23)), [], O.NO_ANSWER, at(21, 23, 50))
        self.assertEqual(d.next_call_at, at(22, 0, 1))


class SystemErrorTest(unittest.TestCase):
    def test_not_counted_retry_2_minutes(self):
        d = run(make_case(), [], O.SYSTEM_ERROR, at(21, 9))
        self.assertFalse(d.counts_attempt)
        self.assertEqual(d.group, Group.RETRY_SYSTEM)
        self.assertEqual(d.next_call_at, at(21, 9, 3))

    def test_still_respects_gap_after_a_ringing_call(self):
        d = run(make_case(), [att(O.NO_ANSWER, at(21, 9, 5))], O.SYSTEM_ERROR, at(21, 9, 8))
        self.assertEqual(d.next_call_at, at(21, 9, 15))

    def test_too_many_in_a_row_p2_backoff(self):
        hist = [att(O.SYSTEM_ERROR, t) for t in (at(21, 9), at(21, 9, 3), at(21, 9, 6))]
        d = run(make_case(), hist, O.SYSTEM_ERROR, at(21, 9, 9))
        self.assertEqual(d.rule_id, "Y3")
        self.assertEqual(d.next_call_at, at(21, 9, 40))

    def test_three_in_a_row_still_normal_retry(self):
        hist = [att(O.SYSTEM_ERROR, t) for t in (at(21, 9), at(21, 9, 3))]
        d = run(make_case(), hist, O.SYSTEM_ERROR, at(21, 9, 6))
        self.assertEqual(d.rule_id, "Y1")

    def test_too_many_in_a_row_p1_goes_to_dtv(self):
        hist = [att(O.SYSTEM_ERROR, t) for t in (at(21, 9), at(21, 9, 3), at(21, 9, 6))]
        d = run(make_case(Urgency.P1), hist, O.SYSTEM_ERROR, at(21, 9, 9))
        self.assertEqual(d.action, Action.HAND_TO_DTV)
        self.assertEqual(d.dtv_priority, "HIGH")


class StopAndHumanTest(unittest.TestCase):
    def test_wrong_number_without_alt_stops(self):
        d = run(make_case(), [], O.WRONG_NUMBER, at(21, 9))
        self.assertEqual(d.action, Action.STOP)
        self.assertEqual(d.block_phone, PRIMARY)
        self.assertEqual(d.ticket, "SAI_SO")
        self.assertIsNone(d.zns_template)  # không SMS cho người lạ

    def test_wrong_number_with_alt_tries_alt(self):
        d = run(make_case(phone_sms=ALT), [], O.WRONG_NUMBER, at(21, 9))
        self.assertEqual(d.action, Action.SCHEDULE_CALL)
        self.assertEqual(d.target_phone, ALT)
        self.assertEqual(d.block_phone, PRIMARY)
        self.assertEqual(d.next_call_at, at(21, 9, 10))

    def test_invalid_number_not_counted(self):
        d = run(make_case(), [], O.INVALID_NUMBER, at(21, 9))
        self.assertFalse(d.counts_attempt)
        self.assertEqual(d.ticket, "SO_KHONG_TON_TAI")

    def test_verify_failed_blocks_number_no_zns_no_alt(self):
        d = run(make_case(phone_sms=ALT), [], O.VERIFY_FAILED, at(21, 9))
        self.assertEqual(d.action, Action.STOP)
        self.assertEqual(d.block_phone, PRIMARY)
        self.assertEqual(d.dtv_priority, "HIGH")
        self.assertIsNone(d.zns_template)
        self.assertIsNone(d.next_call_at)

    def test_do_not_call(self):
        d = run(make_case(), [], O.DO_NOT_CALL, at(21, 9))
        self.assertEqual(d.action, Action.STOP)
        self.assertTrue(d.mark_do_not_call)
        self.assertIsNone(d.zns_template)

    def test_other_number_requested_is_never_dialed(self):
        d = run(make_case(), [], O.OTHER_NUMBER_REQUESTED, at(21, 9))
        self.assertEqual(d.action, Action.STOP)
        self.assertIsNone(d.next_call_at)

    def test_verified_confirmed_closes(self):
        d = run(make_case(), [], O.VERIFIED_CONFIRMED, at(21, 9), llm_confidence=0.95)
        self.assertEqual(d.action, Action.CLOSE)

    def test_denied_goes_to_dtv_high_even_p3(self):
        d = run(make_case(Urgency.P3), [], O.VERIFIED_DENIED, at(21, 9), llm_confidence=0.9)
        self.assertEqual(d.action, Action.HAND_TO_DTV)
        self.assertEqual(d.dtv_priority, "HIGH")

    def test_low_llm_confidence_becomes_unclear(self):
        d = run(make_case(), [], O.VERIFIED_CONFIRMED, at(21, 9), llm_confidence=0.5)
        self.assertEqual(d.action, Action.HAND_TO_DTV)
        self.assertEqual(d.outcome, O.UNCLEAR)
        self.assertIn("0.50", d.reason)

    def test_angry_goes_to_dtv(self):
        d = run(make_case(), [], O.ANGRY, at(21, 9), llm_confidence=0.9)
        self.assertEqual(d.action, Action.HAND_TO_DTV)

    def test_transferred_in_call(self):
        d = run(make_case(), [], O.TRANSFERRED, at(21, 9))
        self.assertEqual(d.rule_id, "H0")

    def test_customer_unsure_goes_to_dtv(self):
        d = run(make_case(), [], O.CUSTOMER_UNSURE, at(21, 9), llm_confidence=0.85)
        self.assertEqual(d.action, Action.HAND_TO_DTV)
        self.assertEqual(d.rule_id, "H5")
        self.assertTrue(d.counts_attempt)

    def test_action_failed_goes_to_dtv_high_always(self):
        # P3 bình thường ĐTV NORMAL, nhưng ACTION_FAILED thì HIGH
        d = run(make_case(Urgency.P3), [], O.ACTION_FAILED, at(21, 9))
        self.assertEqual(d.action, Action.HAND_TO_DTV)
        self.assertEqual(d.rule_id, "H6")
        self.assertEqual(d.dtv_priority, "HIGH")


class AppointmentTest(unittest.TestCase):
    def test_callback_at_customer_time_not_counted(self):
        d = run(make_case(), [], O.CALLBACK_REQUESTED, at(21, 10), callback_at=at(21, 15))
        self.assertEqual(d.next_call_at, at(21, 15))
        self.assertFalse(d.counts_attempt)
        self.assertTrue(d.appointment)

    def test_p1_callback_capped_at_30_minutes(self):
        d = run(make_case(Urgency.P1), [], O.CALLBACK_REQUESTED, at(21, 10), callback_at=at(21, 13))
        self.assertEqual(d.next_call_at, at(21, 10, 31))

    def test_callback_too_soon_respects_min_gap(self):
        d = run(make_case(), [], O.CALLBACK_REQUESTED, at(21, 10), callback_at=at(21, 10, 5))
        self.assertEqual(d.next_call_at, at(21, 10, 10))

    def test_callback_outside_window_moves_to_morning(self):
        d = run(make_case(created=at(21, 21)), [], O.CALLBACK_REQUESTED, at(21, 21, 50),
                callback_at=at(21, 22, 30))
        self.assertEqual(d.next_call_at, at(22, 7))

    def test_callback_without_time_uses_default(self):
        d = run(make_case(), [], O.CALLBACK_REQUESTED, at(21, 10))
        self.assertEqual(d.next_call_at, at(21, 11, 1))

    def test_too_many_appointments_goes_to_dtv(self):
        d = run(make_case(appointments=2), [], O.CALLBACK_REQUESTED, at(21, 10), callback_at=at(21, 15))
        self.assertEqual(d.action, Action.HAND_TO_DTV)


class SmsAndAltPhoneTest(unittest.TestCase):
    def test_p1_zns_after_first_failure(self):
        d = run(make_case(Urgency.P1), [], O.NO_ANSWER, at(21, 9))
        self.assertEqual(d.zns_template, "contact")

    def test_p2_zns_after_second_failure(self):
        case = make_case()
        self.assertIsNone(run(case, [], O.NO_ANSWER, at(21, 9)).zns_template)
        d = run(case, [att(O.NO_ANSWER, at(21, 9))], O.NO_ANSWER, at(21, 9, 11))
        self.assertEqual(d.zns_template, "contact")
        self.assertEqual(d.zns_to, PRIMARY)

    def test_zns_goes_to_phone_sms_when_present(self):
        d = run(make_case(Urgency.P1, phone_sms=ALT), [], O.NO_ANSWER, at(21, 9))
        self.assertEqual(d.zns_to, ALT)

    def test_zns_cap_reserves_last_one_for_final(self):
        case = make_case(zns_sent=2)
        d = run(case, [att(O.NO_ANSWER, at(21, 9))], O.NO_ANSWER, at(21, 9, 11))
        self.assertIsNone(d.zns_template)
        ex = run(make_case(Urgency.P3, created=at(21, 8), zns_sent=2),
                 [att(O.NO_ANSWER, at(21, 8, 30)), att(O.NO_ANSWER, at(21, 9, 10))], O.NO_ANSWER, at(21, 9, 50))
        self.assertEqual(ex.zns_template, "final")
        ex3 = run(make_case(Urgency.P3, created=at(21, 8), zns_sent=3),
                  [att(O.NO_ANSWER, at(21, 8, 30)), att(O.NO_ANSWER, at(21, 9, 10))], O.NO_ANSWER, at(21, 9, 50))
        self.assertIsNone(ex3.zns_template)

    def test_switch_to_alt_after_two_failures_then_back(self):
        case = make_case(Urgency.P1, phone_sms=ALT)
        d1 = run(case, [att(O.NO_ANSWER, at(21, 9))], O.NO_ANSWER, at(21, 9, 11))
        self.assertEqual(d1.target_phone, ALT)
        self.assertEqual(d1.next_call_at, at(21, 9, 42))
        hist = [att(O.NO_ANSWER, at(21, 9)), att(O.NO_ANSWER, at(21, 9, 11))]
        d2 = run(case, hist, O.NO_ANSWER, at(21, 9, 42), phone=ALT)
        self.assertEqual(d2.target_phone, PRIMARY)


class GuardAndPreDialTest(unittest.TestCase):
    def test_closed_case_never_called(self):
        case = make_case(status=CaseStatus.CLOSED)
        self.assertEqual(run(case, [], O.NO_ANSWER, at(21, 9)).rule_id, "G1")
        self.assertEqual(pre_dial_check(case, [], PRIMARY, at(21, 9), CFG).action, Action.STOP)

    def test_do_not_call_case_goes_to_dtv(self):
        d = pre_dial_check(make_case(do_not_call=True), [], PRIMARY, at(21, 9), CFG)
        self.assertEqual(d.rule_id, "G2")
        self.assertEqual(d.action, Action.HAND_TO_DTV)

    def test_pre_dial_ok(self):
        d = pre_dial_check(make_case(), [], PRIMARY, at(21, 9), CFG)
        self.assertEqual(d.action, Action.DIAL_NOW)

    def test_pre_dial_outside_window(self):
        d = pre_dial_check(make_case(created=at(21, 22, 30)), [], PRIMARY, at(21, 23), CFG)
        self.assertEqual(d.action, Action.SCHEDULE_CALL)
        self.assertEqual(d.next_call_at, at(22, 7))

    def test_pre_dial_reroutes_blocked_number(self):
        case = make_case(phone_sms=ALT, blocked_phones=frozenset({PRIMARY}))
        d = pre_dial_check(case, [], PRIMARY, at(21, 9), CFG)
        self.assertEqual(d.action, Action.DIAL_NOW)
        self.assertEqual(d.target_phone, ALT)

    def test_pre_dial_blocked_no_alt(self):
        case = make_case(blocked_phones=frozenset({PRIMARY}))
        self.assertEqual(pre_dial_check(case, [], PRIMARY, at(21, 9), CFG).action, Action.STOP)

    def test_appointment_not_blocked_by_call_limit(self):
        hist = [att(O.NO_ANSWER, t) for t in (at(21, 9), at(21, 9, 11), at(21, 9, 42))]
        d = pre_dial_check(make_case(), hist, PRIMARY, at(21, 15), CFG, appointment=True)
        self.assertEqual(d.action, Action.DIAL_NOW)
        d2 = pre_dial_check(make_case(), hist, PRIMARY, at(21, 15), CFG)
        self.assertEqual(d2.next_call_at, at(22, 9))


class MergeAlertTest(unittest.TestCase):
    def alert(self, **kw):
        base = dict(alert_id="A2", card_ref="CARD-TOKEN-1", customer_id="KH1", amount_vnd=1_000_000,
                    approved=False, card_blocked=True, created_at=at(21, 9, 5))
        base.update(kw)
        return Alert(**base)

    def test_merge_upgrades_and_calls_asap_without_reset(self):
        case = make_case(card_open=False)
        r = merge_alert(case, self.alert(), [att(O.NO_ANSWER, at(21, 9))], at(21, 9, 5), CFG)
        self.assertEqual(r.case.alert_count, 2)
        self.assertEqual(r.case.urgency, Urgency.P1)
        self.assertEqual(r.decision.action, Action.SCHEDULE_CALL)
        self.assertEqual(r.decision.next_call_at, at(21, 9, 10))

    def test_other_customer_not_merged(self):
        r = merge_alert(make_case(), self.alert(card_ref="X", customer_id="KH2"), [], at(21, 9, 5), CFG)
        self.assertIsNone(r)

    def test_closed_case_not_merged(self):
        r = merge_alert(make_case(status=CaseStatus.DONE), self.alert(), [], at(21, 9, 5), CFG)
        self.assertIsNone(r)


class UrgencyTest(unittest.TestCase):
    def a(self, **kw):
        base = dict(alert_id="A1", card_ref="C", customer_id="K", amount_vnd=1_000_000,
                    approved=False, card_blocked=False, created_at=at(21, 9))
        base.update(kw)
        return Alert(**base)

    def test_table(self):
        cases = [
            (self.a(approved=True), 1, Urgency.P1),
            (self.a(approved=True, card_blocked=True), 1, Urgency.P2),
            (self.a(), 1, Urgency.P3),
            (self.a(card_blocked=True, amount_vnd=60_000_000), 1, Urgency.P1),
            (self.a(), 2, Urgency.P1),
        ]
        for alert, n, want in cases:
            with self.subTest(alert=alert, n=n):
                self.assertEqual(compute_urgency(alert, n, CFG)[0], want)


class MiscTest(unittest.TestCase):
    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            decide(make_case(), [], Event(datetime(2026, 10, 21, 9), PRIMARY, O.NO_ANSWER),
                   datetime(2026, 10, 21, 9, 1), CFG)

    def test_decide_does_not_mutate_inputs(self):
        hist = [att(O.NO_ANSWER, at(21, 9))]
        case = make_case()
        run(case, hist, O.NO_ANSWER, at(21, 9, 11))
        self.assertEqual(len(hist), 1)
        self.assertEqual(case, make_case())

    def test_apply_decision(self):
        case = make_case(phone_sms=ALT)
        d = run(case, [], O.WRONG_NUMBER, at(21, 9))
        new = apply_decision(case, d)
        self.assertIn(PRIMARY, new.blocked_phones)
        self.assertEqual(new.status, CaseStatus.OPEN)
        d2 = run(case, [], O.REJECTED, at(21, 9))
        self.assertEqual(apply_decision(case, d2).zns_sent, 1)
        self.assertEqual(apply_decision(case, run(case, [], O.VERIFIED_CONFIRMED, at(21, 9))).status,
                         CaseStatus.DONE)

    def test_pbx_mapping(self):
        self.assertEqual(outcome_from_pbx("No Answer"), O.NO_ANSWER)
        self.assertEqual(outcome_from_pbx("Failed", 603), O.REJECTED)
        self.assertEqual(outcome_from_pbx("Failed", 404), O.INVALID_NUMBER)
        self.assertEqual(outcome_from_pbx("Failed", 480), O.UNREACHABLE)
        self.assertEqual(outcome_from_pbx("Failed", 503), O.SYSTEM_ERROR)
        self.assertEqual(outcome_from_pbx("Completed", answered_seconds=3), O.SHORT_HANGUP)
        self.assertEqual(outcome_from_pbx("Completed", machine_detected=True), O.VOICEMAIL)
        self.assertIsNone(outcome_from_pbx("Completed", answered_seconds=40))
        self.assertEqual(outcome_from_pbx("Incomplete", answered_seconds=40), O.DROPPED)
        with self.assertRaises(ValueError):
            outcome_from_pbx("Ringing")


class ConfigTest(unittest.TestCase):
    def raw(self):
        return yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))

    def test_default_config_valid(self):
        parse_config(self.raw())

    def test_gap_below_docs_rejected(self):
        raw = self.raw()
        raw["levels"]["P1"]["min_gap_minutes"] = 5
        with self.assertRaisesRegex(ConfigError, "P1"):
            parse_config(raw)

    def test_zns_with_transaction_details_rejected(self):
        raw = self.raw()
        raw["zns"]["templates"]["contact"] = "GD {amount} VND"
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_missing_outcome_rejected(self):
        raw = copy.deepcopy(self.raw())
        del raw["outcomes"]["busy"]
        with self.assertRaisesRegex(ConfigError, "busy"):
            parse_config(raw)

    def test_unknown_outcome_rejected(self):
        raw = self.raw()
        raw["outcomes"]["khong_ton_tai"] = {"delays": [10]}
        with self.assertRaises(ConfigError):
            parse_config(raw)


if __name__ == "__main__":
    unittest.main()
