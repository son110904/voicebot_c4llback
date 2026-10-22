"""Map kết quả tổng đài (call_status theo docs + mã SIP) -> Outcome.

Cuộc gọi không có hội thoại thì map thẳng ở đây, không tốn LLM.
Cuộc gọi có hội thoại -> trả None, tầng LLM đọc transcript để gán nhãn.

Bảng mã SIP theo RFC 3261 là GIẢ ĐỊNH: cần đối chiếu log Asterisk/Freeswitch thật,
mỗi nhà mạng trả mã hơi khác nhau.
"""
from __future__ import annotations

from .models import Outcome

SHORT_HANGUP_SECONDS = 5

_SIP: dict[int, Outcome] = {
    486: Outcome.BUSY, 600: Outcome.BUSY,
    603: Outcome.REJECTED,
    404: Outcome.INVALID_NUMBER, 410: Outcome.INVALID_NUMBER,
    484: Outcome.INVALID_NUMBER, 604: Outcome.INVALID_NUMBER,
    480: Outcome.UNREACHABLE,
    408: Outcome.NO_ANSWER, 487: Outcome.NO_ANSWER,
}


def outcome_from_pbx(call_status: str, sip_code: int | None = None,
                     answered_seconds: float | None = None,
                     machine_detected: bool = False) -> Outcome | None:
    """call_status: Completed / No Answer / Busy / Failed / Transferred / Incomplete (theo docs).

    Incomplete -> DROPPED; nếu có transcript thì nhãn LLM cụ thể hơn được ưu tiên
    (ví dụ KH đã phủ nhận GD rồi mới rớt). Mã SIP lạ khi Failed -> coi là lỗi hệ thống.
    """
    s = call_status.strip().lower()
    if s == "transferred":
        return Outcome.TRANSFERRED
    if s == "busy":
        return Outcome.BUSY
    if s == "no answer":
        return Outcome.NO_ANSWER
    if s == "failed":
        return _SIP.get(sip_code, Outcome.SYSTEM_ERROR)
    if s in ("completed", "incomplete"):
        if machine_detected:
            return Outcome.VOICEMAIL
        if answered_seconds is not None and answered_seconds < SHORT_HANGUP_SECONDS:
            return Outcome.SHORT_HANGUP
        return None if s == "completed" else Outcome.DROPPED
    raise ValueError(f"call_status không nằm trong danh sách của docs: {call_status!r}")
