"""Test callback time parser cho cac bieu thuc thoi gian tieng Viet."""
import unittest
from datetime import datetime, timedelta, timezone

from callback.time_parser import parse_callback_time

VN = timezone(timedelta(hours=7))


def now_at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=VN)


class RelativeTimeTest(unittest.TestCase):
    def test_5_phut_nua(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("5 phút nữa", now, VN), now_at(21, 10, 5))

    def test_5p_sau(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("5p sau", now, VN), now_at(21, 10, 5))

    def test_30_phut(self):
        now = now_at(21, 14, 0)
        self.assertEqual(parse_callback_time("30 phut", now, VN), now_at(21, 14, 30))

    def test_1_tieng_nua(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("1 tiếng nữa", now, VN), now_at(21, 11, 0))

    def test_2_gio_sau(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("2 giờ sau", now, VN), now_at(21, 12, 0))

    def test_1h_nua(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("1h nua", now, VN), now_at(21, 11, 0))


class PeriodTest(unittest.TestCase):
    def test_chieu_nay_from_morning(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("chiều nay", now, VN), now_at(21, 14, 0))

    def test_chieu_when_already_afternoon_means_tomorrow(self):
        now = now_at(21, 15, 0)
        self.assertEqual(parse_callback_time("chiều", now, VN), now_at(22, 14, 0))

    def test_sang_mai(self):
        now = now_at(21, 15, 0)
        self.assertEqual(parse_callback_time("sáng mai", now, VN), now_at(22, 8, 0))

    def test_toi_nay_from_afternoon(self):
        now = now_at(21, 15, 0)
        self.assertEqual(parse_callback_time("tối nay", now, VN), now_at(21, 19, 0))

    def test_toi_nay_from_evening_means_tomorrow(self):
        now = now_at(21, 20, 0)
        self.assertEqual(parse_callback_time("tối nay", now, VN), now_at(22, 19, 0))

    def test_ngay_mai_no_period(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("ngày mai", now, VN), now_at(22, 9, 0))

    def test_mai(self):
        now = now_at(21, 21, 0)
        self.assertEqual(parse_callback_time("mai", now, VN), now_at(22, 9, 0))

    def test_chieu_mai(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("chiều mai", now, VN), now_at(22, 14, 0))

    def test_trua(self):
        now = now_at(21, 9, 0)
        self.assertEqual(parse_callback_time("trưa", now, VN), now_at(21, 12, 0))


class ExplicitHourTest(unittest.TestCase):
    def test_3_gio_chieu(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("3 giờ chiều", now, VN), now_at(21, 15, 0))

    def test_3h_chieu(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("3h chiều", now, VN), now_at(21, 15, 0))

    def test_14_30(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("14:30", now, VN), now_at(21, 14, 30))

    def test_2_gio_30_phut_chieu(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("2 giờ 30 phút chiều", now, VN), now_at(21, 14, 30))

    def test_9h_sang_mai(self):
        now = now_at(21, 15, 0)
        self.assertEqual(parse_callback_time("9h sáng mai", now, VN), now_at(22, 9, 0))

    def test_hour_already_passed_means_tomorrow(self):
        now = now_at(21, 16, 0)
        self.assertEqual(parse_callback_time("3 giờ chiều", now, VN), now_at(22, 15, 0))

    def test_10h_sang(self):
        now = now_at(21, 8, 0)
        self.assertEqual(parse_callback_time("10h sáng", now, VN), now_at(21, 10, 0))

    def test_7_gio_toi(self):
        now = now_at(21, 10, 0)
        self.assertEqual(parse_callback_time("7 giờ tối", now, VN), now_at(21, 19, 0))


class ISOTest(unittest.TestCase):
    def test_iso_with_offset(self):
        now = now_at(21, 10, 0)
        result = parse_callback_time("2026-10-21T15:00:00+07:00", now, VN)
        self.assertEqual(result, datetime(2026, 10, 21, 15, 0, tzinfo=VN))

    def test_iso_without_offset_gets_tz(self):
        now = now_at(21, 10, 0)
        result = parse_callback_time("2026-10-21T15:00:00", now, VN)
        self.assertEqual(result, datetime(2026, 10, 21, 15, 0, tzinfo=VN))

    def test_iso_utc_z(self):
        now = now_at(21, 10, 0)
        result = parse_callback_time("2026-10-21T08:00:00Z", now, VN)
        self.assertEqual(result.utcoffset(), timedelta(0))


class EdgeCaseTest(unittest.TestCase):
    def test_empty_string(self):
        self.assertIsNone(parse_callback_time("", now_at(21, 10), VN))

    def test_nonsense(self):
        self.assertIsNone(parse_callback_time("abc xyz", now_at(21, 10), VN))

    def test_whitespace(self):
        self.assertIsNone(parse_callback_time("   ", now_at(21, 10), VN))

    def test_zero_minutes(self):
        self.assertIsNone(parse_callback_time("0 phút nữa", now_at(21, 10), VN))


if __name__ == "__main__":
    unittest.main()
