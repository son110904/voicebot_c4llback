"""Che thông tin nhạy cảm trước khi gửi transcript cho LLM.

Dữ liệu KH là của ngân hàng, LLM là bên thứ 3 -> KHÔNG được gửi đi:
số thẻ, số CMND/CCCD, số TK, OTP/PIN, số điện thoại, email, số tiền giao dịch cụ thể.
Che bằng token (vd '<CARD>') để LLM vẫn hiểu ngữ cảnh "KH nhắc đến số thẻ".
"""
from __future__ import annotations

import re

# Thứ tự quan trọng: khớp mẫu dài trước (card 13-19 số) trước số điện thoại
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:\d[ \-]?){12,18}\d\b"), "<CARD>"),          # số thẻ 13-19 chữ số
    (re.compile(r"\b0\d{9,10}\b"), "<PHONE>"),                      # số ĐT VN 
    (re.compile(r"\b\d{9,12}\b"), "<ID>"),                          # CMND 9, CCCD 12, số TK
    (re.compile(r"\b\d{4,8}\b"), "<CODE>"),                         # OTP/PIN
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"\b\d{1,3}(?:[., ]\d{3}){1,}(?:\s*(?:đ|vnd|đồng))?\b", re.I), "<AMOUNT>"),
]


def mask_pii(text: str) -> str:
    """Che PII. Token <AMOUNT>/<CARD>/<PHONE>/<ID>/<CODE>/<EMAIL> giữ nguyên ngữ cảnh."""
    for pattern, token in _RULES:
        text = pattern.sub(token, text)
    return text
