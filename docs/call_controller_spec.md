# Đặc tả Call Controller

Cập nhật 16/11/2026. Bản sống để đồng nghiệp sửa và comment nằm trên Claude Docs; file này là bản xuất để đọc trên GitHub.

Call controller là lớp điều phối cuộc gọi outbound của voicebot xác thực giao dịch thẻ nghi gian lận: lấy lịch gọi từ DB, quay số qua FreeSWITCH, điều khiển hội thoại cùng voice agent, thu kết quả và đưa về rule engine để quyết định bước tiếp. Rule engine, DB, LLM labeler và scheduler đã có trong repo `voicebot_callback`; tài liệu này đặc tả phần nối chúng với tổng đài thật và voice agent.

## 1. Phạm vi

Call controller chịu trách nhiệm từ lúc một lịch gọi tới hạn đến lúc kết quả cuộc gọi được ghi vào DB. Nó không quyết định *có gọi hay không* (việc của rule engine) và không hiểu KH nói gì (việc của voice agent và LLM labeler).

**Làm:**

- Nhận lịch gọi tới hạn từ bảng `schedule`, ưu tiên P1 trước.
- Gọi `pre_dial_check` ngay trước khi quay số; chỉ quay khi rule engine trả `DIAL_NOW`.
- Quay số qua FreeSWITCH (ESL inbound, lệnh `originate`) với caller ID là hotline.
- Khi KH nhấc máy: điều khiển cuộc gọi qua ESL outbound — phát câu bot nói, thu câu KH nói, chuyển cho voice agent, chuyển ĐTV khi cần, cúp máy.
- Thu kết quả: hangup cause, mã SIP, thời lượng, các channel variable mà voice agent để lại → `DialResult`.
- Ghi kết quả qua `record_call_result` (idempotent theo `call_id`) để rule engine quyết định bước tiếp.
- Đẩy các event trong `outbox` (ZNS, DTV_HANDOFF, PUSH_IPAY, CASE_FINAL) tới hệ thống ngoài.

**Không làm:**

- Không tính P1/P2/P3, không đếm lượt, không chọn số dự phòng — đó là rule engine (`callback/rules.py`).
- Không nhận diện giọng nói, không sinh câu trả lời — đó là voice agent (ASR → LLM → TTS).
- Không gán nhãn transcript — đó là LLM labeler (`llm/labeler.py`); controller chỉ chuyển nhãn về.
- Không gửi ZNS hay push iPay thật — chỉ ghi `outbox`, gateway ngoài đọc và gửi.
- Không tự khóa thẻ — chỉ chuyển ĐTV kèm ticket.

| Thành phần giáp ranh | Ai làm | Giao tiếp với call controller |
| --- | --- | --- |
| Rule engine | repo `voicebot_callback`, hàm thuần, đã có | Gọi hàm `start_call` / `record_call_result` qua `CallbackService` |
| Voice agent (ASR/LLM/TTS) | Đạt, model chạy trên máy có GPU | Audio hai chiều + kết quả hội thoại trả về qua channel variables |
| FreeSWITCH | repo `virtual-pbx` (Docker) | ESL inbound cổng 8021 để quay số, socket 8084 để điều khiển cuộc gọi |
| ZNS gateway, iPay, hàng đợi ĐTV, agent của Đạt | Bên ngoài | Đọc bảng `outbox`, đánh dấu đã gửi |

## 2. Kiến trúc tổng thể

![Kiến trúc: 9 khối, luồng từ cảnh báo tới outbox](img/call_controller_architecture.png)

Cảnh báo từ TWO/TWFA vào DB qua `ingest_alert`. Scheduler lấy lịch tới hạn, dialer quay số qua FreeSWITCH; khi KH nhấc máy FreeSWITCH giao cuộc gọi cho session controller, session controller nói chuyện với voice agent rồi để kết quả lại trong channel variables. Dialer đọc chúng lúc cúp máy, scheduler ghi vào DB, rule engine quyết định bước tiếp, và các hệ thống ngoài đọc outbox.

