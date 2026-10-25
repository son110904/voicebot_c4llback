"""Kiểu dữ liệu dùng chung cho campaign rules gọi lại."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Urgency(str, Enum):
    P1 = "P1"  # khẩn: thẻ còn mở + GD được cấp phép / số tiền lớn / nhiều cảnh báo
    P2 = "P2"  # thẻ đã bị khóa tự động
    P3 = "P3"  # GD bị từ chối, thẻ chưa khóa

    @property
    def rank(self) -> int:  # số nhỏ = khẩn hơn
        return int(self.value[1])


def most_urgent(a: Urgency, b: Urgency) -> Urgency:
    return a if a.rank <= b.rank else b


class CaseStatus(str, Enum):
    OPEN = "OPEN"            # bot còn đang xử lý
    DONE = "DONE"            # KH đã xác nhận xong
    HUMAN = "HUMAN"          # đã chuyển ĐTV
    STOPPED = "STOPPED"      # bot dừng (nhầm số, KH không cho gọi...)
    EXHAUSTED = "EXHAUSTED"  # hết lượt, không liên hệ được
    CLOSED = "CLOSED"        # đóng từ ngoài (ĐTV xử lý xong, TWO đóng cảnh báo...)


class Outcome(str, Enum):
    # --- lấy thẳng từ tổng đài, không cần LLM (xem pbx.py)
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    REJECTED = "rejected"                  # KH bấm từ chối cuộc gọi
    UNREACHABLE = "unreachable"            # tắt máy / ngoài vùng phủ sóng
    INVALID_NUMBER = "invalid_number"      # số không tồn tại
    VOICEMAIL = "voicemail"
    SHORT_HANGUP = "short_hangup"          # nhấc máy rồi cúp < 5 giây / im lặng
    DROPPED = "dropped"                    # rớt giữa chừng (Incomplete)
    TRANSFERRED = "transferred"            # đã chuyển ĐTV ngay trong cuộc gọi
    SYSTEM_ERROR = "system_error"          # lỗi phía mình: SIP trunk, media adapter, agent...
    BAD_AUDIO = "bad_audio"                # âm thanh kém, bot không nghe được
    # --- LLM gán nhãn từ transcript
    VERIFIED_CONFIRMED = "verified_confirmed"          # KH xác nhận chính chủ thực hiện GD
    VERIFIED_DENIED = "verified_denied"                # KH phủ nhận GD -> nghi gian lận thật
    WRONG_NUMBER = "wrong_number"                      # người nghe không phải chủ thẻ, không quen biết
    RELATIVE_ANSWERED = "relative_answered"            # người nhà nghe máy -> số đúng
    CALLBACK_REQUESTED = "callback_requested"          # KH hẹn gọi lại
    VERIFY_FAILED = "verify_failed"                    # xác thực danh tính sai nhiều lần -> nghi SIM bị chiếm
    ANGRY = "angry"                                    # bức xúc / phàn nàn
    DO_NOT_CALL = "do_not_call"                        # yêu cầu không liên hệ
    OTHER_NUMBER_REQUESTED = "other_number_requested"  # đòi gọi sang số khác
    SCAM_SUSPECTED_HANGUP = "scam_suspected_hangup"    # KH tưởng lừa đảo nên cúp
    NEED_HUMAN = "need_human"                          # đòi gặp người / bot không hiểu 2 lần / ngoài phạm vi
    CUSTOMER_UNSURE = "customer_unsure"                # KH không chắc chắn sau 1 lần hỏi lại -> ĐTV (docs case 5)
    UNCLEAR = "unclear"                                # không xác định được (hoặc LLM không chắc)
    # --- bot/scheduler báo (không phải LLM) khi thao tác hệ thống thất bại
    ACTION_FAILED = "action_failed"                    # khóa thẻ / ghi kết quả vào core bank fail -> ĐTV HIGH (docs case 7)


# Nhãn chỉ LLM mới gán được -> áp ngưỡng độ tin cậy
LLM_OUTCOMES = frozenset({
    Outcome.VERIFIED_CONFIRMED, Outcome.VERIFIED_DENIED, Outcome.WRONG_NUMBER,
    Outcome.RELATIVE_ANSWERED, Outcome.CALLBACK_REQUESTED, Outcome.VERIFY_FAILED,
    Outcome.ANGRY, Outcome.DO_NOT_CALL, Outcome.OTHER_NUMBER_REQUESTED,
    Outcome.SCAM_SUSPECTED_HANGUP, Outcome.NEED_HUMAN, Outcome.CUSTOMER_UNSURE,
})


class Group(str, Enum):
    DONE = "XONG"
    STOP = "DỪNG BOT"
    HUMAN = "NGƯỜI THẬT XỬ LÝ"
    RETRY_CUSTOMER = "GỌI LẠI - LỖI PHÍA KHÁCH"     # tính lượt
    RETRY_SYSTEM = "GỌI LẠI - LỖI PHÍA HỆ THỐNG"    # không tính lượt
    APPOINTMENT = "KHÁCH HẸN"                       # không tính lượt


# Outcome -> (nhóm, cuộc gọi này có tính vào giới hạn số lượt không)
OUTCOME_GROUP: dict[Outcome, tuple[Group, bool]] = {
    Outcome.VERIFIED_CONFIRMED: (Group.DONE, True),
    Outcome.TRANSFERRED: (Group.HUMAN, True),
    Outcome.VERIFIED_DENIED: (Group.HUMAN, True),
    Outcome.NEED_HUMAN: (Group.HUMAN, True),
    Outcome.ANGRY: (Group.HUMAN, True),
    Outcome.CUSTOMER_UNSURE: (Group.HUMAN, True),
    Outcome.ACTION_FAILED: (Group.HUMAN, True),
    Outcome.UNCLEAR: (Group.HUMAN, True),
    Outcome.WRONG_NUMBER: (Group.STOP, True),
    Outcome.INVALID_NUMBER: (Group.STOP, False),
    Outcome.VERIFY_FAILED: (Group.STOP, True),
    Outcome.DO_NOT_CALL: (Group.STOP, True),
    Outcome.OTHER_NUMBER_REQUESTED: (Group.STOP, True),
    Outcome.NO_ANSWER: (Group.RETRY_CUSTOMER, True),
    Outcome.BUSY: (Group.RETRY_CUSTOMER, True),
    Outcome.REJECTED: (Group.RETRY_CUSTOMER, True),
    Outcome.UNREACHABLE: (Group.RETRY_CUSTOMER, True),
    Outcome.VOICEMAIL: (Group.RETRY_CUSTOMER, True),
    Outcome.SHORT_HANGUP: (Group.RETRY_CUSTOMER, True),
    Outcome.RELATIVE_ANSWERED: (Group.RETRY_CUSTOMER, True),
    Outcome.SCAM_SUSPECTED_HANGUP: (Group.RETRY_CUSTOMER, True),
    Outcome.DROPPED: (Group.RETRY_CUSTOMER, True),
    Outcome.SYSTEM_ERROR: (Group.RETRY_SYSTEM, False),
    Outcome.BAD_AUDIO: (Group.RETRY_SYSTEM, False),
    Outcome.CALLBACK_REQUESTED: (Group.APPOINTMENT, False),
}

# Máy KH không đổ chuông -> không tính vào khoảng cách tối thiểu giữa 2 lần gọi
NOT_RINGING = frozenset({Outcome.SYSTEM_ERROR, Outcome.INVALID_NUMBER})


class Action(str, Enum):
    DIAL_NOW = "DIAL_NOW"              # (pre_dial_check) quay số ngay
    SCHEDULE_CALL = "SCHEDULE_CALL"    # hẹn giờ gọi (next_call_at)
    CLOSE = "CLOSE"                    # xong, đóng case
    HAND_TO_DTV = "HAND_TO_DTV"        # bot dừng, đưa vào hàng đợi ĐTV
    STOP = "STOP"                      # bot dừng (có thể kèm ticket / ĐTV)
    EXHAUSTED = "EXHAUSTED"            # hết lượt: SMS cuối + ticket "không liên hệ được"


@dataclass(frozen=True)
class Alert:
    alert_id: str
    card_ref: str          # mã tham chiếu thẻ đã che/token hóa - KHÔNG BAO GIỜ là số thẻ đầy đủ
    customer_id: str
    amount_vnd: int
    approved: bool         # GD được cấp phép (True) hay bị từ chối (False)
    card_blocked: bool     # hệ thống đã tự khóa thẻ chưa
    created_at: datetime


@dataclass(frozen=True)
class Case:
    case_id: str
    card_ref: str
    customer_id: str
    phone: str                          # số gọi chính
    created_at: datetime
    urgency: Urgency
    phone_sms: str | None = None        # Phone_SMS - số dự phòng (vẫn gọi là phone_sms theo tên trường bank)
    status: CaseStatus = CaseStatus.OPEN
    card_open: bool = True
    alert_count: int = 1
    zns_sent: int = 0                   # số lượng ZNS đã gửi (thay cho sms_sent cũ)
    appointments: int = 0
    do_not_call: bool = False
    blocked_phones: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Attempt:
    """Một cuộc gọi đã thực hiện (đã lưu DB)."""
    started_at: datetime
    phone: str
    outcome: Outcome
    counted: bool


@dataclass(frozen=True)
class Event:
    """Kết quả cuộc gọi vừa kết thúc (từ tổng đài + nhãn LLM nếu có)."""
    started_at: datetime
    phone: str
    outcome: Outcome
    verified: bool = False                 # đã xác thực/xác nhận xong trước khi rớt chưa
    callback_at: datetime | None = None    # giờ KH hẹn (LLM tách từ câu nói)
    llm_confidence: float | None = None
    # context cho ĐTV khi chuyển máy (docs: screen-pop)
    transcript: str | None = None                  # transcript gốc (đã che PII trước khi lưu)
    transcript_summary: str | None = None          # tóm tắt do bot/LLM sinh
    recording_id: str | None = None                # mã bản ghi âm trên tổng đài
    misunderstanding_count: int = 0                # số lần bot phải hỏi lại trong cuộc này


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    reason: str
    group: Group | None = None
    outcome: Outcome | None = None         # outcome đã dùng để quyết định (sau khi xét độ tin cậy LLM)
    counts_attempt: bool = False
    next_call_at: datetime | None = None
    target_phone: str | None = None
    zns_template: str | None = None        # khóa mẫu trong config: "pre_call" / "contact" / "final"
    zns_to: str | None = None
    ticket: str | None = None
    dtv_priority: str | None = None        # "HIGH" / "NORMAL"
    block_phone: str | None = None         # không bao giờ gọi lại số này cho case này
    mark_do_not_call: bool = False
    appointment: bool = False
