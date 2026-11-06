"""Parse thoi gian tuong doi/tuyet doi tu loi KH tieng Viet.

KH noi: "goi lai cho toi luc chieu", "5 phut nua", "sang mai", "3 gio chieu"
-> chuyen thanh datetime tuyet doi de scheduler hen lich.

LLM co the tra ve ISO-8601, nhung cung co the tra chuoi tu nhien (nhat la model nho).
Module nay dung lam fallback khi ISO parse that bai, hoac dung doc-lap khi bot
real-time can resolve ngay trong cuoc goi (truoc khi co LLM post-processing).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo


_RELATIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "5 phut nua", "5p sau", "5 phut"
    (re.compile(r"(\d+)\s*(?:phút|phut|p)\s*(?:nữa|nua|sau)?\b", re.I), "minutes"),
    # "1 tieng nua", "2 giờ nữa", "1h nua" — BAT BUOC co "nua/sau" de phan biet voi "3 gio chieu"
    (re.compile(r"(\d+)\s*(?:tiếng|tieng|giờ|gio|h)\s+(?:nữa|nua|sau)\b", re.I), "hours"),
]

_PERIOD_MAP: dict[str, tuple[int, int]] = {
    # keyword -> (default_hour, default_minute)
    "sang": (8, 0),
    "sáng": (8, 0),
    "trua": (12, 0),
    "trưa": (12, 0),
    "chieu": (14, 0),
    "chiều": (14, 0),
    "toi": (19, 0),
    "tối": (19, 0),
}

_TOMORROW = re.compile(
    r"(?:ngày\s*mai|ngay\s*mai|mai|hôm\s*sau|hom\s*sau)", re.I
)

_HOUR_PERIOD = re.compile(
    r"(\d{1,2})\s*(?:giờ|gio|h|:)\s*(\d{1,2})?\s*"
    r"(?:phút|phut|p)?\s*"
    r"(sáng|sang|chiều|chieu|tối|toi|trưa|trua)?",
    re.I,
)


def parse_callback_time(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    """Parse thoi gian callback tu chuoi tieng Viet. None neu khong hieu.

    Tat ca datetime tra ve deu timezone-aware (theo tz).
    """
    text = text.strip()
    if not text:
        return None

    # Thu ISO-8601 truoc
    iso = _try_iso(text, tz)
    if iso is not None:
        return iso

    now_local = now.astimezone(tz)

    # "3 gio chieu", "14:30", "2 gio 30 phut chieu", "3h sang mai"
    explicit = _try_explicit_hour(text, now_local, tz)
    if explicit is not None:
        return explicit

    # "5 phut nua", "1 tieng nua" — sau explicit de "2 gio 30 phut chieu" khong bi bat nham
    rel = _try_relative(text, now_local)
    if rel is not None:
        return rel

    # "chieu nay", "sang mai", "toi mai"
    period = _try_period(text, now_local, tz)
    if period is not None:
        return period

    return None


def _try_iso(text: str, tz: tzinfo) -> datetime | None:
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.utcoffset() is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        return None


def _try_relative(text: str, now: datetime) -> datetime | None:
    for pat, unit in _RELATIVE_PATTERNS:
        m = pat.search(text)
        if m:
            val = int(m.group(1))
            if val <= 0:
                continue
            return now + timedelta(**{unit: val})
    return None


_RELATIVE_HOUR_MARKER = re.compile(
    r"\d+\s*(?:tiếng|tieng|giờ|gio|h)\s+(?:nữa|nua|sau)\b", re.I
)


def _try_explicit_hour(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    if _RELATIVE_HOUR_MARKER.search(text):
        return None
    m = _HOUR_PERIOD.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    period = (m.group(3) or "").lower().strip()

    if period in ("chiều", "chieu", "tối", "toi"):
        if hour < 12:
            hour += 12
    elif period in ("sáng", "sang", "trưa", "trua"):
        if hour == 12 and period in ("sáng", "sang"):
            hour = 0
    else:
        if hour < 12 and now.hour >= 12:
            hour += 12

    tomorrow = _TOMORROW.search(text) is not None
    day = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow:
        day += timedelta(days=1)
    elif day <= now:
        day += timedelta(days=1)
    return day


def _try_period(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    lower = text.lower()
    tomorrow = _TOMORROW.search(lower) is not None

    for keyword, (h, m) in _PERIOD_MAP.items():
        if keyword in lower:
            day = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if tomorrow:
                day += timedelta(days=1)
            elif day <= now:
                day += timedelta(days=1)
            return day
    # "ngay mai" / "mai" ma khong co buoi cu the -> 9h sang
    if tomorrow:
        return now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return None