Hai repo: `voicebot_callback` (scheduler, dialer, rule engine, DB) và `virtual-pbx` (FreeSWITCH + session controller mô phỏng). Khi làm session controller thật nên đặt nó trong `voicebot_callback` để dùng chung config và models.

## 3. Thành phần và trách nhiệm

Bốn khối đã có code; hai khối còn thiếu là session controller thật và cầu nối voice agent.

| Khối | Trách nhiệm | File | Trạng thái |
| --- | --- | --- | --- |
| Scheduler worker | Vòng lặp: khôi phục cuộc mất kết quả → claim lịch tới hạn → quay từng cuộc → đẩy outbox → ngủ tới lịch kế (tối đa 10 s) | `scheduler.py` | Đã có, test với dialer giả |
| Dialer (ESL inbound) | Kết nối ESL 8021, `originate` tới số KH, chờ `CHANNEL_HANGUP_COMPLETE`, dựng `DialResult` | `dialer/esl_dialer.py` | Đã có, mới test loopback |
| Session controller (ESL outbound) | Nhận socket từ dialplan khi KH nhấc máy, chạy kịch bản hội thoại (mục 6), `export` channel variables trước khi cúp | `virtual-pbx/esl_outbound_server.py` | Mới là mô phỏng bằng tone (ext 9992, 9993, 9994, 9985); phần thật chưa làm |
| Cầu nối voice agent | Đưa audio KH tới ASR, lấy câu trả lời từ LLM, phát TTS về cho KH | chưa có | Chưa làm, cần chốt phương án ở mục 7 |
| Result mapper | Hangup cause + mã SIP → `pbx_status` → `Outcome` | `_CAUSE_TO_STATUS` trong `esl_dialer.py`, `callback/pbx.py` | Đã có; bảng mã SIP là giả định theo RFC 3261 |
| CallbackService | Mỗi hàm là một transaction: `start_call`, `record_call_result`, `recover_lost_calls`, ghi outbox | `db/service.py` | Đã có, 7 bảng Postgres |
| Outbox dispatcher | Gửi từng event outbox tới ZNS gateway / iPay / hàng đợi ĐTV / agent của Đạt, đánh dấu SENT hoặc lỗi | giao diện `OutboxDispatcher` trong `scheduler.py` | Chưa có sender thật |
| FreeSWITCH | PBX: SIP trunk, dialplan, media, ghi âm, AMD | `virtual-pbx/` (Docker, image `safarov/freeswitch`) | Chạy local, Linphone đăng ký được qua TCP |

## 4. Vòng đời một cuộc gọi

![Vòng đời lịch gọi: 5 trạng thái](img/call_controller_lifecycle.png)

Một vòng `tick()` của worker đi qua 8 bước:

1. `recover_lost_calls`: lịch `DIALING` quá 15 phút → ghi `system_error`, rule engine tạo lịch mới.
2. `claim_due_calls`: lấy tối đa 10 lịch `PENDING` tới hạn (hoặc `CLAIMED` quá 5 phút), P1 trước, đánh dấu `CLAIMED` với `worker_id`.
3. Với từng lịch, `start_call`: kiểm tra thẻ còn mở (`card_checker`), rồi `pre_dial_check`. Không phải `DIAL_NOW` → hủy lịch này, ghi quyết định, tạo lịch mới nếu cần.
4. `DIAL_NOW` → `mark_dialing`, dialer `originate` tới `target_phone`.
5. KH nhấc máy → FreeSWITCH chạy dialplan → session controller điều khiển hội thoại (mục 6).
6. Cúp máy → dialer dựng `DialResult` từ hangup cause, mã SIP, `billsec` và channel variables.
7. `record_call_result`: map sang `Outcome`, `decide()`, ghi `call_attempts` + `decisions`, đặt lịch mới / ZNS / `DTV_HANDOFF` vào `outbox`, lịch cũ → `DONE`. Cùng `call_id` gửi lại thì trả quyết định cũ.
8. Đẩy outbox `PENDING` qua dispatcher, đánh dấu `SENT` hoặc tăng `tries`.

