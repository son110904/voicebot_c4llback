"""Tự tính mức khẩn P1/P2/P3 từ dữ liệu cảnh báo (không để người nhập tay)."""
from __future__ import annotations

from .config import Config
from .models import Alert, Urgency


def compute_urgency(alert: Alert, alert_count: int, cfg: Config) -> tuple[Urgency, str]:
    """Trả về (mức khẩn, lý do). alert_count = số cảnh báo của case tính cả cảnh báo này."""
    if not alert.card_blocked and alert.approved:
        return Urgency.P1, "thẻ còn mở và GD đã được cấp phép - có thể đang mất tiền"
    if alert.amount_vnd >= cfg.large_amount_vnd:
        return Urgency.P1, f"số tiền >= {cfg.large_amount_vnd:,} VND"
    if alert_count >= cfg.multi_alert_count:
        return Urgency.P1, f"{alert_count} cảnh báo cho cùng thẻ/KH"
    if alert.card_blocked:
        return Urgency.P2, "thẻ đã bị khóa tự động"
    return Urgency.P3, "GD bị từ chối, thẻ chưa khóa"
