"""Test campaign_type + hợp đồng dữ liệu trigger + job pull (không cần DB)."""
import copy
import unittest
from datetime import date, datetime, timedelta, timezone

import yaml

from callback import (
    Alert, CampaignType, Case, CaseStatus, ConfigError, Urgency, compute_urgency, load_config, merge_alert,
    parse_config,
)
from callback.config import DEFAULT_PATH
from triggers import alert_from_payload
from triggers.pull_job import rows_to_alerts

VN = timezone(timedelta(hours=7))
CFG = load_config()
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=VN)


def reminder(campaign=CampaignType.LOAN_DUE, due=date(2026, 11, 18), **kw):
    base = dict(alert_id="L1", card_ref="LN-1", customer_id="KH1", amount_vnd=1_000_000, approved=False,
                card_blocked=False, created_at=NOW, campaign=campaign, due_date=due)
    base.update(kw)
    return Alert(**base)


class UrgencyByCampaignTest(unittest.TestCase):
    def test_loan_due_table(self):
        cases = [
            (date(2026, 11, 20), Urgency.P3),   # còn 4 ngày
            (date(2026, 11, 17), Urgency.P2),   # còn 1 ngày (urgent_within_days=1)
            (date(2026, 11, 16), Urgency.P2),   # hôm nay
            (date(2026, 11, 15), Urgency.P2),   # quá hạn 1 ngày
            (date(2026, 11, 13), Urgency.P1),   # quá hạn 3 ngày (overdue_days_p1=3)
        ]
        for due, want in cases:
            with self.subTest(due=due):
                self.assertEqual(compute_urgency(reminder(due=due), 1, CFG)[0], want)

    def test_statement_overdue_threshold_differs(self):
        a = reminder(CampaignType.STATEMENT_DUE, due=date(2026, 11, 13))  # quá hạn 3, ngưỡng P1 là 5
        self.assertEqual(compute_urgency(a, 1, CFG)[0], Urgency.P2)

    def test_reminder_without_due_date_rejected(self):
        with self.assertRaises(ValueError):
            compute_urgency(reminder(due=None), 1, CFG)

    def test_card_fraud_unchanged(self):
        a = Alert("A", "C", "K", 1_000_000, approved=True, card_blocked=False, created_at=NOW)
        self.assertEqual(compute_urgency(a, 1, CFG)[0], Urgency.P1)


class CampaignConfigTest(unittest.TestCase):
    def raw(self):
        return yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))

    def test_card_fraud_uses_top_level_levels(self):
        self.assertIs(CFG.campaigns[CampaignType.CARD_FRAUD].levels, CFG.levels)
        self.assertEqual(CFG.level(CampaignType.CARD_FRAUD, Urgency.P1), CFG.levels[Urgency.P1])

    def test_reminder_levels_are_their_own(self):
        lv = CFG.level(CampaignType.LOAN_DUE, Urgency.P3)
        self.assertEqual(lv.max_calls_24h, 1)
        self.assertNotEqual(lv, CFG.levels[Urgency.P3])
        self.assertIn("kỳ trả nợ", CFG.zns_for(CampaignType.LOAN_DUE)["contact"])

    def test_missing_campaign_rejected(self):
        raw = copy.deepcopy(self.raw())
        del raw["campaigns"]["statement_due"]
        with self.assertRaisesRegex(ConfigError, "statement_due"):
            parse_config(raw)

    def test_reminder_zns_with_amount_rejected(self):
        raw = copy.deepcopy(self.raw())
        raw["campaigns"]["loan_due"]["zns_templates"]["contact"] = "Khoản {amount} đến hạn"
        with self.assertRaisesRegex(ConfigError, "loan_due"):
            parse_config(raw)

    def test_reminder_gap_below_docs_rejected(self):
        raw = copy.deepcopy(self.raw())
        raw["campaigns"]["loan_due"]["levels"]["P1"]["min_gap_minutes"] = 2
        with self.assertRaisesRegex(ConfigError, "campaign loan_due"):
            parse_config(raw)


class MergeAcrossCampaignTest(unittest.TestCase):
    def test_reminder_not_merged_into_fraud_case(self):
        case = Case(case_id="C1", card_ref="LN-1", customer_id="KH1", phone="0900000001", created_at=NOW,
                    urgency=Urgency.P2, campaign=CampaignType.CARD_FRAUD)
        self.assertIsNone(merge_alert(case, reminder(), [], NOW, CFG))

    def test_same_campaign_merges(self):
        case = Case(case_id="C1", card_ref="LN-1", customer_id="KH1", phone="0900000001", created_at=NOW,
                    urgency=Urgency.P3, campaign=CampaignType.LOAN_DUE)
        r = merge_alert(case, reminder(due=date(2026, 11, 16)), [], NOW, CFG)
        self.assertIsNotNone(r)
        self.assertEqual(r.case.urgency, Urgency.P2)
        self.assertEqual(r.case.status, CaseStatus.OPEN)


