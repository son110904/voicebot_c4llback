"""Demo: ESL inbound dialer — chay nhieu kich ban qua FreeSWITCH loopback.

Kich ban:
  1. Verified    (9992) — KH xac nhan giao dich
  2. Incomplete  (9993) — KH gac may som
  3. Denied      (9994) — KH tu choi
  4. Callback    (9985) — KH hen goi lai
  5. No answer   (8888) — extension khong ton tai

Yeu cau: docker compose up -d (FreeSWITCH + ESL outbound server)
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")
from dialer.esl_dialer import ESLDialer, ESLDialerConfig
from scheduler import DialResult


class DemoCall:
    def __init__(self, case_id, phone):
        self.schedule_id = 1
        self.case_id = case_id
        self.phone = phone
        self.appointment = False


class DemoDecision:
    def __init__(self, phone):
        self.target_phone = phone
        self.appointment = False


SCENARIOS = [
    ("Verified (KH xac nhan GD)", "CASE-001", "9998", "9992"),
    ("Incomplete (KH gac may som)", "CASE-002", "9998", "9993"),
    ("Denied (KH tu choi)", "CASE-003", "9998", "9994"),
    ("Callback (KH hen goi lai)", "CASE-004", "9998", "9985"),
    ("No answer (ext khong ton tai)", "CASE-005", "8888", "9992"),
]


def run_scenario(dialer_cfg_base, name, case_id, a_leg_ext, voicebot_ext):
    cfg = ESLDialerConfig(
        esl_host=dialer_cfg_base["esl_host"],
        esl_port=dialer_cfg_base["esl_port"],
        esl_password=dialer_cfg_base["esl_password"],
        voicebot_ext=voicebot_ext,
        caller_id="19001560",
        ring_timeout=10,
        call_timeout=30,
        use_loopback=True,
        loopback_context="default",
    )

    dialer = ESLDialer(cfg)
    call = DemoCall(case_id, a_leg_ext)
    decision = DemoDecision(a_leg_ext)

    start = time.time()
    result: DialResult = dialer(call, decision)
    elapsed = time.time() - start

    return result, elapsed


def print_result(name, result, elapsed):
    status_icon = {
        "Completed": "[OK]",
        "Incomplete": "[..] ",
        "Failed": "[XX]",
        "No Answer": "[--]",
        "Busy": "[!!]",
        "Transferred": "[->]",
    }.get(result.pbx_status, "[??]")

    llm_str = "—"
    if result.llm:
        llm_str = f"{result.llm.label.value} ({result.llm.confidence:.0%})"
        if result.llm.callback_at:
            llm_str += f" hen {result.llm.callback_at.strftime('%H:%M %d/%m')}"

    print(f"  {status_icon} {name}")
    print(f"       Status: {result.pbx_status}  |  Duration: {result.answered_seconds or 0:.0f}s  |  Verified: {result.verified}")
    print(f"       LLM:    {llm_str}")
    print(f"       Call:   {result.call_id}  ({elapsed:.1f}s)")
    print()


def main():
    print()
    print("=" * 64)
    print("   DEMO: ESL Dialer — Multi-Scenario Test")
    print("=" * 64)
    print()

    base_cfg = {
        "esl_host": "127.0.0.1",
        "esl_port": 8021,
        "esl_password": "ClueCon",
    }

    results = []
    for i, (name, case_id, a_leg, voicebot) in enumerate(SCENARIOS, 1):
        print(f"  [{i}/{len(SCENARIOS)}] {name}...")
        try:
            result, elapsed = run_scenario(base_cfg, name, case_id, a_leg, voicebot)
            results.append((name, result, elapsed))
        except Exception as e:
            print(f"       LOI: {e}")
            print()
            results.append((name, None, 0))

    print()
    print("=" * 64)
    print("   KET QUA")
    print("=" * 64)
    print()
    for name, result, elapsed in results:
        if result:
            print_result(name, result, elapsed)
        else:
            print(f"  [!!] {name} — LOI KET NOI")
            print()

    # Summary
    passed = sum(1 for _, r, _ in results if r is not None)
    print("-" * 64)
    print(f"  Tong: {passed}/{len(SCENARIOS)} kich ban chay thanh cong")

    # Validate expected outcomes
    checks = []
    for name, result, _ in results:
        if result is None:
            checks.append((name, False, "connection error"))
            continue
        if "Verified" in name:
            ok = result.pbx_status == "Completed" and result.verified
            checks.append((name, ok, f"expected Completed+verified, got {result.pbx_status}+verified={result.verified}"))
        elif "Incomplete" in name:
            ok = result.pbx_status == "Incomplete" or (result.pbx_status == "Completed" and not result.verified)
            checks.append((name, ok, f"expected Incomplete, got {result.pbx_status}"))
        elif "Denied" in name:
            ok = result.pbx_status == "Completed" and not result.verified
            llm_ok = result.llm and result.llm.label.value == "verified_denied"
            checks.append((name, ok and llm_ok, f"expected Completed+verified_denied, got {result.pbx_status}+llm={result.llm}"))
        elif "Callback" in name:
            ok = result.pbx_status == "Completed"
            cb_ok = result.llm and result.llm.callback_at is not None
            checks.append((name, ok and cb_ok, f"expected callback_at, got {result.llm}"))
        elif "No answer" in name:
            ok = result.pbx_status in ("Failed", "No Answer")
            checks.append((name, ok, f"expected Failed/No Answer, got {result.pbx_status}"))

    print()
    all_ok = True
    for name, ok, detail in checks:
        icon = "PASS" if ok else "FAIL"
        print(f"  [{icon}] {name}")
        if not ok:
            print(f"         {detail}")
            all_ok = False

    print()
    if all_ok:
        print("  >>> Tat ca kich ban PASS! <<<")
    else:
        print("  >>> Co kich ban FAIL — xem chi tiet o tren <<<")
    print()


if __name__ == "__main__":
    main()
