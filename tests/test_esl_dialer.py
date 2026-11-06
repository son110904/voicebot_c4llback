"""Unit test cho ESL dialer (greenswitch version).

Test kiem tra:
  - Cause mapping logic
  - _build_result voi cac trang thai khac nhau
  - Originate error parsing
  - Full flow qua mock ESL server (gevent-based)
"""
from datetime import datetime, timezone

import gevent
import gevent.socket

import pytest

from dialer.esl_dialer import ESLDialer, ESLDialerConfig, _CallState, _CAUSE_TO_STATUS


class FakeScheduledCall:
    def __init__(self, case_id="CASE-1", phone="0900000001"):
        self.schedule_id = 1
        self.case_id = case_id
        self.phone = phone
        self.due_at = datetime.now(timezone.utc)
        self.appointment = False


class FakeDecision:
    def __init__(self, phone="0900000001"):
        self.target_phone = phone
        self.appointment = False


# ============================================================ unit tests

class TestCauseMapping:
    def test_no_answer_causes(self):
        for cause in ("NO_ANSWER", "ORIGINATOR_CANCEL", "NO_USER_RESPONSE", "ALLOTTED_TIMEOUT"):
            assert _CAUSE_TO_STATUS[cause] == "No Answer"

    def test_busy_causes(self):
        for cause in ("USER_BUSY", "NORMAL_CIRCUIT_CONGESTION", "CALL_REJECTED"):
            assert _CAUSE_TO_STATUS[cause] == "Busy"

    def test_failed_causes(self):
        for cause in ("UNALLOCATED_NUMBER", "SUBSCRIBER_ABSENT", "NORMAL_TEMPORARY_FAILURE",
                      "SERVICE_UNAVAILABLE", "DESTINATION_OUT_OF_ORDER"):
            assert _CAUSE_TO_STATUS[cause] == "Failed"

    def test_transfer_causes(self):
        assert _CAUSE_TO_STATUS["ATTENDED_TRANSFER"] == "Transferred"
        assert _CAUSE_TO_STATUS["BLIND_TRANSFER"] == "Transferred"

    def test_normal_clearing_needs_context(self):
        assert _CAUSE_TO_STATUS["NORMAL_CLEARING"] is None


class TestBuildResult:
    def setup_method(self):
        self.dialer = ESLDialer(ESLDialerConfig())

    def test_completed_call(self):
        state = _CallState(uuid="test-uuid", answered=True, hangup_cause="NORMAL_CLEARING",
                           duration_seconds=45.0,
                           channel_vars={"voicebot_status": "completed", "voicebot_verified": "true",
                                         "voicebot_llm_label": "verified_confirmed",
                                         "voicebot_llm_conf": "0.95"})
        state.done = True
        r = self.dialer._build_result("call-1", state)
        assert r.pbx_status == "Completed"
        assert r.answered_seconds == 45.0
        assert r.verified is True
        assert r.llm is not None
        assert r.llm.confidence == 0.95

    def test_incomplete_call(self):
        state = _CallState(uuid="test-uuid", answered=True, hangup_cause="NORMAL_CLEARING",
                           duration_seconds=10.0, channel_vars={})
        state.done = True
        r = self.dialer._build_result("call-2", state)
        assert r.pbx_status == "Incomplete"

    def test_no_answer(self):
        state = _CallState(uuid="test-uuid", answered=False, hangup_cause="NO_ANSWER",
                           sip_code=408)
        state.done = True
        r = self.dialer._build_result("call-3", state)
        assert r.pbx_status == "No Answer"
        assert r.sip_code == 408

    def test_busy(self):
        state = _CallState(uuid="test-uuid", answered=False, hangup_cause="USER_BUSY",
                           sip_code=486)
        state.done = True
        r = self.dialer._build_result("call-4", state)
        assert r.pbx_status == "Busy"
        assert r.sip_code == 486

    def test_transferred(self):
        state = _CallState(uuid="test-uuid", answered=True, hangup_cause="NORMAL_CLEARING",
                           channel_vars={"voicebot_status": "transferred"})
        state.done = True
        r = self.dialer._build_result("call-5", state)
        assert r.pbx_status == "Transferred"

    def test_machine_detected(self):
        state = _CallState(uuid="test-uuid", answered=True, hangup_cause="NORMAL_CLEARING",
                           duration_seconds=3.0,
                           channel_vars={"voicebot_machine": "true", "voicebot_status": "completed"})
        state.done = True
        r = self.dialer._build_result("call-6", state)
        assert r.machine_detected is True

    def test_unanswered_normal_clearing(self):
        state = _CallState(uuid="test-uuid", answered=False, hangup_cause="NORMAL_CLEARING")
        state.done = True
        r = self.dialer._build_result("call-7", state)
        assert r.pbx_status == "Failed"

    def test_llm_label_parse_error_is_graceful(self):
        state = _CallState(uuid="test-uuid", answered=True, hangup_cause="NORMAL_CLEARING",
                           channel_vars={"voicebot_status": "completed",
                                         "voicebot_llm_label": "invalid_label",
                                         "voicebot_llm_conf": "0.8"})
        state.done = True
        r = self.dialer._build_result("call-8", state)
        assert r.pbx_status == "Completed"
        assert r.llm is None  # parse error -> None, not crash


class TestOriginateErrorParse:
    def test_known_cause(self):
        assert ESLDialer._parse_originate_error("-ERR USER_BUSY\n") == "USER_BUSY"
        assert ESLDialer._parse_originate_error("-ERR NO_ANSWER\n") == "NO_ANSWER"
        assert ESLDialer._parse_originate_error("-ERR UNALLOCATED_NUMBER\n") == "UNALLOCATED_NUMBER"

    def test_unknown_cause(self):
        result = ESLDialer._parse_originate_error("-ERR SOME_WEIRD_ERROR\n")
        assert result == "SOME_WEIRD_ERROR"

    def test_empty(self):
        result = ESLDialer._parse_originate_error("-ERR ")
        assert result == "NORMAL_TEMPORARY_FAILURE"


