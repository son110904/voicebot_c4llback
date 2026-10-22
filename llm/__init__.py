"""Tầng LLM: đọc transcript của cuộc gọi và trả về nhãn có cấu trúc.

Vì sao tách riêng: khi đổi nhà cung cấp (OpenAI -> model nội bộ) chỉ cần đổi `client`,
luật nghiệp vụ và DB không đụng. Rule engine vẫn tin được: output đi qua schema, lỗi/thấp
confidence -> UNCLEAR -> ĐTV.
"""
from .labeler import LabelerConfig, LlmClient, LlmLabeler, OpenAIClient, label_transcript
from .pii import mask_pii