class PayloadTest(unittest.TestCase):
    def fraud(self, **kw):
        base = {"alert_id": "TWO-1", "card_ref": "tok-1", "customer_id": "KH1", "phone": "0901",
                "amount_vnd": "1500000", "approved": "true", "card_blocked": False,
                "created_at": "2026-11-16T09:05:00+07:00"}
        base.update(kw)
        return base

    def test_card_fraud_ok(self):
        a = alert_from_payload(self.fraud(phone_sms="0981"), NOW)
        self.assertEqual(a.alert.campaign, CampaignType.CARD_FRAUD)
        self.assertEqual(a.alert.amount_vnd, 1_500_000)
        self.assertTrue(a.alert.approved)
        self.assertFalse(a.alert.card_blocked)
        self.assertEqual(a.alert.created_at.utcoffset(), timedelta(hours=7))
        self.assertEqual(a.phone_sms, "0981")

    def test_phone_sms_same_as_phone_dropped(self):
        self.assertIsNone(alert_from_payload(self.fraud(phone_sms="0901"), NOW).phone_sms)

    def test_missing_fields(self):
        with self.assertRaisesRegex(ValueError, "customer_id"):
            alert_from_payload(self.fraud(customer_id=""), NOW)
        with self.assertRaisesRegex(ValueError, "approved"):
            alert_from_payload({k: v for k, v in self.fraud().items() if k != "approved"}, NOW)

    def test_bad_values(self):
        with self.assertRaisesRegex(ValueError, "campaign"):
            alert_from_payload(self.fraud(campaign="thu_hoi_no"), NOW)
        with self.assertRaisesRegex(ValueError, "amount_vnd"):
            alert_from_payload(self.fraud(amount_vnd="nhieu"), NOW)
        with self.assertRaisesRegex(ValueError, "múi giờ"):
            alert_from_payload(self.fraud(created_at="2026-11-16T09:05:00"), NOW)
        with self.assertRaisesRegex(ValueError, "JSON"):
            alert_from_payload(None, NOW)

    def test_loan_due_needs_due_date(self):
        p = {"campaign": "loan_due", "alert_id": "L1", "card_ref": "LN-1", "customer_id": "KH1",
             "phone": "0902", "amount_vnd": 5_000_000}
        with self.assertRaisesRegex(ValueError, "due_date"):
            alert_from_payload(p, NOW)
        a = alert_from_payload({**p, "due_date": "2026-11-18"}, NOW)
        self.assertEqual(a.alert.due_date, date(2026, 11, 18))
        self.assertEqual(a.alert.created_at, NOW)
        self.assertFalse(a.alert.approved)


class PullJobTest(unittest.TestCase):
    def rows(self):
        return [
            {"contract_ref": "LN-1", "customer_id": "K1", "phone": "0901", "phone_sms": "", "amount_vnd": "100",
             "due_date": "2026-11-18"},   # còn 2 ngày -> lấy
            {"contract_ref": "LN-2", "customer_id": "K2", "phone": "0902", "phone_sms": "0982", "amount_vnd": "200",
             "due_date": "2026-11-20"},   # còn 4 ngày > lookahead 3 -> bỏ
            {"contract_ref": "LN-3", "customer_id": "K3", "phone": "0903", "phone_sms": "", "amount_vnd": "300",
             "due_date": "2026-11-01"},   # quá hạn 15 > max_overdue 10 -> bỏ
            {"contract_ref": "LN-4", "customer_id": "K4", "phone": "0904", "phone_sms": "", "amount_vnd": "400",
             "due_date": "2026-11-10"},   # quá hạn 6 -> lấy, P1
            {"contract_ref": "LN-5", "customer_id": "K5", "phone": "", "phone_sms": "", "amount_vnd": "500",
             "due_date": "2026-11-17"},   # thiếu phone -> lỗi
            {"contract_ref": "LN-6", "customer_id": "K6", "phone": "0906", "phone_sms": "", "amount_vnd": "600",
             "due_date": "18/11/2026"},   # sai định dạng -> lỗi
        ]

    def test_window_and_ids(self):
        alerts, errors = rows_to_alerts(CampaignType.LOAN_DUE, self.rows(), NOW, CFG)
        self.assertEqual([a.alert.alert_id for a in alerts],
                         ["LOAN_DUE-LN-1-2026-11-18", "LOAN_DUE-LN-4-2026-11-10"])
        self.assertEqual(len(errors), 2)
        self.assertIn("dòng 6", errors[0])
        self.assertIn("dòng 7", errors[1])
        self.assertEqual(compute_urgency(alerts[1].alert, 1, CFG)[0], Urgency.P1)

    def test_rerun_same_day_gives_same_ids(self):
        a1, _ = rows_to_alerts(CampaignType.LOAN_DUE, self.rows(), NOW, CFG)
        a2, _ = rows_to_alerts(CampaignType.LOAN_DUE, self.rows(), NOW + timedelta(hours=5), CFG)
        self.assertEqual([x.alert.alert_id for x in a1], [x.alert.alert_id for x in a2])

    def test_card_fraud_not_pullable(self):
        with self.assertRaises(ValueError):
            rows_to_alerts(CampaignType.CARD_FRAUD, [], NOW, CFG)


if __name__ == "__main__":
    unittest.main()
