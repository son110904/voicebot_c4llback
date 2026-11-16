"""Tự tính mức khẩn P1/P2/P3 từ dữ liệu cảnh báo (không để người nhập tay).

card_fraud: theo trạng thái thẻ, cấp phép, số tiền, số cảnh báo.
loan_due / statement_due: theo số ngày còn lại tới hạn (tính từ created_at của cảnh báo).
"""
from __future__ import annotations

from .config import Config
from .models import Alert, CampaignType, Urgency


def compute_urgency(alert: Alert, alert_count: int, cfg: Config) -> tuple[Urgency, str]:
    """Trả về (mức khẩn, lý do). alert_count = số cảnh báo của case tính cả cảnh báo này."""
    if alert.campaign is CampaignType.CARD_FRAUD:
        return _card_fraud(alert, alert_count, cfg)
    return _reminder(alert, cfg)


def _card_fraud(alert: Alert, alert_count: int, cfg: Config) -> tuple[Urgency, str]:
    if not alert.card_blocked and alert.approved:
        return Urgency.P1, "thẻ còn mở và GD đã được cấp phép - có thể đang mất tiền"
    if alert.amount_vnd >= cfg.large_amount_vnd:
        return Urgency.P1, f"số tiền >= {cfg.large_amount_vnd:,} VND"
    if alert_count >= cfg.multi_alert_count:
        return Urgency.P1, f"{alert_count} cảnh báo cho cùng thẻ/KH"
    if alert.card_blocked:
        return Urgency.P2, "thẻ đã bị khóa tự động"
    return Urgency.P3, "GD bị từ chối, thẻ chưa khóa"


def _reminder(alert: Alert, cfg: Config) -> tuple[Urgency, str]:
    if alert.due_date is None:
        raise ValueError(f"cảnh báo {alert.campaign.value} phải có due_date")
    camp = cfg.campaigns[alert.campaign]
    days_left = (alert.due_date - alert.created_at.astimezone(cfg.tz).date()).days
    if days_left < 0:
        overdue = -days_left
        if overdue >= camp.overdue_days_p1:
            return Urgency.P1, f"quá hạn {overdue} ngày (>= {camp.overdue_days_p1})"
        return Urgency.P2, f"quá hạn {overdue} ngày"
    if days_left <= camp.urgent_within_days:
        return Urgency.P2, f"còn {days_left} ngày tới hạn (<= {camp.urgent_within_days})"
    return Urgency.P3, f"còn {days_left} ngày tới hạn"
