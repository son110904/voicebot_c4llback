# voicebot_callback

Bộ **campaign rules + DB + LLM labeler + scheduler** cho voicebot xác thực giao dịch thẻ nghi gian lận (VietinBank). Chỉ tập trung vào **quyết định gọi lại khách hàng**; không gọi điện thật.

## Cấu trúc

```
voicebot_callback/
├── campaign_rules.yaml       # Mọi con số: P1/P2/P3, khoảng chờ, khung giờ, SMS
├── callback/                 # Rule engine (hàm thuần)
│   ├── models.py             # Enum + dataclass
│   ├── config.py             # Đọc & kiểm tra YAML
│   ├── urgency.py            # Tính P1/P2/P3 từ dữ liệu cảnh báo
│   ├── pbx.py                # Map call_status/SIP -> Outcome
│   └── rules.py              # decide / pre_dial_check / merge_alert
├── db/                       # Lưu trữ + luồng xử lý
│   ├── schema.sql            # 7 bảng SQLite
│   ├── repo.py               # SQL thuần
│   └── service.py            # CallbackService ghép rule engine + DB
├── llm/                      # Gán nhãn transcript
│   ├── pii.py                # Che số thẻ / ĐT / CCCD / OTP / số tiền
│   └── labeler.py            # Prompt + validate + fallback UNCLEAR
├── scheduler.py              # Worker: nhận lịch -> gọi -> ghi kết quả -> đẩy outbox
├── tests/                    # 102 unit test (unittest, không cần pytest)
├── demo_rules.py             # Mô phỏng rule engine
└── demo_db.py                # Mô phỏng cả luồng lưu DB
```

## Chạy

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
python demo_rules.py
python demo_db.py
```

Dùng Python 3.10+.

## Tích hợp

```python
from datetime import datetime, timezone
from callback import Alert, load_config
from db import CallbackService, LlmLabel, connect

cfg = load_config()
svc = CallbackService(connect("callback.db"), cfg, config_version="prod-v1")

# 1. Nhận cảnh báo từ TWO/TWFA
svc.ingest_alert(Alert(...), phone="0912...", now=datetime.now(timezone.utc), phone_sms="0987...")

# 2. Worker tick (xem scheduler.py để tích hợp với tổng đài thật)
from scheduler import Scheduler, SchedulerConfig
Scheduler(svc, dialer=my_dialer, dispatcher=my_outbox_sender).run_forever()
```

### Nối LLM thật

```python
from llm import LlmLabeler, OpenAIClient
from llm.labeler import LabelerConfig
labeler = LlmLabeler(OpenAIClient(LabelerConfig(model="gpt-4o-mini")), tz=cfg.tz)
label = labeler.label(transcript)      # LlmLabel; lỗi/thấp confidence -> UNCLEAR
svc.record_call_result(..., llm=label)
```

### Dialer tích hợp Freeswitch/Asterisk

Dialer là callable `(ScheduledCall, Decision) -> DialResult`. Trong `DialResult` điền `pbx_status`, `sip_code`, `answered_seconds`, `machine_detected`, `verified` (bot đã xác thực thành công trước khi rớt chưa), và `llm` (nếu đã chạy LLM labeler ngay sau cuộc gọi).

## Luật nghiệp vụ

Mọi con số nằm ở [campaign_rules.yaml](campaign_rules.yaml). Nguồn: `YEU_CAU_BAI_TOAN_VOICEBOT` mục 4 (Redial Policy). Các chỗ docs không nói mà mình tự chốt đều có chú thích `GIẢ ĐỊNH` trong YAML.

Các điểm chính:

- 6 nhóm kết quả cuộc gọi (XONG / DỪNG BOT / NGƯỜI THẬT / RETRY-KH / RETRY-HỆ THỐNG / HẸN).
- P1/P2/P3 tự tính từ trạng thái thẻ, cấp phép, số tiền, số cảnh báo.
- Đếm số cuộc theo cửa sổ 24h trượt, không theo ngày lịch.
- Giới hạn trên mỗi số điện thoại (nhiều case cùng số).
- Lỗi hệ thống không tính lượt; quá 3 lần liên tiếp -> P1 chuyển ĐTV, P2/P3 backoff + báo động vận hành.
- Nhầm số / xác thực sai nhiều lần -> chặn số, không SMS.
- SMS KHÔNG được chứa chi tiết giao dịch; config bắt lỗi `{...}` trong mẫu.
- Cảnh báo trùng gộp vào case đang mở, giữ nguyên số lượt đã gọi, nâng mức khẩn nếu cần.
- Mỗi `call_id` chỉ ghi 1 lần (idempotent); mỗi case tối đa 1 lịch sống (unique index).

## Chưa làm (đợt sau)

- Nối thật với TWO/TWFA, Freeswitch/Asterisk, agent của Đạt.
- Dashboard theo dõi tình trạng case.
- Chuyển SQLite -> Postgres khi lên UAT.