Sau đó worker ngủ tới lịch kế tiếp, tối đa 10 giây. Cảnh báo P1 mới có thể gọi thẳng `dial_one` ngay khi ingest, không chờ vòng lặp.

## 5. Hợp đồng với FreeSWITCH

Controller nói chuyện với FreeSWITCH qua hai kênh: ESL inbound (controller chủ động gọi lệnh) để quay số, và ESL outbound (FreeSWITCH gọi sang controller) để điều khiển hội thoại.

**Quay số (ESL inbound, cổng 8021).** Dialer gửi một lệnh `originate` cho mỗi cuộc:

```
originate {origination_caller_id_number=<hotline>,originate_timeout=30,ignore_early_media=true,voicebot_call_id=<call_id>}sofia/gateway/<trunk>/<phone> <voicebot_ext> XML default
```

- Test local thay `sofia/gateway/<trunk>/<phone>` bằng `loopback/<phone>/default`.
- `originate` chặn tới khi KH nhấc máy hoặc lỗi: `+OK <uuid>` = đã nhấc máy, `-ERR <cause>` = không nối được.
- Sau `+OK`: `filter Unique-ID <uuid>` rồi chờ `CHANNEL_HANGUP_COMPLETE`, tối đa `call_timeout` (300 s).
- `voicebot_call_id` đi theo kênh để session controller và log khớp được với `DialResult`.

**Dialplan.** Khi KH nhấc máy, B-leg vào extension voicebot: `answer` → `sleep 500` → `socket esl-server:8084 full`. FreeSWITCH mở kết nối tới session controller và trao quyền điều khiển kênh (file `conf/99_voicebot.xml`).

**Channel variables.** Session controller `export` (không phải `set`, để biến lan sang A-leg) các biến sau trước khi cúp; dialer đọc chúng từ header `variable_*` của event hangup.

| Biến | Giá trị | Ý nghĩa |
| --- | --- | --- |
| `voicebot_call_id` | do dialer đặt lúc originate | Khớp cuộc gọi với `DialResult` |
| `voicebot_status` | `completed` / `incomplete` / `transferred` | Bot đi hết kịch bản, rớt giữa chừng, hay đã chuyển ĐTV |
| `voicebot_verified` | `true` / `false` | Đã xác thực danh tính KH trước khi kết thúc chưa |
| `voicebot_llm_label` | một giá trị `Outcome` (`verified_confirmed`, `callback_requested`, …) | Nhãn kết quả hội thoại |
| `voicebot_llm_conf` | số thực 0 đến 1 | Độ tin cậy; dưới `llm_min_confidence` (0.7) rule engine coi là `unclear` |
| `voicebot_callback_at` | ISO-8601 có offset | Giờ KH hẹn gọi lại |
| `voicebot_machine` | `true` | AMD phát hiện hộp thư thoại |
| `voicebot_recording_id` (đề xuất thêm) | mã bản ghi âm | ĐTV nghe lại khi nhận chuyển máy |
| `voicebot_misunderstandings` (đề xuất thêm) | số nguyên | Số lần bot phải hỏi lại; rule engine dùng cho screen-pop |

**Map kết quả.** Hai bước: hangup cause → `pbx_status` (dialer), rồi `pbx_status` + mã SIP + thời lượng → `Outcome` (`callback/pbx.py`).

