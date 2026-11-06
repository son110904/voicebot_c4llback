"""Gán nhãn transcript bằng LLM.

Luồng:
    transcript thô -> che PII -> prompt + schema -> gọi LLM -> parse JSON -> validate -> LlmLabel
    bất kỳ lỗi/thấp confidence/nhãn lạ -> UNCLEAR (rule engine sẽ chuyển ĐTV).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Protocol

from callback.models import LLM_OUTCOMES, Outcome
from callback.time_parser import parse_callback_time
from db.service import LlmLabel
from .pii import mask_pii

PROMPT_VERSION = "v1"

# Tập nhãn LLM được phép trả về (không bao gồm UNCLEAR - đó là fallback nội bộ)
_ALLOWED = {o.value for o in LLM_OUTCOMES if o is not Outcome.UNCLEAR}

_PROMPT_TEMPLATE = """Bạn phân loại kết quả cuộc gọi của voicebot ngân hàng xác thực giao dịch thẻ.
Thời điểm cuộc gọi: {{call_time}}.

Đọc transcript (PII đã che bằng token <CARD>/<PHONE>/<ID>/<CODE>/<AMOUNT>/<EMAIL>) rồi trả về CHỈ MỘT JSON đúng schema:

{{{{
  "outcome": "<một trong {allowed}>",
  "confidence": <số thực 0..1>,
  "callback_at": "<ISO-8601 có offset nếu KH hẹn giờ, hoặc chuỗi thời gian tương đối nếu KH nói 'chiều nay'/'5 phút nữa'/'sáng mai', null nếu không hẹn>",
  "evidence": "<trích 1 câu KH nói, <=160 ký tự>"
}}}}

Hướng dẫn chọn nhãn:
- verified_confirmed: KH xác nhận chính chủ thực hiện giao dịch.
- verified_denied: KH phủ nhận, không phải mình thực hiện.
- wrong_number: người nghe không phải chủ thẻ, không biết KH.
- relative_answered: người nhà nghe máy, hứa chuyển máy sau.
- callback_requested: KH đồng ý nghe nhưng hẹn gọi lại sau.
- verify_failed: KH không trả lời đúng câu xác thực danh tính nhiều lần.
- angry: KH bức xúc, phàn nàn, chửi.
- do_not_call: KH yêu cầu không liên hệ nữa.
- other_number_requested: người nghe đòi gọi sang số khác.
- scam_suspected_hangup: KH nghi ngờ lừa đảo rồi cúp máy.
- need_human: KH đòi gặp người thật, hoặc ngoài phạm vi bot.

callback_at: nếu KH hẹn giờ cụ thể ("3 giờ chiều", "14:30") -> ISO-8601 tuyệt đối.
Nếu KH nói tương đối ("5 phút nữa", "chiều nay", "sáng mai") -> ghi nguyên lời KH.
Không hẹn -> null.

Không được bịa. Không chắc -> confidence thấp."""


class LlmClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class LabelerConfig:
    model: str = "gpt-4o-mini"
    temperature: float = 0.0
    timeout_s: float = 20.0
    max_transcript_chars: int = 8000       # cắt transcript dài để tránh tốn token / lộ nhiều PII


class OpenAIClient:
    """Adapter OpenAI mỏng. Cần `pip install openai` và biến môi trường OPENAI_API_KEY."""

    def __init__(self, cfg: LabelerConfig):
        self.cfg = cfg
        from openai import OpenAI
        self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.cfg.model, temperature=self.cfg.temperature, timeout=self.cfg.timeout_s,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


class LlmLabeler:
    def __init__(self, client: LlmClient, cfg: LabelerConfig | None = None, tz: tzinfo = timezone.utc):
        self.client = client
        self.cfg = cfg or LabelerConfig()
        self.tz = tz

    def label(self, transcript: str, now: datetime | None = None) -> LlmLabel:
        now = now or datetime.now(self.tz)
        masked = mask_pii(transcript)[: self.cfg.max_transcript_chars]
        call_time = now.astimezone(self.tz).strftime("%Y-%m-%d %H:%M %Z")
        prompt = _PROMPT_TEMPLATE.format(allowed=sorted(_ALLOWED), call_time=call_time)
        try:
            raw = self.client.complete(prompt, masked)
        except Exception as e:
            return self._fallback(f"gọi LLM lỗi: {e!r}", raw=None)
        return self._parse(raw, now)

    def _parse(self, raw: str, now: datetime) -> LlmLabel:
        try:
            data = json.loads(_extract_json(raw))
        except (json.JSONDecodeError, ValueError) as e:
            return self._fallback(f"LLM không trả JSON hợp lệ: {e}", raw)

        label_name = data.get("outcome")
        if label_name not in _ALLOWED:
            return self._fallback(f"nhãn LLM không được phép: {label_name!r}", raw)

        try:
            conf = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            return self._fallback("confidence không phải số", raw)
        conf = max(0.0, min(1.0, conf))

        callback_at = None
        cb = data.get("callback_at")
        if cb:
            cb_str = str(cb)
            # Thu ISO-8601 truoc, sau do fallback sang time parser tieng Viet
            parsed = parse_callback_time(cb_str, now, self.tz)
            if parsed is not None:
                callback_at = parsed
            else:
                return self._fallback(f"callback_at không parse được: {cb!r}", raw)

        return LlmLabel(label=Outcome(label_name), confidence=conf, callback_at=callback_at,
                        model=getattr(self.client, "cfg", LabelerConfig()).model,
                        prompt_version=PROMPT_VERSION, raw_output=raw)

    def _fallback(self, reason: str, raw: str | None) -> LlmLabel:
        return LlmLabel(label=Outcome.UNCLEAR, confidence=0.0, model=None, prompt_version=PROMPT_VERSION,
                        raw_output=json.dumps({"fallback_reason": reason, "raw": raw}, ensure_ascii=False))


def _extract_json(raw: str) -> str:
    """LLM đôi khi bọc JSON trong ```json ... ``` hoặc kèm chú thích -> lấy khối {...} đầu tiên."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("không thấy khối JSON")
    return raw[i:j + 1]


def label_transcript(transcript: str, client: LlmClient, tz: tzinfo = timezone.utc,
                     cfg: LabelerConfig | None = None, now: datetime | None = None) -> LlmLabel:
    return LlmLabeler(client, cfg, tz).label(transcript, now)
