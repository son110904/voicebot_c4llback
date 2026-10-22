"""Campaign rules

Các hàm công khai đều là hàm thuần: không đọc/ghi DB, không gọi mạng, không tự lấy giờ hệ thống.
Cùng đầu vào -> cùng quyết định, nên test được và tái hiện được khi cần giải trình.

- decide()          sau mỗi cuộc gọi: làm gì tiếp (gọi lại lúc nào, SMS, chuyển ĐTV, dừng...)
- pre_dial_check()  ngay trước khi quay số: case còn được gọi không, giờ này gọi được không
- merge_alert()     cảnh báo mới của cùng thẻ/KH khi case đang mở: gộp, nâng mức khẩn, không reset lượt
- apply_decision()  cập nhật Case theo Decision (cho tầng DB/scheduler)

Mỗi Decision có rule_id + reason để ghi bảng decisions và giải thích với nghiệp vụ.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from .config import Config, LevelConfig, OutcomeConfig, Window
from .models import (
    LLM_OUTCOMES, NOT_RINGING, OUTCOME_GROUP, Action, Alert, Attempt, Case, CaseStatus,
    Decision, Event, Group, Outcome, Urgency, most_urgent,
)
from .urgency import compute_urgency

# Số ĐT -> các thời điểm đã gọi (có tính lượt) tới số đó từ các case KHÁC, trong 24h gần nhất
PhoneHistory = Mapping[str, Sequence[datetime]]

# Số chính lỗi liên tiếp kiểu này -> có thể KH không dùng số đó nữa -> thử Phone_SMS
_SWITCH_TRIGGERS = frozenset({Outcome.NO_ANSWER, Outcome.UNREACHABLE, Outcome.VOICEMAIL})

_LABEL = {
    Outcome.NO_ANSWER: "Không nghe máy",
    Outcome.BUSY: "Máy bận",
    Outcome.REJECTED: "KH bấm từ chối",
    Outcome.UNREACHABLE: "Thuê bao tắt máy/ngoài vùng phủ sóng",
    Outcome.VOICEMAIL: "Vào hộp thư thoại (không để lại lời nhắn)",
    Outcome.SHORT_HANGUP: "Nhấc máy rồi cúp ngay/im lặng",
    Outcome.RELATIVE_ANSWERED: "Người nhà nghe máy",
    Outcome.SCAM_SUSPECTED_HANGUP: "KH nghi cuộc gọi lừa đảo nên cúp máy",
    Outcome.DROPPED: "Rớt cuộc gọi trước khi xác thực xong",
    Outcome.SYSTEM_ERROR: "Lỗi phía hệ thống",
    Outcome.BAD_AUDIO: "Âm thanh kém, bot không nghe rõ",
}

_HUMAN = {
    Outcome.VERIFIED_DENIED: ("H1", "KH phủ nhận giao dịch -> nghi gian lận thật, ĐTV xử lý (khóa thẻ/tra soát)"),
    Outcome.NEED_HUMAN: ("H2", "KH muốn gặp người thật / bot không hiểu 2 lần / ngoài phạm vi bot"),
    Outcome.ANGRY: ("H3", "KH bức xúc/phàn nàn -> bot không gọi lại"),
    Outcome.UNCLEAR: ("H4", "Không xác định được kết quả cuộc gọi -> ĐTV nghe lại ghi âm"),
    Outcome.CUSTOMER_UNSURE: ("H5", "KH không chắc chắn sau 1 lần hỏi lại -> ĐTV xác nhận lại giao dịch"),
    Outcome.ACTION_FAILED: ("H6", "Thao tác hệ thống thất bại (khóa thẻ/ghi kết quả core) -> ĐTV xử lý thủ công"),
}

_STATUS_AFTER = {
    Action.DIAL_NOW: CaseStatus.OPEN,
    Action.SCHEDULE_CALL: CaseStatus.OPEN,
    Action.CLOSE: CaseStatus.DONE,
    Action.HAND_TO_DTV: CaseStatus.HUMAN,
    Action.STOP: CaseStatus.STOPPED,
    Action.EXHAUSTED: CaseStatus.EXHAUSTED,
}


@dataclass(frozen=True)
class MergeResult:
    case: Case            # case sau khi gộp
    decision: Decision


@dataclass(frozen=True)
class _Ctx:
    case: Case
    history: tuple[Attempt, ...]      # các lần gọi trước (+ cuộc vừa xong, nếu gọi từ decide)
    now: datetime
    cfg: Config
    phone_history: PhoneHistory
    event: Event | None = None
    outcome: Outcome | None = None    # outcome sau khi xét độ tin cậy LLM

    @property
    def level(self) -> LevelConfig:
        return self.cfg.levels[self.case.urgency]


# =============================================================== hàm công khai

def decide(case: Case, attempts: Sequence[Attempt], event: Event, now: datetime,
           cfg: Config, phone_history: PhoneHistory | None = None) -> Decision:
    """Quyết định sau cuộc gọi vừa kết thúc. attempts = các lần gọi TRƯỚC cuộc này."""
    _require_aware(now, event.started_at, event.callback_at, *(a.started_at for a in attempts))
    guard = _guard(case)
    if guard:
        return guard
    outcome = _effective_outcome(event, cfg)
    group, counts = OUTCOME_GROUP[outcome]
    if outcome is Outcome.DROPPED and event.verified:
        group = Group.DONE
    current = Attempt(event.started_at, event.phone, outcome, counts)
    ctx = _Ctx(case, (*attempts, current), now, cfg, phone_history or {}, event, outcome)
    return replace(_HANDLERS[group](ctx), group=group, outcome=outcome, counts_attempt=counts)


def pre_dial_check(case: Case, attempts: Sequence[Attempt], phone: str, now: datetime,
                   cfg: Config, phone_history: PhoneHistory | None = None,
                   appointment: bool = False) -> Decision:
    """Kiểm tra lại ngay trước khi quay số (trạng thái case có thể đã đổi từ lúc hẹn lịch)."""
    _require_aware(now, *(a.started_at for a in attempts))
    guard = _guard(case)
    if guard:
        return guard
    ctx = _Ctx(case, tuple(attempts), now, cfg, phone_history or {})
    target = phone
    if phone in case.blocked_phones:
        target = _choose_phone(ctx)
        if target is None:
            return Decision(Action.STOP, "K1", "Số dự kiến gọi đã bị chặn và không còn số nào khác",
                            ticket="KHONG_CON_SO_HOP_LE", dtv_priority=_prio(ctx))
    # cuộc KH đã hẹn không bị chặn bởi giới hạn số lượt, nhưng vẫn phải đủ khoảng cách + đúng khung giờ
    t, notes = _earliest_call(ctx, target, now, counts=not appointment)
    if t is None:
        return _exhausted(ctx, "K2", "; ".join(notes))
    if t > now:
        return Decision(Action.SCHEDULE_CALL, "K3", "Chưa được gọi lúc này: " + "; ".join(notes),
                        next_call_at=t, target_phone=target, appointment=appointment)
    return Decision(Action.DIAL_NOW, "K0", "Case còn mở, đủ điều kiện -> quay số",
                    target_phone=target, appointment=appointment)


def merge_alert(case: Case, alert: Alert, attempts: Sequence[Attempt], now: datetime,
                cfg: Config, phone_history: PhoneHistory | None = None) -> MergeResult | None:
    """Gộp cảnh báo mới vào case đang mở của cùng thẻ/KH. None = không gộp, tạo case mới."""
    _require_aware(now, alert.created_at, *(a.started_at for a in attempts))
    if case.status is not CaseStatus.OPEN:
        return None
    if alert.card_ref != case.card_ref and alert.customer_id != case.customer_id:
        return None
    n = case.alert_count + 1
    new_u, why = compute_urgency(alert, n, cfg)
    urgency = most_urgent(case.urgency, new_u)
    if alert.card_ref == case.card_ref:
        card_open = not alert.card_blocked
    else:  # thẻ khác của cùng KH: còn 1 thẻ mở là còn rủi ro
        card_open = case.card_open or not alert.card_blocked
    merged = replace(case, alert_count=n, urgency=urgency, card_open=card_open)
    head = (f"Gộp cảnh báo {alert.alert_id} vào case {case.case_id} (cảnh báo thứ {n}); "
            f"mức khẩn {case.urgency.value}->{urgency.value} ({why}); giữ nguyên số lượt đã gọi")

    guard = _guard(merged)
    if guard:
        return MergeResult(merged, replace(guard, reason=head + "; " + guard.reason))
    ctx = _Ctx(merged, tuple(attempts), now, cfg, phone_history or {})
    phone = _choose_phone(ctx)
    if phone is None:
        return MergeResult(merged, Decision(Action.HAND_TO_DTV, "M2", head + "; không còn số hợp lệ để gọi",
                                            dtv_priority=_prio(ctx), ticket="CHUYEN_DTV"))
    t, notes = _earliest_call(ctx, phone, now)
    if t is None:
        return MergeResult(merged, _exhausted(ctx, "M3", "; ".join(notes)))
    return MergeResult(merged, Decision(Action.SCHEDULE_CALL, "M1", _join(head + "; gọi sớm nhất có thể", notes),
                                        next_call_at=t, target_phone=phone))


def apply_decision(case: Case, d: Decision) -> Case:
    """Case sau khi thực hiện Decision (đếm SMS, số lần hẹn, chặn số, trạng thái)."""
    status = case.status if d.rule_id == "G1" else _STATUS_AFTER[d.action]
    blocked = case.blocked_phones | {d.block_phone} if d.block_phone else case.blocked_phones
    return replace(
        case,
        status=status,
        sms_sent=case.sms_sent + (1 if d.sms_template else 0),
        appointments=case.appointments + (1 if d.appointment and d.action is Action.SCHEDULE_CALL else 0),
        do_not_call=case.do_not_call or d.mark_do_not_call,
        blocked_phones=frozenset(blocked),
    )


# =============================================================== xử lý theo nhóm

def _done(ctx: _Ctx) -> Decision:
    if ctx.outcome is Outcome.DROPPED:
        return Decision(Action.CLOSE, "D2", "Cuộc gọi rớt nhưng KH đã xác nhận xong -> không cần gọi lại")
    return Decision(Action.CLOSE, "D1", "KH xác nhận chính chủ thực hiện giao dịch -> đóng case")


def _human(ctx: _Ctx) -> Decision:
    o, ev = ctx.outcome, ctx.event
    if o is Outcome.TRANSFERRED:
        return Decision(Action.HAND_TO_DTV, "H0", "Đã chuyển ĐTV ngay trong cuộc gọi -> bot dừng")
    rid, text = _HUMAN[o]
    if ev.outcome is not o:
        text += (f" (LLM gán '{ev.outcome.value}' nhưng độ tin cậy {ev.llm_confidence:.2f}"
                 f" < {ctx.cfg.llm_min_confidence})")
    # docs: không có ĐTV rảnh -> đưa vào hàng đợi gọi lại ưu tiên
    prio = "HIGH" if o in (Outcome.VERIFIED_DENIED, Outcome.ACTION_FAILED) else _prio(ctx)
    return Decision(Action.HAND_TO_DTV, rid, text + " -> hàng đợi ĐTV", dtv_priority=prio, ticket="CHUYEN_DTV")


def _stop(ctx: _Ctx) -> Decision:
    o, ev = ctx.outcome, ctx.event
    if o is Outcome.VERIFY_FAILED:
        return Decision(Action.STOP, "S3",
                        "Xác thực danh tính sai nhiều lần -> nghi SIM bị chiếm/người lạ giữ máy; "
                        "không gọi lại số này, không gửi SMS, ĐTV xử lý ưu tiên cao",
                        block_phone=ev.phone, dtv_priority="HIGH", ticket="NGHI_CHIEM_SIM")
    if o is Outcome.DO_NOT_CALL:
        return Decision(Action.STOP, "S4",
                        "KH yêu cầu không liên hệ về case này -> bot dừng; cảnh báo sau của KH do ĐTV xử lý",
                        mark_do_not_call=True, dtv_priority=_prio(ctx), ticket="KH_TU_CHOI_LIEN_HE")
    if o is Outcome.OTHER_NUMBER_REQUESTED:
        return Decision(Action.STOP, "S5",
                        "Người nghe đòi gọi sang số khác -> bot không gọi số do người nghe cung cấp "
                        "(rủi ro lừa đảo); ĐTV kiểm tra với số đã đăng ký",
                        dtv_priority=_prio(ctx), ticket="YEU_CAU_GOI_SO_KHAC")

    # WRONG_NUMBER / INVALID_NUMBER: chặn số này, thử số còn lại nếu có
    if o is Outcome.WRONG_NUMBER:
        rid, ticket, text = "S1", "SAI_SO", "Nhầm số (người nghe không phải chủ thẻ)"
    else:
        rid, ticket, text = "S2", "SO_KHONG_TON_TAI", "Số không tồn tại/không hợp lệ"
    blocked = frozenset({ev.phone})
    phone = _choose_phone(ctx, blocked)
    if phone is not None:
        t, notes = _earliest_call(ctx, phone, ctx.now)
        if t is not None:
            return Decision(Action.SCHEDULE_CALL, rid, _join(f"{text} -> chặn số này, thử số còn lại", notes),
                            next_call_at=t, target_phone=phone, block_phone=ev.phone)
        return replace(_exhausted(ctx, rid, _join(text, notes), blocked), block_phone=ev.phone)
    sms, to = _sms(ctx, "final", blocked)
    return Decision(Action.STOP, rid, f"{text} -> chặn số này; không còn số khác, ĐTV tìm cách liên hệ",
                    block_phone=ev.phone, ticket=ticket, dtv_priority=_prio(ctx), sms_template=sms, sms_to=to)


def _retry_customer(ctx: _Ctx) -> Decision:
    o = ctx.outcome
    ocfg = ctx.cfg.outcomes[o]
    same = sum(1 for a in ctx.history if a.outcome is o)
    if ocfg.max_per_case is not None and same > ocfg.max_per_case:
        return _exhausted(ctx, "R2", f"{_label(o)} lần {same}, vượt mức {ocfg.max_per_case} lần/case")
    delay = ocfg.delays[min(same, len(ocfg.delays)) - 1]
    phone = _choose_phone(ctx)
    if phone is None:
        return _exhausted(ctx, "R3", "không còn số điện thoại hợp lệ")
    t, notes = _earliest_call(ctx, phone, ctx.now + timedelta(minutes=delay))
    if t is None:
        return _exhausted(ctx, "R4", "; ".join(notes))
    sms, to = _retry_sms(ctx, ocfg)
    head = f"{_label(o)} (lần {same}) -> gọi lại sau {delay} phút"
    if phone != ctx.event.phone:
        head += ", chuyển sang số " + ("dự phòng (Phone_SMS)" if phone == ctx.case.phone_sms else "chính")
    if sms:
        head += ", gửi SMS báo KH gọi hotline"
    return Decision(Action.SCHEDULE_CALL, "R1", _join(head, notes), next_call_at=t, target_phone=phone,
                    sms_template=sms, sms_to=to)


def _retry_system(ctx: _Ctx) -> Decision:
    o, case = ctx.outcome, ctx.case
    streak = 0
    for a in reversed(ctx.history):
        if OUTCOME_GROUP[a.outcome][0] is not Group.RETRY_SYSTEM:
            break
        streak += 1
    alert = None
    if streak > ctx.cfg.system_retry_max:
        alert = (f"Case {case.case_id}: lỗi hệ thống {streak} lần liên tiếp ({o.value}) - "
                 f"kiểm tra SIP trunk / media adapter / agent")
        if case.urgency is Urgency.P1:
            return Decision(Action.HAND_TO_DTV, "Y2",
                            f"{_label(o)} {streak} lần liên tiếp, case P1 không chờ được -> ĐTV gọi thủ công",
                            dtv_priority="HIGH", ticket="LOI_HE_THONG", ops_alert=alert)
        rid, delay = "Y3", ctx.cfg.system_backoff_minutes
    else:
        delays = ctx.cfg.outcomes[o].delays
        rid, delay = "Y1", delays[min(streak, len(delays)) - 1]
    phone = ctx.event.phone if ctx.event.phone not in case.blocked_phones else _choose_phone(ctx)
    if phone is None:
        return replace(_exhausted(ctx, rid, "không còn số điện thoại hợp lệ"), ops_alert=alert)
    t, notes = _earliest_call(ctx, phone, ctx.now + timedelta(minutes=delay))
    if t is None:
        return replace(_exhausted(ctx, rid, "; ".join(notes)), ops_alert=alert)
    head = f"{_label(o)} (lần {streak} liên tiếp, không tính lượt) -> thử lại sau {delay} phút"
    if alert:
        head += ", báo động vận hành"
    return Decision(Action.SCHEDULE_CALL, rid, _join(head, notes), next_call_at=t, target_phone=phone,
                    ops_alert=alert)


def _appointment(ctx: _Ctx) -> Decision:
    case, lvl, ev, tz = ctx.case, ctx.level, ctx.event, ctx.cfg.tz
    if case.appointments >= lvl.max_appointments:
        return Decision(Action.HAND_TO_DTV, "A2",
                        f"KH đã hẹn gọi lại {case.appointments} lần -> ĐTV liên hệ, tránh kéo dài case",
                        dtv_priority=_prio(ctx), ticket="CHUYEN_DTV")
    max_wait = timedelta(minutes=lvl.appointment_max_wait_minutes)
    notes: list[str] = []
    if ev.callback_at is None:
        want = ctx.now + timedelta(minutes=ctx.cfg.appointment_default_minutes)
        notes.append(f"KH không nói giờ -> mặc định {ctx.cfg.appointment_default_minutes} phút")
    else:
        want = ev.callback_at
    if want > ctx.now + max_wait:
        want = ctx.now + max_wait
        notes.append(f"vượt mức chờ tối đa {lvl.appointment_max_wait_minutes} phút của {case.urgency.value}"
                     + (f" (KH hẹn {_fmt(ev.callback_at, tz)})" if ev.callback_at else ""))
    phone = ev.phone if ev.phone not in case.blocked_phones else case.phone
    t, more = _earliest_call(ctx, phone, want, counts=False)
    return Decision(Action.SCHEDULE_CALL, "A1",
                    _join(f"KH hẹn gọi lại -> gọi lúc {_fmt(t, tz)} (không tính lượt)", notes + more),
                    next_call_at=t, target_phone=phone, appointment=True)


_HANDLERS = {
    Group.DONE: _done,
    Group.HUMAN: _human,
    Group.STOP: _stop,
    Group.RETRY_CUSTOMER: _retry_customer,
    Group.RETRY_SYSTEM: _retry_system,
    Group.APPOINTMENT: _appointment,
}


# =============================================================== hàm phụ

def _guard(case: Case) -> Decision | None:
    if case.status is not CaseStatus.OPEN:
        return Decision(Action.STOP, "G1", f"Case đang ở trạng thái {case.status.value} -> bot không gọi")
    if case.do_not_call:
        return Decision(Action.HAND_TO_DTV, "G2", "KH đã yêu cầu bot không liên hệ -> ĐTV xử lý",
                        dtv_priority="HIGH" if case.urgency is Urgency.P1 else "NORMAL", ticket="CHUYEN_DTV")
    return None


def _effective_outcome(event: Event, cfg: Config) -> Outcome:
    if (event.outcome in LLM_OUTCOMES and event.llm_confidence is not None
            and event.llm_confidence < cfg.llm_min_confidence):
        return Outcome.UNCLEAR
    return event.outcome


def _exhausted(ctx: _Ctx, rule_id: str, why: str, extra_blocked: frozenset[str] = frozenset()) -> Decision:
    case = ctx.case
    sms, to = _sms(ctx, "final", extra_blocked)
    reason = f"Hết lượt gọi: {why}. Tạo ticket 'không liên hệ được'"
    if sms:
        reason += ", gửi SMS cuối"
    prio = None
    if case.urgency is Urgency.P1:
        prio = "HIGH"
        reason += "; P1 -> chuyển ĐTV ưu tiên cao"
        if case.card_open:
            reason += " (thẻ còn mở: ĐTV xem xét khóa thẻ, bot không tự khóa)"
    return Decision(Action.EXHAUSTED, rule_id, reason, sms_template=sms, sms_to=to,
                    ticket="KHONG_LIEN_HE_DUOC", dtv_priority=prio)


def _choose_phone(ctx: _Ctx, extra_blocked: frozenset[str] = frozenset()) -> str | None:
    case = ctx.case
    blocked = case.blocked_phones | extra_blocked
    alt = case.phone_sms if case.phone_sms and case.phone_sms != case.phone else None
    alt_ok = (alt is not None and alt not in blocked
              and sum(1 for a in ctx.history if a.phone == alt and a.counted) < ctx.cfg.alt_max_attempts)
    if case.phone in blocked:
        return alt if alt_ok else None
    if alt_ok and _primary_fail_streak(ctx) >= ctx.cfg.alt_switch_after:
        return alt
    return case.phone


def _primary_fail_streak(ctx: _Ctx) -> int:
    n = 0
    for a in reversed(ctx.history):
        if OUTCOME_GROUP[a.outcome][0] is Group.RETRY_SYSTEM:
            continue  # lỗi hệ thống không nói gì về số điện thoại
        if a.phone != ctx.case.phone or a.outcome not in _SWITCH_TRIGGERS:
            break
        n += 1
    return n


def _retry_sms(ctx: _Ctx, ocfg: OutcomeConfig) -> tuple[str | None, str | None]:
    failures = sum(1 for a in ctx.history
                   if a.counted and OUTCOME_GROUP[a.outcome][0] is Group.RETRY_CUSTOMER)
    if ocfg.sms_before or failures == ctx.level.sms_after_failures:
        return _sms(ctx, "contact")
    return None, None


def _sms(ctx: _Ctx, kind: str, extra_blocked: frozenset[str] = frozenset()) -> tuple[str | None, str | None]:
    case = ctx.case
    to = case.phone_sms or case.phone
    if case.do_not_call or to in case.blocked_phones or to in extra_blocked:
        return None, None
    # luôn chừa 1 tin cho SMS cuối khi hết lượt
    limit = ctx.cfg.sms_max_per_case if kind == "final" else ctx.cfg.sms_max_per_case - 1
    if case.sms_sent >= limit:
        return None, None
    return kind, to


def _earliest_call(ctx: _Ctx, phone: str, not_before: datetime,
                   counts: bool = True) -> tuple[datetime | None, list[str]]:
    """Thời điểm sớm nhất >= not_before thỏa mọi ràng buộc. None = quá hạn case (hết lượt).

    counts=False (cuộc KH đã hẹn): bỏ qua giới hạn số lượt và hạn case,
    vẫn giữ khoảng cách tối thiểu và khung giờ.
    """
    cfg, lvl = ctx.cfg, ctx.level
    notes: list[str] = []
    t = max(not_before, ctx.now)

    rang = [a.started_at for a in ctx.history if a.outcome not in NOT_RINGING]
    gap = timedelta(minutes=lvl.min_gap_minutes)
    if rang and t < max(rang) + gap:
        t = max(rang) + gap
        notes.append(f"giãn đủ {lvl.min_gap_minutes} phút từ lần gọi trước")

    case_times = sorted(a.started_at for a in ctx.history if a.counted)
    phone_times = sorted([a.started_at for a in ctx.history if a.counted and a.phone == phone]
                         + list(ctx.phone_history.get(phone, ())))
    hours = int(cfg.rolling_window.total_seconds() // 3600)
    for _ in range(100):  # mỗi vòng chỉ đẩy t về sau -> hội tụ sau vài vòng
        before = t
        if counts:
            t2 = _rolling_free(case_times, t, cfg.rolling_window, lvl.max_calls_24h)
            if t2 > t:
                t = t2
                notes.append(f"case đã đủ {lvl.max_calls_24h} cuộc trong {hours}h")
            t2 = _rolling_free(phone_times, t, cfg.rolling_window, cfg.per_phone_cap_24h)
            if t2 > t:
                t = t2
                notes.append(f"số ĐT đã nhận đủ {cfg.per_phone_cap_24h} cuộc trong {hours}h (mọi case)")
        t2 = _into_window(t, lvl.window, cfg)
        if t2 > t:
            t = t2
            notes.append(f"ngoài khung giờ {lvl.window}")
        if t == before:
            break
    t = t.astimezone(cfg.tz)

    if counts:
        deadline = ctx.case.created_at + timedelta(days=lvl.max_days)
        if t >= deadline:  # chạm hạn cũng tính là hết: P1 chuyển ĐTV ngay thay vì chờ tới phút cuối
            notes.append(f"lần gọi hợp lệ sớm nhất ({_fmt(t, cfg.tz)}) đã chạm hạn {lvl.max_days:g} ngày "
                         f"của case {ctx.case.urgency.value} ({_fmt(deadline, cfg.tz)})")
            return None, _uniq(notes)
    return t, _uniq(notes)


def _rolling_free(times: list[datetime], t: datetime, window: timedelta, cap: int) -> datetime:
    """Sớm nhất >= t mà số cuộc trong (t - window, t] < cap. times đã sắp xếp."""
    recent = [x for x in times if t - window < x <= t]
    if len(recent) < cap:
        return t
    return recent[len(recent) - cap] + window


def _into_window(t: datetime, w: Window, cfg: Config) -> datetime:
    if w.full_day:
        return t
    local = t.astimezone(cfg.tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = day + timedelta(minutes=w.start), day + timedelta(minutes=w.end)
    if local < start:
        return start
    if local >= end:
        return start + timedelta(days=1)
    return t


def _prio(ctx: _Ctx) -> str:
    return "HIGH" if ctx.case.urgency is Urgency.P1 else "NORMAL"


def _label(o: Outcome) -> str:
    return _LABEL.get(o, o.value)


def _fmt(t: datetime, tz) -> str:
    return t.astimezone(tz).strftime("%d/%m %H:%M")


def _join(head: str, notes: list[str]) -> str:
    return head + ("; điều chỉnh: " + "; ".join(notes) if notes else "")


def _uniq(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _require_aware(*dts: datetime | None) -> None:
    for dt in dts:
        if dt is not None and dt.utcoffset() is None:
            raise ValueError("Mọi thời điểm phải có múi giờ (timezone-aware datetime)")