| Hangup cause của FreeSWITCH | `pbx_status` |
| --- | --- |
| `NORMAL_CLEARING`, `NORMAL_UNSPECIFIED` | Theo `voicebot_status`: completed → Completed, transferred → Transferred, còn lại → Incomplete |
| `NO_ANSWER`, `ORIGINATOR_CANCEL`, `NO_USER_RESPONSE`, `ALLOTTED_TIMEOUT` | No Answer |
| `USER_BUSY`, `NORMAL_CIRCUIT_CONGESTION`, `CALL_REJECTED` | Busy |
| `UNALLOCATED_NUMBER`, `NO_ROUTE_DESTINATION`, `SUBSCRIBER_ABSENT`, `NORMAL_TEMPORARY_FAILURE`, `SERVICE_UNAVAILABLE`, `NETWORK_OUT_OF_ORDER`, … | Failed, kèm mã SIP `sip_term_status` |
| `ATTENDED_TRANSFER`, `BLIND_TRANSFER` | Transferred |

| `pbx_status` | Điều kiện | `Outcome` | Tính lượt |
| --- | --- | --- | --- |
| Failed | SIP 486, 600 | `busy` | có |
| Failed | SIP 603 | `rejected` | có |
| Failed | SIP 404, 410, 484, 604 | `invalid_number` | không |
| Failed | SIP 480 | `unreachable` | có |
| Failed | SIP 408, 487 | `no_answer` | có |
| Failed | mã khác hoặc không có | `system_error` | không |
| Completed / Incomplete | `voicebot_machine=true` | `voicemail` | có |
| Completed / Incomplete | nói dưới 5 giây | `short_hangup` | có |
| Incomplete | còn lại | `dropped` (nếu `verified=true` → coi như xong) | có |
| Completed | còn lại | nhãn LLM; không có nhãn → `unclear` → ĐTV | có |

Bảng mã SIP là giả định theo RFC 3261. Mỗi nhà mạng trả mã hơi khác nhau, cần đối chiếu log của trunk thật trước khi pilot.

## 6. Kịch bản hội thoại trong cuộc gọi

![Kịch bản hội thoại: 5 bước, 2 rẽ nhánh](img/call_controller_dialog.png)

| Nhánh ở bước 4 | Nhãn | Nhóm rule engine | Bot làm gì tiếp |
| --- | --- | --- | --- |
| KH xác nhận chính chủ | `verified_confirmed` | XONG | Cảm ơn, cúp máy; case đóng |
| KH phủ nhận | `verified_denied` | NGƯỜI THẬT | Chuyển ĐTV ưu tiên cao (khóa thẻ, tra soát) |
| KH không chắc | hỏi lại 1 lần; vẫn không chắc → `customer_unsure` | NGƯỜI THẬT | Chuyển ĐTV |
| KH hẹn gọi lại | `callback_requested` + `callback_at` | KHÁCH HẸN | Chào, cúp; rule engine đặt lịch theo giờ hẹn, không tính lượt |
| Đòi người thật, bấm 0, bot không hiểu 2 lần | `need_human` | NGƯỜI THẬT | `transfer` sang ĐTV ngay nếu có (`voicebot_status=transferred`), không thì cúp và tạo ticket |
| Người nghe không phải chủ thẻ | `wrong_number` hoặc `relative_answered` | DỪNG BOT hoặc GỌI LẠI | Xin lỗi, cúp |
| KH bức xúc, yêu cầu không gọi | `angry` hoặc `do_not_call` | NGƯỜI THẬT hoặc DỪNG BOT | Xin lỗi, cúp |
| KH nghi lừa đảo rồi cúp | `scam_suspected_hangup` | GỌI LẠI | Rule engine gửi ZNS trước khi gọi lại |

**Quy tắc chung:**

