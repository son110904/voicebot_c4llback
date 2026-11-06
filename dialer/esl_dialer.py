"""
Kien truc:
    Scheduler -> ESLDialer() -> FreeSWITCH ESL inbound port 8021
        -> originate sofia/gateway/{trunk}/{phone} {ext} XML default
        -> CHANNEL_ANSWER -> voicebot ESL outbound xu ly hoi thoai
        -> CHANNEL_HANGUP_COMPLETE -> doc hangup cause + channel vars
        -> DialResult(pbx_status, sip_code, answered_seconds, ...)

Channel variables voicebot ESL outbound server can set:
    voicebot_status      completed | incomplete | transferred
    voicebot_verified    true | false
    voicebot_llm_label   outcome label (e.g. verified_confirmed)
    voicebot_llm_conf    float confidence
    voicebot_callback_at ISO datetime (KH hen goi lai)
    voicebot_machine     true (AMD detected voicemail)

Su dung greenswitch library (gevent-based) thay cho raw TCP.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import gevent
import gevent.event
from greenswitch import InboundESL
from greenswitch.esl import NotConnectedError

from scheduler import DialResult, new_call_id

log = logging.getLogger("dialer")

# FreeSWITCH hangup cause -> docs call_status
# None = need more context (answered or not, voicebot_status)
_CAUSE_TO_STATUS: dict[str, str | None] = {
    "NORMAL_CLEARING": None,
    "NORMAL_UNSPECIFIED": None,
    # No Answer
    "NO_ANSWER": "No Answer",
    "ORIGINATOR_CANCEL": "No Answer",
    "NO_USER_RESPONSE": "No Answer",
    "ALLOTTED_TIMEOUT": "No Answer",
    # Busy
    "USER_BUSY": "Busy",
    "NORMAL_CIRCUIT_CONGESTION": "Busy",
    "CALL_REJECTED": "Busy",
    # Failed (SIP code cua tung loai deu khac nhau, pbx.py se map chi tiet)
    "UNALLOCATED_NUMBER": "Failed",
    "NO_ROUTE_DESTINATION": "Failed",
    "SUBSCRIBER_ABSENT": "Failed",
    "NORMAL_TEMPORARY_FAILURE": "Failed",
    "SERVICE_UNAVAILABLE": "Failed",
    "RECOVERY_ON_TIMER_EXPIRE": "Failed",
    "NETWORK_OUT_OF_ORDER": "Failed",
    "DESTINATION_OUT_OF_ORDER": "Failed",
    "INVALID_NUMBER_FORMAT": "Failed",
    "FACILITY_REJECTED": "Failed",
    "BEARERCAPABILITY_NOTAVAIL": "Failed",
    "EXCHANGE_ROUTING_ERROR": "Failed",
    # Transferred (FreeSWITCH sets these on attended/blind transfer)
    "ATTENDED_TRANSFER": "Transferred",
    "BLIND_TRANSFER": "Transferred",
}


@dataclass(frozen=True)
class ESLDialerConfig:
    esl_host: str = "127.0.0.1"
    esl_port: int = 8021
    esl_password: str = "ClueCon"
    sip_gateway: str = "trunk"          # ten gateway trong sip_profiles/external
    voicebot_ext: str = "9999"          # extension voicebot trong dialplan
    caller_id: str = "19001560"         # so hien thi khi goi ra
    ring_timeout: int = 30              # giay cho chuong truoc khi bo cuoc
    call_timeout: int = 300             # giay toi da 1 cuoc goi (bao gom hoi thoai)
    # Cho test local: dung loopback thay vi SIP gateway
    use_loopback: bool = False
    loopback_context: str = "default"


@dataclass
class _CallState:
    uuid: str
    answered: bool = False
    answered_at: datetime | None = None
    hangup_cause: str = ""
    sip_code: int | None = None
    duration_seconds: float | None = None
    channel_vars: dict[str, str] = field(default_factory=dict)
    done: bool = False
    error: str | None = None


class ESLDialer:
    """Dialer that se dung trong scheduler.Scheduler(dialer=ESLDialer(...)).

    Implements scheduler.Dialer protocol: __call__(call, decision) -> DialResult.
    Su dung greenswitch InboundESL (gevent-based) de ket noi ESL.
    """

    def __init__(self, cfg: ESLDialerConfig | None = None):
        self.cfg = cfg or ESLDialerConfig()

    def __call__(self, call, decision) -> DialResult:
        """Sync entry point cho Scheduler. Goi ra va cho ket qua."""
        return gevent.spawn(self._dial, call, decision).get()

    def _dial(self, call, decision) -> DialResult:
        call_id = new_call_id()
        phone = decision.target_phone or call.phone
        cfg = self.cfg

        try:
            esl = InboundESL(cfg.esl_host, cfg.esl_port, cfg.esl_password, timeout=10)
            esl.connect()
        except Exception as e:
            log.error("Khong ket noi duoc ESL %s:%s: %s", cfg.esl_host, cfg.esl_port, e)
            return DialResult(call_id, "Failed")

        try:
            return self._do_call(esl, call_id, phone, cfg)
        except Exception as e:
            log.exception("Loi trong cuoc goi %s -> %s", call_id, phone)
            return DialResult(call_id, "Failed")
        finally:
            try:
                with gevent.Timeout(5):
                    esl.stop()
            except Exception:
                try:
                    esl.sock.close()
                except Exception:
                    pass

    def _do_call(self, esl: InboundESL, call_id: str, phone: str,
                 cfg: ESLDialerConfig) -> DialResult:
        # Step 1: Subscribe events
        esl.send('event plain CHANNEL_HANGUP_COMPLETE')

        # Step 2: Set up hangup tracking
        state = _CallState(uuid="")
        hangup_result = gevent.event.AsyncResult()

        def on_hangup(event):
            if not hangup_result.ready():
                hangup_result.set(event)

        esl.register_handle('CHANNEL_HANGUP_COMPLETE', on_hangup)

        # Step 3: Build originate string
        call_vars = (
            f"origination_caller_id_number={cfg.caller_id},"
            f"originate_timeout={cfg.ring_timeout},"
            f"ignore_early_media=true,"
            f"voicebot_call_id={call_id}"
        )
        if cfg.use_loopback:
            dial_str = f"loopback/{phone}/{cfg.loopback_context}"
        else:
            dial_str = f"sofia/gateway/{cfg.sip_gateway}/{phone}"

        originate_cmd = (
            f"api originate "
            f"{{{call_vars}}}"
            f"{dial_str} "
            f"{cfg.voicebot_ext} XML default"
        )
        log.info("Originate: %s -> ext %s (call_id=%s)", phone, cfg.voicebot_ext, call_id)

        # Step 4: Send originate (api blocks until call connects or fails)
        try:
            with gevent.Timeout(cfg.ring_timeout + 15):
                resp = esl.send(originate_cmd)
        except gevent.Timeout:
            log.warning("Originate timeout cho call_id=%s", call_id)
            return DialResult(call_id, "Failed")
        except NotConnectedError as e:
            log.error("ESL disconnect during originate: %s", e)
            return DialResult(call_id, "Failed")

        result_text = (getattr(resp, 'data', '') or '').strip()

        if not result_text or result_text.startswith('-ERR'):
            cause = self._parse_originate_error(result_text)
            log.info("Originate loi: %s -> %s", phone, cause)
            status = _CAUSE_TO_STATUS.get(cause, "Failed") or "Failed"
            return DialResult(call_id, status)

        # Step 5: api originate thanh cong = call da answered
        channel_uuid = result_text.replace('+OK ', '').strip()
        state.uuid = channel_uuid
        state.answered = True
        state.answered_at = datetime.now(timezone.utc)
        log.info("Channel UUID: %s (answered)", channel_uuid)

        # Step 6: Filter events for this UUID
        esl.send(f'filter Unique-ID {channel_uuid}')

        # Step 7: Wait for hangup
        try:
            hangup_event = hangup_result.get(timeout=cfg.call_timeout)
        except gevent.Timeout:
            log.warning("Call timeout cho %s", call_id)
            return self._build_result(call_id, state)

        # Step 8: Process hangup event
        state.done = True
        state.hangup_cause = hangup_event.headers.get('Hangup-Cause', 'NORMAL_CLEARING')

        sip_str = hangup_event.headers.get('variable_sip_term_status', '')
        if sip_str.isdigit():
            state.sip_code = int(sip_str)

        dur_str = hangup_event.headers.get(
            'variable_billsec',
            hangup_event.headers.get('variable_duration', ''))
        if dur_str:
            try:
                state.duration_seconds = float(dur_str)
            except ValueError:
                pass

        for var in ("voicebot_status", "voicebot_verified", "voicebot_llm_label",
                    "voicebot_llm_conf", "voicebot_callback_at", "voicebot_machine"):
            val = hangup_event.headers.get(f"variable_{var}", "")
            if val:
                state.channel_vars[var] = val

        log.info("Hangup: cause=%s sip=%s dur=%s vars=%s",
                 state.hangup_cause, state.sip_code, state.duration_seconds,
                 state.channel_vars)

        return self._build_result(call_id, state)

    def _build_result(self, call_id: str, state: _CallState) -> DialResult:
        cause = state.hangup_cause
        cvars = state.channel_vars
        voicebot_status = cvars.get("voicebot_status", "")

        # Fallback: loopback calls may not fire CHANNEL_ANSWER on the monitored UUID,
        # but if we have voicebot vars or billsec > 0, the call was answered.
        answered = state.answered or bool(voicebot_status) or (
            state.duration_seconds is not None and state.duration_seconds > 0
        )

        # Determine pbx_status
        mapped = _CAUSE_TO_STATUS.get(cause)
        if mapped is not None:
            pbx_status = mapped
        elif answered:
            if voicebot_status == "transferred":
                pbx_status = "Transferred"
            elif voicebot_status == "completed":
                pbx_status = "Completed"
            else:
                pbx_status = "Incomplete"
        else:
            pbx_status = "Failed"

        machine = cvars.get("voicebot_machine") == "true"
        verified = cvars.get("voicebot_verified") == "true"

        # LLM label from voicebot (if set)
        llm = None
        llm_label = cvars.get("voicebot_llm_label")
        llm_conf = cvars.get("voicebot_llm_conf")
        if llm_label and llm_conf:
            from db.service import LlmLabel
            from callback.models import Outcome
            try:
                callback_at = None
                cb_str = cvars.get("voicebot_callback_at")
                if cb_str:
                    callback_at = datetime.fromisoformat(cb_str)
                llm = LlmLabel(
                    label=Outcome(llm_label),
                    confidence=float(llm_conf),
                    callback_at=callback_at,
                )
            except (ValueError, KeyError) as e:
                log.warning("LLM label parse loi: %s", e)

        return DialResult(
            call_id=call_id,
            pbx_status=pbx_status,
            sip_code=state.sip_code,
            answered_seconds=state.duration_seconds,
            machine_detected=machine,
            verified=verified,
            llm=llm,
        )

    @staticmethod
    def _parse_originate_error(result: str) -> str:
        # "-ERR NORMAL_TEMPORARY_FAILURE\n" or "-ERR [cause] message"
        text = result.replace("-ERR", "").strip()
        for cause in _CAUSE_TO_STATUS:
            if cause in text:
                return cause
        return text.split()[0] if text else "NORMAL_TEMPORARY_FAILURE"
