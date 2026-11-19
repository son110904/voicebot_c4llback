"""StreamingDialer — Controller cho pipeline streaming (sơ đồ của Hiếu).

Khác ESLDialer (chạy kịch bản thoại qua ESL outbound). Ở mô hình streaming, hội thoại nằm hẳn trong
pipeline của Hiếu (FreeSWITCH mod_audio_fork -> Media Adapter -> Gateway -> STT -> agent -> TTS).
Controller chỉ còn lo 3 việc:

    1. Điểm bàn giao 1 — originate: sinh call_id, POST ngữ cảnh sang agent, quay số qua ESL.
    2. Vòng đời cuộc gọi: chờ kết quả, cúp máy (uuid_kill).
    3. Điểm bàn giao 2 — nhận kết quả: agent POST về Flask -> ghi call_outcomes -> dialer đọc.

Lưới an toàn (vì không chắc agent luôn gọi ngược): dialer chốt theo cái nào tới trước -
kết quả agent POST, sự kiện hangup từ ESL, hay hết call_timeout - nên case không bao giờ kẹt.

Phần nghiệp vụ (orchestration, map nhãn, lưới an toàn) là hàm thuần, test được không cần gevent/DB.
Phần chạm ESL thật (GreenswitchEsl) và DB/HTTP được tách sau protocol để inject fake khi test.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from callback.models import Outcome
from db.service import LlmLabel
from scheduler import DialResult, new_call_id

log = logging.getLogger("streaming_dialer")


# ---------------------------------------------------------------- map nhãn agent -> Outcome

# Nhãn agent (Hiếu) trả về -> Outcome rule engine hiểu. Nhãn lạ / rỗng -> UNCLEAR (an toàn, ra ĐTV).
# Hiếu đổi tên nhãn thì chỉ sửa bảng này, rule engine và DB không đụng tới.
AGENT_OUTCOME_MAP = {
    "verified_confirmed": Outcome.VERIFIED_CONFIRMED,
    "verified_denied": Outcome.VERIFIED_DENIED,
    "wrong_number": Outcome.WRONG_NUMBER,
    "relative_answered": Outcome.RELATIVE_ANSWERED,
    "callback": Outcome.CALLBACK_REQUESTED,
    "callback_requested": Outcome.CALLBACK_REQUESTED,
    "verify_failed": Outcome.VERIFY_FAILED,
    "angry": Outcome.ANGRY,
    "do_not_call": Outcome.DO_NOT_CALL,
    "other_number_requested": Outcome.OTHER_NUMBER_REQUESTED,
    "scam_suspected_hangup": Outcome.SCAM_SUSPECTED_HANGUP,
    "need_human": Outcome.NEED_HUMAN,
    "customer_unsure": Outcome.CUSTOMER_UNSURE,
    "transferred": Outcome.TRANSFERRED,
}


def map_label(label: str | None) -> Outcome:
    if not label:
        return Outcome.UNCLEAR
    return AGENT_OUTCOME_MAP.get(str(label).strip().lower(), Outcome.UNCLEAR)


# ---------------------------------------------------------------- config + kiểu trả về

@dataclass(frozen=True)
class StreamingDialerConfig:
    esl_host: str = "127.0.0.1"
    esl_port: int = 8021
    esl_password: str = "ClueCon"
    sip_gateway: str = "trunk"
    voicebot_ext: str = "9999"       # extension dialplan: answer + audio_fork tới Adapter (trong container FreeSWITCH)
    caller_id: str = "1900558868"    # khớp hotline trong campaign_rules.yaml
    ring_timeout: int = 30
    call_timeout: int = 300          # tối đa một cuộc kể cả hội thoại
    poll_interval_s: float = 0.5     # nhịp đọc call_outcomes + kiểm tra hangup
    hangup_grace_s: float = 3.0      # KH cúp trước -> chờ thêm chừng này cho outcome POST muộn
    agent_context_url: str = ""      # ví dụ http://agent-agent:8000/calls  (POST <url>/<call_id>/context)


@dataclass(frozen=True)
class OriginateResult:
    answered: bool
    uuid: str | None = None
    status: str = "Failed"           # khi answered=False: No Answer / Busy / Failed
    sip_code: int | None = None


# ---------------------------------------------------------------- protocol cho phần chạm hệ thống

class EslClient(Protocol):
    def originate(self, call_id: str, phone: str, cfg: StreamingDialerConfig) -> OriginateResult: ...
    def is_hungup(self, uuid: str) -> tuple[bool, float | None, int | None]: ...  # hungup?, billsec, sip_code
    def kill(self, uuid: str) -> None: ...


class OutcomeStore(Protocol):
    def read(self, call_id: str) -> dict | None: ...


class ContextSink(Protocol):
    def register(self, call_id: str, context: dict) -> None: ...


# ---------------------------------------------------------------- orchestration (hàm thuần, test được)

class StreamingDialer:
    """Dùng trong scheduler.Scheduler(dialer=StreamingDialer(...)). Thỏa protocol Dialer."""

    def __init__(self, cfg: StreamingDialerConfig, esl: EslClient, outcome_store: OutcomeStore,
                 context_sink: ContextSink, context_provider: Callable[[str], dict],
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.esl = esl
        self.outcome_store = outcome_store
        self.context_sink = context_sink
        self.context_provider = context_provider
        self._now = clock
        self._sleep = sleep

    def __call__(self, call, decision) -> DialResult:
        call_id = new_call_id()
        phone = decision.target_phone or call.phone

        # Điểm bàn giao 1: đăng ký ngữ cảnh với agent (lỗi thì vẫn gọi; agent có thể hỏi lại / fallback)
        try:
            ctx = dict(self.context_provider(call.case_id))
        except Exception:
            log.exception("Lấy ngữ cảnh lỗi case=%s", call.case_id)
            ctx = {"case_id": call.case_id}
        ctx["call_id"] = call_id
        try:
            self.context_sink.register(call_id, ctx)
        except Exception:
            log.exception("POST ngữ cảnh lỗi call_id=%s -> vẫn originate", call_id)

        res = self.esl.originate(call_id, phone, self.cfg)
        if not res.answered:
            # Không nghe / bận / lỗi: không tốn pipeline, map thẳng
            return DialResult(call_id, res.status, sip_code=res.sip_code)

        return self._wait_result(call_id, res.uuid)

    def _wait_result(self, call_id: str, uuid: str) -> DialResult:
        deadline = self._now() + self.cfg.call_timeout
        while self._now() < deadline:
            row = self.outcome_store.read(call_id)
            if row is not None:                          # agent đã báo kết quả
                self._safe_kill(uuid)                    # Controller cúp máy (câu 3)
                return self._from_outcome(call_id, row)

            hung, billsec, _sip = self.esl.is_hungup(uuid)
            if hung:                                     # KH tự cúp trước khi agent báo
                row = self._await_grace(call_id)         # chờ outcome POST muộn vài giây
                if row is not None:
                    return self._from_outcome(call_id, row)
                return self._lost(call_id, billsec)

            self._sleep(self.cfg.poll_interval_s)

        self._safe_kill(uuid)
        log.warning("call_timeout call_id=%s -> system_error", call_id)
        return DialResult(call_id, "Failed")             # không có mã SIP -> system_error (không tính lượt)

    def _await_grace(self, call_id: str) -> dict | None:
        end = self._now() + self.cfg.hangup_grace_s
        while self._now() < end:
            row = self.outcome_store.read(call_id)
            if row is not None:
                return row
            self._sleep(self.cfg.poll_interval_s)
        return self.outcome_store.read(call_id)

    def _from_outcome(self, call_id: str, row: dict) -> DialResult:
        status = str(row.get("status", "completed")).lower()
        pbx = {"completed": "Completed", "incomplete": "Incomplete",
               "transferred": "Transferred"}.get(status, "Completed")
        llm = LlmLabel(
            label=map_label(row.get("label")),
            confidence=_as_float(row.get("confidence")),
            callback_at=_parse_dt(row.get("callback_at")),
            model="agent", prompt_version=row.get("prompt_version"),
            raw_output=json.dumps(row, ensure_ascii=False),
        )
        return DialResult(call_id, pbx, answered_seconds=_as_float(row.get("answered_seconds"), None),
                          verified=bool(row.get("verified")), llm=llm)

    def _lost(self, call_id: str, billsec: float | None) -> DialResult:
        """KH cúp trước, agent chưa báo gì. Trả 'Completed' không kèm nhãn -> rule engine map:
        <5 giây = short_hangup (gọi lại + ZNS), còn lại = unclear -> ĐTV nghe lại ghi âm.
        Không đoán là đã xong, vì có thể hội thoại dở dang."""
        log.info("Mất kết quả call_id=%s (KH cúp trước, agent chưa báo), billsec=%s", call_id, billsec)
        return DialResult(call_id, "Completed", answered_seconds=billsec)

    def _safe_kill(self, uuid: str) -> None:
        try:
            self.esl.kill(uuid)
        except Exception:
            log.exception("uuid_kill lỗi uuid=%s", uuid)


def _as_float(v, default: float = 0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


# ---------------------------------------------------------------- phần chạm hệ thống thật

class DbOutcomeStore:
    """Đọc call_outcomes. Phải dùng CONNECTION RIÊNG (không chung với svc), và commit sau mỗi lần đọc
    để kết thúc transaction -> lần đọc sau mới thấy row Flask (tiến trình khác) vừa commit."""

    def __init__(self, conn):
        self.conn = conn

    def read(self, call_id: str) -> dict | None:
        from db import repo
        try:
            return repo.read_call_outcome(self.conn, call_id)
        finally:
            self.conn.commit()


class HttpContextSink:
    """POST ngữ cảnh sang agent: POST <base>/<call_id>/context."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def register(self, call_id: str, context: dict) -> None:
        import urllib.request
        if not self.base:
            log.warning("agent_context_url trống -> bỏ qua POST ngữ cảnh call_id=%s", call_id)
            return
        body = json.dumps(context, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/{call_id}/context", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()


class GreenswitchEsl:
    """ESL thật qua greenswitch. CHƯA chạy ở máy dev (thiếu gevent) — cần test trên server DMZ.

    Một instance xử lý MỘT cuộc tại một thời điểm (giữ 1 connection). Gọi song song -> mỗi worker một
    instance. audio_fork do dialplan trong container FreeSWITCH bật (câu 4), dialer không gọi uuid_audio_fork;
    dialplan phải nhét ${voicebot_call_id} vào metadata của fork để Gateway khớp luồng audio với ngữ cảnh.
    """

    _CAUSE_TO_STATUS = {
        "NO_ANSWER": "No Answer", "ORIGINATOR_CANCEL": "No Answer", "NO_USER_RESPONSE": "No Answer",
        "ALLOTTED_TIMEOUT": "No Answer", "USER_BUSY": "Busy", "NORMAL_CIRCUIT_CONGESTION": "Busy",
        "CALL_REJECTED": "Busy",
    }

    def __init__(self, cfg: StreamingDialerConfig):
        self.cfg = cfg
        self._esl = None
        self._state: dict = {}

    def originate(self, call_id: str, phone: str, cfg: StreamingDialerConfig) -> OriginateResult:
        from greenswitch import InboundESL
        esl = InboundESL(cfg.esl_host, cfg.esl_port, cfg.esl_password, timeout=10)
        esl.connect()
        esl.send("event plain CHANNEL_HANGUP_COMPLETE")
        state = {"hung": False, "billsec": None, "sip": None, "uuid": None}

        def on_hangup(ev):
            if state["uuid"] and ev.headers.get("Unique-ID") == state["uuid"]:
                state["hung"] = True
                b = ev.headers.get("variable_billsec", "")
                state["billsec"] = float(b) if b.isdigit() else None
                s = ev.headers.get("variable_sip_term_status", "")
                state["sip"] = int(s) if s.isdigit() else None

        esl.register_handle("CHANNEL_HANGUP_COMPLETE", on_hangup)
        cvars = (f"origination_caller_id_number={cfg.caller_id},originate_timeout={cfg.ring_timeout},"
                 f"ignore_early_media=true,voicebot_call_id={call_id}")
        resp = esl.send(f"api originate {{{cvars}}}sofia/gateway/{cfg.sip_gateway}/{phone} "
                        f"{cfg.voicebot_ext} XML default")
        data = (getattr(resp, "data", "") or "").strip()
        if not data or data.startswith("-ERR"):
            cause = data.replace("-ERR", "").strip().split()[0] if data else "NO_ANSWER"
            try:
                esl.stop()
            except Exception:
                pass
            return OriginateResult(False, status=self._CAUSE_TO_STATUS.get(cause, "Failed"))
        uuid = data.replace("+OK ", "").strip()
        state["uuid"] = uuid
        self._esl, self._state = esl, state
        return OriginateResult(True, uuid=uuid)

    def is_hungup(self, uuid: str) -> tuple[bool, float | None, int | None]:
        s = self._state
        return s.get("hung", False), s.get("billsec"), s.get("sip")

    def kill(self, uuid: str) -> None:
        try:
            if self._esl is not None:
                self._esl.send(f"api uuid_kill {uuid}")
        finally:
            try:
                self._esl.stop()
            except Exception:
                pass


def make_streaming_dialer(svc, cfg: StreamingDialerConfig | None = None, store_conn=None):
    """Lắp ráp dialer thật cho worker. Dùng gevent.sleep để vòng lặp nhường cho ESL xử lý sự kiện."""
    import gevent
    from db import connect
    cfg = cfg or StreamingDialerConfig(agent_context_url=os.environ.get("AGENT_CONTEXT_URL", ""))
    store_conn = store_conn or connect(os.environ.get("CALLBACK_DB_URL", ""))
    return StreamingDialer(cfg, GreenswitchEsl(cfg), DbOutcomeStore(store_conn),
                           HttpContextSink(cfg.agent_context_url), svc.call_context,
                           clock=time.monotonic, sleep=gevent.sleep)