- Câu opt-out đọc sau khi xác thực danh tính, trước khi đọc giao dịch (`call_script.opt_out_prompt` trong YAML). KH chọn opt-out → `do_not_call` hoặc `need_human`.
- Không đọc chi tiết giao dịch trước khi xác thực xong; không bao giờ đọc số thẻ đầy đủ.
- Câu hỏi xác thực danh tính và số lần sai tối đa chưa chốt (mục 12); sai quá → `verify_failed`, chặn số, không ZNS, ĐTV ưu tiên cao.
- Mỗi lần bot phải hỏi lại tăng `misunderstanding_count`; đến 2 → `need_human`.
- Toàn cuộc không quá `call_timeout` 300 giây; im lặng quá lâu sau khi nhắc → cúp, dialer thấy thời lượng ngắn → `short_hangup`.
- Kết thúc: `export` đủ biến ở mục 5 rồi mới `hangup`; thiếu biến thì dialer coi là Incomplete.

## 7. Nối voice agent (ASR / LLM / TTS)

Chốt làm phương án A (theo lượt) trước để chạy được end-to-end sớm, rồi nâng lên B (streaming) khi pilot với KH thật. Giao diện giữa controller và agent giữ cố định để đổi A sang B không đụng rule engine.

| Phương án | Cách làm | Ưu | Nhược | Dùng khi |
| --- | --- | --- | --- | --- |
| A. Theo lượt | Session controller `record` câu KH nói (dừng khi im lặng hoặc hết thời gian), gửi file WAV tới agent qua HTTP, nhận file audio trả lời, `playback` | Chỉ cần ESL + HTTP, dễ debug, đủ để test rule engine và demo nội bộ | Trễ mỗi lượt bằng tổng thời gian ASR + LLM + TTS; KH không ngắt lời bot được | Giai đoạn 1: demo, test 2 máy Linphone |
| B. Streaming | FreeSWITCH đẩy audio PCM qua WebSocket tới agent (`mod_audio_stream` hoặc `mod_audio_fork`), agent trả audio stream về | Trễ thấp, ngắt lời được, giống tổng đài thật | Phải build module vào image FreeSWITCH; agent phải xử lý stream và VAD; khó debug hơn | Giai đoạn 2: pilot với KH thật |
| C. Agent là SIP endpoint | Agent đăng ký như một máy nhánh (giống Linphone), FreeSWITCH bridge KH với agent | FreeSWITCH gần như không đổi | Agent phải tự lo SIP/RTP stack, khó nhất, trùng việc với FreeSWITCH | Không chọn |

**Giao diện với agent (áp dụng cho cả A và B):**

- Mở phiên: controller gửi `call_id`, `case_id`, mức khẩn, nội dung cần xác thực (tên merchant, số tiền, thời gian — không có số thẻ đầy đủ), và câu opt-out lấy từ `campaign_rules.yaml`.
- Mỗi lượt: audio KH vào, audio (hoặc text để TTS) bot ra, kèm cờ `end_of_call` và `action` (`continue` / `transfer` / `hangup`).
- Đóng phiên: agent trả `label`, `confidence`, `verified`, `callback_at`, `transcript` (đã che PII), `misunderstanding_count`. Session controller `export` các giá trị này thành channel variables ở mục 5.
- Agent không được gọi số do người nghe cung cấp và không được đọc chi tiết GD trước khi xác thực danh tính.

## 8. Lỗi, timeout, khôi phục và đồng thời

Nguyên tắc: lỗi phía mình không bao giờ làm mất lượt gọi của KH, và không cuộc gọi nào được ghi hai lần.