# ============================================================ mock ESL server (gevent)

def _read_until_blank(f):
    """Read ESL command lines until blank line."""
    while True:
        line = f.readline()
        if not line or line in (b"\n", b"\r\n"):
            break


def _send_reply(sock, text):
    sock.sendall(f"Content-Type: command/reply\nReply-Text: {text}\n\n".encode())


def _mock_esl_handler(sock, scenario="completed"):
    """Mo phong FreeSWITCH ESL inbound — protocol tuong thich voi greenswitch."""
    f = sock.makefile('rb')

    try:
        # Auth
        sock.sendall(b"Content-Type: auth/request\n\n")
        _read_until_blank(f)  # auth ClueCon
        _send_reply(sock, "+OK accepted")

        # Event subscription
        _read_until_blank(f)  # event plain ...
        _send_reply(sock, "+OK event listener enabled plain")

        # api originate
        _read_until_blank(f)
        gevent.sleep(0.05)

        if scenario == "no_answer":
            body = "-ERR NO_ANSWER\n"
            sock.sendall(f"Content-Type: api/response\nContent-Length: {len(body)}\n\n{body}".encode())
            # Handle exit from stop()
            _read_until_blank(f)
            _send_reply(sock, "+OK bye")
            return

        # api/response +OK
        channel_uuid = "ch-uuid-abc123"
        body = f"+OK {channel_uuid}\n"
        sock.sendall(f"Content-Type: api/response\nContent-Length: {len(body)}\n\n{body}".encode())

        # Filter
        _read_until_blank(f)  # filter Unique-ID ...
        _send_reply(sock, "+OK filter added")

        gevent.sleep(0.1)

        # CHANNEL_HANGUP_COMPLETE
        hangup_vars = {
            "Event-Name": "CHANNEL_HANGUP_COMPLETE",
            "Unique-ID": channel_uuid,
            "Hangup-Cause": "NORMAL_CLEARING",
            "variable_sip_term_status": "200",
            "variable_billsec": "42",
        }
        if scenario == "completed":
            hangup_vars.update({
                "variable_voicebot_status": "completed",
                "variable_voicebot_verified": "true",
                "variable_voicebot_llm_label": "verified_confirmed",
                "variable_voicebot_llm_conf": "0.92",
            })
        hangup_body = "\n".join(f"{k}: {v}" for k, v in hangup_vars.items()) + "\n"
        sock.sendall(
            f"Content-Type: text/event-plain\nContent-Length: {len(hangup_body)}\n\n{hangup_body}".encode())

        # Handle exit from stop()
        _read_until_blank(f)
        _send_reply(sock, "+OK bye")

    except Exception:
        pass
    finally:
        try:
            f.close()
            sock.close()
        except Exception:
            pass


def _run_mock_server(scenario):
    """Start mock ESL server, return (port, handler_greenlet, server_socket)."""
    server_sock = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_STREAM)
    server_sock.setsockopt(gevent.socket.SOL_SOCKET, gevent.socket.SO_REUSEADDR, 1)
    server_sock.bind(('127.0.0.1', 0))
    port = server_sock.getsockname()[1]
    server_sock.listen(1)

    def accept():
        try:
            conn, _ = server_sock.accept()
            _mock_esl_handler(conn, scenario)
        except Exception:
            pass

    handler = gevent.spawn(accept)
    return port, handler, server_sock


# ============================================================ integration tests

def test_full_dial_completed():
    """Test full flow: connect -> auth -> originate -> hangup(completed)."""
    port, handler, server_sock = _run_mock_server("completed")
    try:
        cfg = ESLDialerConfig(esl_host="127.0.0.1", esl_port=port, use_loopback=True)
        dialer = ESLDialer(cfg)
        result = dialer(FakeScheduledCall(), FakeDecision())

        assert result.pbx_status == "Completed"
        assert result.answered_seconds == 42.0
        assert result.verified is True
        assert result.llm is not None
        assert result.llm.confidence == 0.92
    finally:
        handler.join(timeout=5)
        server_sock.close()


def test_full_dial_no_answer():
    """Test originate returns NO_ANSWER."""
    port, handler, server_sock = _run_mock_server("no_answer")
    try:
        cfg = ESLDialerConfig(esl_host="127.0.0.1", esl_port=port, use_loopback=True)
        dialer = ESLDialer(cfg)
        result = dialer(FakeScheduledCall(), FakeDecision())

        assert result.pbx_status == "No Answer"
    finally:
        handler.join(timeout=5)
        server_sock.close()


def test_full_dial_incomplete():
    """Test answered call without voicebot_status -> Incomplete."""
    port, handler, server_sock = _run_mock_server("incomplete")
    try:
        cfg = ESLDialerConfig(esl_host="127.0.0.1", esl_port=port, use_loopback=True)
        dialer = ESLDialer(cfg)
        result = dialer(FakeScheduledCall(), FakeDecision())

        assert result.pbx_status == "Incomplete"
    finally:
        handler.join(timeout=5)
        server_sock.close()


def test_connection_refused():
    """Test khi FreeSWITCH khong chay."""
    cfg = ESLDialerConfig(esl_host="127.0.0.1", esl_port=19999)
    dialer = ESLDialer(cfg)
    result = dialer(FakeScheduledCall(), FakeDecision())
    assert result.pbx_status == "Failed"
