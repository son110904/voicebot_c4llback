"""Dialer: StreamingDialer (mô hình streaming của Hiếu) và ESLDialer (ESL outbound, cần gevent).

ESLDialer import có phòng vệ: máy dev thiếu gevent vẫn dùng được StreamingDialer và chạy test.
"""
from .streaming_dialer import StreamingDialer, StreamingDialerConfig

try:
    from .esl_dialer import ESLDialer, ESLDialerConfig
except ImportError:  # thiếu gevent/greenswitch (máy dev) -> ESLDialer không khả dụng, không chặn StreamingDialer
    ESLDialer = ESLDialerConfig = None

__all__ = ["StreamingDialer", "StreamingDialerConfig", "ESLDialer", "ESLDialerConfig"]