| Tình huống | Phát hiện bằng | Xử lý | Khối |
| --- | --- | --- | --- |
| Không kết nối được ESL 8021 | `connect()` lỗi, timeout 10 s | `DialResult(Failed)` → `system_error`, không tính lượt, thử lại sau 2 phút; quá 3 lần liên tiếp: P1 chuyển ĐTV, P2/P3 backoff 30 phút | dialer + rule engine |
| `originate` không trả lời | quá `ring_timeout` + 15 s | `Failed` | dialer |
| `originate` trả `-ERR <cause>` | parse cause | map thẳng sang No Answer / Busy / Failed theo bảng mục 5 | dialer |
| Cuộc gọi kéo dài quá `call_timeout` (300 s) | không nhận được hangup | Dựng kết quả từ những gì có (Incomplete); **cần thêm** `uuid_kill <uuid>` để cúp kênh, hiện chưa làm | dialer |
| Worker chết sau khi đã quay số | lịch ở `DIALING` quá `dial_timeout` (15 phút) | `recover_lost_calls` ghi `Failed` → `system_error`, rule engine tạo lịch mới | service |
| Worker chết trước khi quay số | lịch ở `CLAIMED` quá `claim_ttl` (5 phút) | Worker khác nhận lại lịch | `repo.claim_due` |
| Kết quả gửi lại (retry) | trùng `call_id` | Trả quyết định cũ, không ghi gì | service |
| Hai worker cùng sửa một case | `version` không khớp | `ConcurrentUpdateError`, rollback cả transaction, vòng sau xử lý lại | repo |
| Case đóng hoặc thẻ đã khóa sau khi hẹn lịch | `pre_dial_check`, `card_checker` | `G1 STOP` hoặc `K5 CLOSE`, không quay số | service |
| Cảnh báo mới đến khi đang có cuộc gọi dở | `has_dialing` | Gộp vào case, không tạo lịch mới, chờ kết quả cuộc đang gọi | service |
| Session controller mất kết nối giữa hội thoại | `OutboundSessionHasGoneAway` | Kết thúc handler; không export được biến → dialer thấy Incomplete → `dropped` | session controller |
| Voice agent không trả lời trong một lượt | timeout HTTP | Bot xin lỗi, kết thúc, export `voicebot_status=incomplete` kèm lý do; coi là `bad_audio`/`system_error` (không tính lượt) — **cần chốt nhãn** | session controller |

**Đồng thời và giới hạn tải:**

- Mỗi case chỉ có một lịch sống (unique index trong `schedule`); giới hạn theo số điện thoại (5 cuộc/24h mọi case) do rule engine lo.
- Một worker lấy tối đa `batch` = 10 lịch mỗi vòng và quay tuần tự (dialer chặn tới khi cúp máy). Số cuộc song song = số worker; muốn nhiều hơn thì chạy nhiều worker (`worker_id` khác nhau) hoặc dùng gevent pool trong một worker.
- Trần thật sự là số kênh của SIP trunk — chưa biết, hỏi bên tổng đài. **Cần thêm** tham số `max_concurrent_calls` để không claim quá số kênh có.

## 9. Cấu hình

Luật nghiệp vụ nằm trong `campaign_rules.yaml`; tham số kỹ thuật của controller nằm trong hai dataclass và biến môi trường dưới đây.

