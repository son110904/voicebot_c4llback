"""Hợp đồng dữ liệu cho một cảnh báo đầu vào (JSON webhook hoặc một dòng CSV).

Trường chung (mọi campaign):
    campaign      card_fraud | loan_due | statement_due   (mặc định card_fraud)
    alert_id      duy nhất; gửi lại cùng id -> bỏ qua (idempotent)
    card_ref      mã thẻ/hợp đồng đã token hóa - KHÔNG phải số thẻ đầy đủ
    customer_id   CIF
    phone         số gọi chính
    phone_sms     số dự phòng (tùy chọn)
    amount_vnd    số nguyên >= 0
    created_at    ISO-8601 có offset (tùy chọn, mặc định = lúc nhận)
card_fraud thêm:  approved (bool), card_blocked (bool)
loan_due / statement_due thêm:  due_date (YYYY-MM-DD)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from callback.models import Alert, CampaignType

_REQUIRED = ("alert_id", "card_ref", "customer_id", "phone")
_REMINDERS = (CampaignType.LOAN_DUE, CampaignType.STATEMENT_DUE)


@dataclass(frozen=True)
class AlertIn:
    alert: Alert
    phone: str
    phone_sms: str | None


def alert_from_payload(data: Mapping[str, Any] | None, now: datetime) -> AlertIn:
    """Kiểm tra và dựng Alert. Sai -> ValueError với thông điệp tiếng Việt để trả 400."""
    if not isinstance(data, Mapping):
        raise ValueError("body phải là JSON object")
    missing = [k for k in _REQUIRED if not _text(data.get(k))]
    if missing:
        raise ValueError("thiếu trường: " + ", ".join(missing))

    try:
        campaign = CampaignType(str(data.get("campaign") or CampaignType.CARD_FRAUD.value))
    except ValueError:
        raise ValueError(f"campaign không hợp lệ: {data.get('campaign')!r}; "
                         f"chọn một trong {[c.value for c in CampaignType]}") from None

    amount = _int(data.get("amount_vnd", 0), "amount_vnd")
    if amount < 0:
        raise ValueError("amount_vnd phải >= 0")

    created_at = _datetime(data.get("created_at"), now)

    if campaign is CampaignType.CARD_FRAUD:
        if "approved" not in data or "card_blocked" not in data:
            raise ValueError("card_fraud cần approved và card_blocked")
        approved, card_blocked = _bool(data["approved"], "approved"), _bool(data["card_blocked"], "card_blocked")
        due = None
    else:
        approved = card_blocked = False
        due = _date(data.get("due_date"))
        if due is None:
            raise ValueError(f"{campaign.value} cần due_date dạng YYYY-MM-DD")

    phone = _text(data["phone"])
    phone_sms = _text(data.get("phone_sms")) or None
    if phone_sms == phone:
        phone_sms = None

    alert = Alert(alert_id=_text(data["alert_id"]), card_ref=_text(data["card_ref"]),
                  customer_id=_text(data["customer_id"]), amount_vnd=amount, approved=approved,
                  card_blocked=card_blocked, created_at=created_at, campaign=campaign, due_date=due)
    return AlertIn(alert, phone, phone_sms)


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _int(v: Any, name: str) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"{name} phải là số") from None


def _bool(v: Any, name: str) -> bool:
    if isinstance(v, bool):
        return v
    s = _text(v).lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n", ""):
        return False
    raise ValueError(f"{name} phải là true/false")


def _date(v: Any) -> date | None:
    s = _text(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f"due_date không đúng dạng YYYY-MM-DD: {s!r}") from None


def _datetime(v: Any, default: datetime) -> datetime:
    s = _text(v)
    if not s:
        return default
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"created_at không đúng ISO-8601: {s!r}") from None
    if dt.utcoffset() is None:
        raise ValueError("created_at phải có múi giờ (vd. +07:00)")
    return dt
