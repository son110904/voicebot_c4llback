-- DB cho luồng gọi lại (demo: SQLite; lên Postgres đổi INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL).
-- Thời điểm lưu ISO-8601 UTC độ dài cố định -> so sánh chuỗi = so sánh thời gian.
-- KHÔNG lưu số thẻ đầy đủ, KHÔNG lưu transcript (chỉ nhãn LLM đã che PII).

-- Mỗi case = 1 lần cần xác thực với 1 KH (có thể gộp nhiều cảnh báo)
CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    card_ref       TEXT NOT NULL,                 -- token thẻ
    customer_id    TEXT NOT NULL,
    phone          TEXT NOT NULL,
    phone_sms      TEXT,
    urgency        TEXT NOT NULL CHECK (urgency IN ('P1', 'P2', 'P3')),
    status         TEXT NOT NULL CHECK (status IN ('OPEN', 'DONE', 'HUMAN', 'STOPPED', 'EXHAUSTED', 'CLOSED')),
    card_open      INTEGER NOT NULL,
    alert_count    INTEGER NOT NULL DEFAULT 1,
    sms_sent       INTEGER NOT NULL DEFAULT 0,
    appointments   INTEGER NOT NULL DEFAULT 0,
    do_not_call    INTEGER NOT NULL DEFAULT 0,
    blocked_phones TEXT NOT NULL DEFAULT '[]',    -- JSON
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0     -- khóa lạc quan: phát hiện 2 tiến trình cùng sửa 1 case
);
CREATE INDEX IF NOT EXISTS ix_cases_card ON cases (card_ref, status);
CREATE INDEX IF NOT EXISTS ix_cases_customer ON cases (customer_id, status);

-- Cảnh báo gốc từ TWO/TWFA. alert_id là khóa chính -> nhận trùng thì bỏ qua (chống trùng)
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases (case_id),
    card_ref     TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    amount_vnd   INTEGER NOT NULL,
    approved     INTEGER NOT NULL,
    card_blocked INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    received_at  TEXT NOT NULL
);

-- Mỗi cuộc gọi đã thực hiện. call_id từ tổng đài là UNIQUE -> đẩy kết quả 2 lần không ghi trùng
CREATE TABLE IF NOT EXISTS call_attempts (
    attempt_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id          TEXT NOT NULL UNIQUE,
    case_id          TEXT NOT NULL REFERENCES cases (case_id),
    phone            TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    pbx_status       TEXT NOT NULL,                -- Completed / No Answer / Busy / Failed / Transferred / Incomplete
    sip_code         INTEGER,
    answered_seconds REAL,
    verified         INTEGER NOT NULL DEFAULT 0,
    outcome          TEXT NOT NULL,                -- outcome rule engine đã dùng
    counted          INTEGER NOT NULL              -- có tính vào giới hạn số lượt không
);
CREATE INDEX IF NOT EXISTS ix_attempts_case ON call_attempts (case_id, started_at);
CREATE INDEX IF NOT EXISTS ix_attempts_phone ON call_attempts (phone, started_at);

-- Nhãn LLM gán cho cuộc gọi có hội thoại (để soát lại LLM gán đúng/sai, đổi prompt thì so sánh được)
CREATE TABLE IF NOT EXISTS llm_labels (
    label_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     INTEGER NOT NULL UNIQUE REFERENCES call_attempts (attempt_id),
    label          TEXT NOT NULL,
    confidence     REAL,
    callback_at    TEXT,
    model          TEXT,
    prompt_version TEXT,
    raw_output     TEXT,                           -- JSON LLM trả về (đã che PII)
    created_at     TEXT NOT NULL
);

-- Mọi quyết định của rule engine + lý do -> giải trình được "vì sao gọi/không gọi"
CREATE TABLE IF NOT EXISTS decisions (
    decision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id        TEXT NOT NULL REFERENCES cases (case_id),
    attempt_id     INTEGER REFERENCES call_attempts (attempt_id),
    kind           TEXT NOT NULL CHECK (kind IN ('alert', 'pre_dial', 'after_call', 'external')),
    action         TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    next_call_at   TEXT,
    config_version TEXT,                           -- quyết định theo bộ luật phiên bản nào
    decided_at     TEXT NOT NULL,
    detail         TEXT NOT NULL                   -- JSON đầy đủ của Decision
);
CREATE INDEX IF NOT EXISTS ix_decisions_case ON decisions (case_id, decision_id);

-- Lịch gọi. PENDING -> CLAIMED (worker nhận) -> DIALING (đang gọi) -> DONE; hoặc CANCELLED
CREATE TABLE IF NOT EXISTS schedule (
    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases (case_id),
    due_at      TEXT NOT NULL,
    phone       TEXT NOT NULL,
    appointment INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL CHECK (status IN ('PENDING', 'CLAIMED', 'DIALING', 'DONE', 'CANCELLED')),
    decision_id INTEGER REFERENCES decisions (decision_id),
    claimed_by  TEXT,
    claimed_at  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Mỗi case tối đa 1 lịch đang sống -> không bao giờ có 2 cuộc gọi song song cho 1 case
CREATE UNIQUE INDEX IF NOT EXISTS ux_schedule_active ON schedule (case_id)
    WHERE status IN ('PENDING', 'CLAIMED', 'DIALING');
CREATE INDEX IF NOT EXISTS ix_schedule_due ON schedule (status, due_at);

-- SMS đã quyết định gửi (đếm giới hạn SMS/case, đối soát với SMS gateway)
CREATE TABLE IF NOT EXISTS sms_log (
    sms_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases (case_id),
    decision_id INTEGER NOT NULL UNIQUE REFERENCES decisions (decision_id),
    phone       TEXT NOT NULL,
    template    TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Hộp thư đi: việc cần đẩy ra hệ thống khác (agent của Đạt, SMS gateway, hàng đợi ĐTV, vận hành).
-- Ghi cùng transaction với quyết định -> không mất việc khi sập giữa chừng; bên đẩy tự retry.
CREATE TABLE IF NOT EXISTS outbox (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases (case_id),
    decision_id INTEGER REFERENCES decisions (decision_id),
    event_type  TEXT NOT NULL CHECK (event_type IN ('SMS', 'DTV_HANDOFF', 'OPS_ALERT', 'CASE_FINAL')),
    payload     TEXT NOT NULL,                     -- JSON
    status      TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT')),
    tries       INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox (status, event_id);