| Tham số | Mặc định | Ở đâu | Ghi chú |
| --- | --- | --- | --- |
| `esl_host`, `esl_port`, `esl_password` | 127.0.0.1, 8021, ClueCon | `ESLDialerConfig` | Đổi mật khẩu và chỉ mở 8021 trong mạng nội bộ khi lên thật |
| `sip_gateway` | trunk | `ESLDialerConfig` | Tên gateway trong `sip_profiles/external` của FreeSWITCH |
| `voicebot_ext` | 9999 | `ESLDialerConfig` | Extension dialplan trỏ tới `socket esl-server:8084 full` |
| `caller_id` | 19001560 | `ESLDialerConfig` | **Lệch** với `hotline: 1900558868` trong YAML; nên bỏ tham số này và đọc từ YAML |
| `ring_timeout` | 30 s | `ESLDialerConfig` | Thời gian đổ chuông trước khi bỏ cuộc |
| `call_timeout` | 300 s | `ESLDialerConfig` | Tối đa một cuộc kể cả hội thoại |
| `use_loopback`, `loopback_context` | false, default | `ESLDialerConfig` | Bật khi test local không có trunk |
| `worker_id`, `tick_s`, `batch`, `outbox_batch` | worker-1, 10 s, 10, 50 | `SchedulerConfig` | Mỗi worker một `worker_id` riêng |
| `claim_ttl`, `dial_timeout` | 5 phút, 15 phút | `CallbackService` | Ngưỡng coi worker đã chết |
| `CALLBACK_DB_URL` | không có | biến môi trường | `postgresql://user:pass@host:5432/callback` |
| `OPENAI_API_KEY` | không có | biến môi trường | Chỉ cần nếu LLM labeler chạy ở phía controller thay vì trong agent |
| `LISTEN_HOST`, `LISTEN_PORT` | 0.0.0.0, 8084 | `esl_outbound_server.py` | Phải khớp với dialplan |
| Cổng FreeSWITCH | 5060 TCP/UDP, 8021, RTP 16384 đến 16484 UDP | `docker-compose.yml` | Mở firewall Windows cho 5060 và dải RTP khi test 2 máy |
| `hotline`, `llm_min_confidence`, khung giờ, khoảng chờ, mẫu ZNS, `opt_out_prompt` | xem file | `campaign_rules.yaml` | Controller đọc qua `load_config()` |

## 10. Giám sát và bảo mật

Mọi cuộc gọi phải truy ngược được từ `call_id` tới quyết định của rule engine, và không nơi nào chứa số thẻ đầy đủ.

**Log mỗi cuộc** (ghi vào `call_attempts` + `decisions`, và log text của worker):

- `call_id`, `case_id`, `schedule_id`, channel UUID, số gọi (che 4 số giữa trong log text), thời điểm originate / nhấc máy / cúp.
- Hangup cause, mã SIP, `billsec`, toàn bộ channel variables `voicebot_*`.
- `rule_id` + `reason` của quyết định sau cuộc (đã có sẵn trong `decisions.detail`).

**Chỉ số theo dõi:**

| Chỉ số | Nguồn | Ngưỡng báo động |
| --- | --- | --- |
| Cuộc gọi / phút, tỉ lệ nhấc máy | `call_attempts` | Theo dõi xu hướng, chưa đặt ngưỡng |
| Chuỗi `system_error` liên tiếp | rule engine (`Y2`/`Y3`) | Quá 3 → event `OPS_ALERT` trong outbox |
| Lịch quá hạn chưa claim | `schedule` (`due_at < now`, PENDING) | Quá 5 phút → worker chết hoặc quá tải |
| Outbox tồn đọng | `outbox` (PENDING, `tries` tăng) | `tries` ≥ 5 → gateway ngoài lỗi |
| Độ trễ mỗi lượt hội thoại | session controller | Đo khi có agent thật, quyết định chuyển phương án B |
| Tỉ lệ `unclear` | `llm_labels` | Tăng đột biến → prompt hoặc ASR có vấn đề |

**Bảo mật và tuân thủ:**

- Case chỉ giữ `card_ref` (token), không bao giờ lưu số thẻ đầy đủ; transcript đi qua `llm/pii.py` để che số thẻ, số ĐT, CCCD, OTP, số tiền trước khi lưu hoặc gửi LLM.
- ZNS không chứa chi tiết giao dịch; config từ chối mẫu có biến.
- Bot đọc câu opt-out sau khi xác thực danh tính và trước khi vào nội dung GD; KH chọn opt-out → `do_not_call`, bot không gọi lại.
- Không gọi số do người nghe cung cấp (`other_number_requested` → ĐTV kiểm tra với số đã đăng ký).
- ESL: đổi mật khẩu mặc định `ClueCon`, cổng 8021 và 8084 chỉ mở trong mạng nội bộ Docker.
- Ghi âm: nơi lưu, thời hạn giữ, ai được nghe — chưa chốt (mục 12).

## 11. Trạng thái hiện tại và việc còn lại

Phần quyết định và lưu trữ đã xong; phần còn lại tập trung vào session controller thật và voice agent.

| Việc | Trạng thái | Ghi chú |
| --- | --- | --- |
| Rule engine + 102 unit test | Xong | `callback/`, `tests/` |
| Postgres 7 bảng + `CallbackService` | Xong | `db/` |
| Scheduler với dialer giả | Xong | `scheduler.py`, `demo_dial.py` |
| `ESLDialer` quay số qua ESL | Xong, test loopback | `dialer/esl_dialer.py` |
| FreeSWITCH Docker + dialplan + ESL outbound mô phỏng | Xong | `virtual-pbx/`, ext 9992 đến 9994, 9985 |
| Linphone đăng ký vào FreeSWITCH | Xong (TCP) | user 1001, 1002 |
| Test 2 máy Linphone gọi nhau qua FreeSWITCH | Đang làm | Máy thứ hai mượn của chị Hường |
| Session controller thật theo kịch bản mục 6 | Chưa | Thay tone bằng TTS + record + gọi agent |
| Cầu nối voice agent phương án A | Chưa | Chờ model chạy trên máy GPU |
| Chuyển ĐTV trong cuộc gọi (bridge sang máy nhánh hoặc hàng đợi) | Chưa | Hiện chỉ tạo ticket `DTV_HANDOFF` |
| Outbox dispatcher thật (ZNS, iPay, ĐTV, agent Đạt) | Chưa | Có giao diện, chưa có sender |
| `uuid_kill` khi quá `call_timeout`, `max_concurrent_calls` | Chưa | Mục 8 |
| SIP trunk thật + đối chiếu bảng mã SIP | Chưa | Cần tổng đài của bank |
| Thống nhất caller ID (YAML vs code) | Chưa | Mục 9 |

**Thứ tự làm đề xuất:**

1. Xong test 2 máy Linphone (echo 9998, gọi 1001 ↔ 1002) để chắc audio hai chiều qua FreeSWITCH.
2. Viết session controller thật + cầu nối A; dùng Linphone làm "KH" để thử kịch bản.
3. Chạy end-to-end: `seed_demo.py` → scheduler → `ESLDialer` (loopback) → session controller → DB; kiểm tra timeline trên dashboard.
4. Outbox dispatcher thật, bắt đầu từ `DTV_HANDOFF` và `CASE_FINAL` cho agent của Đạt.
5. Nối trunk thật, đối chiếu mã SIP, đo độ trễ rồi quyết định có lên phương án B.

## 12. Câu hỏi mở

- [ ] Voice agent của Đạt nhận và trả audio theo dạng nào: file WAV theo lượt hay stream? Quyết định A/B ở mục 7.
- [ ] Tổng đài của bank là FreeSWITCH hay Asterisk? Trunk trả mã SIP thế nào → cập nhật bảng trong `callback/pbx.py`.
- [ ] Số kênh đồng thời cho phép trên trunk.
- [ ] Chuyển ĐTV: hàng đợi ĐTV nằm ở tổng đài nào, chuyển bằng bridge ngay trong cuộc hay chỉ tạo ticket để ĐTV gọi lại?
- [ ] `dtv_online_count` lấy từ đâu để hoãn gọi khi không có ĐTV online?
- [ ] Câu hỏi xác thực danh tính KH là gì (ngày sinh, 4 số cuối CCCD, …) và sai tối đa mấy lần?
- [ ] Ghi âm có bắt buộc không? Lưu ở đâu, giữ bao lâu, ai được nghe?
- [ ] Caller ID hiển thị: 1900558868 (YAML) hay 19001560 (code)?
- [ ] AMD (phát hiện hộp thư thoại) có bật trên FreeSWITCH không, hay để agent tự nhận ra?
- [ ] Agent không trả lời giữa cuộc thì gán nhãn gì: `bad_audio` hay `system_error` (cả hai không tính lượt)?
