-- Schema Postgres cho voicebot_callback.
-- Khác schema_sqlite.sql ở: BIGSERIAL thay AUTOINCREMENT, DOUBLE PRECISION thay REAL.
-- Boolean vẫn lưu bằng SMALLINT 0/1 để code đọc/ghi đồng nhất với SQLite.
-- JSON lưu dưới dạng TEXT (demo); UAT có thể đổi sang JSONB bằng ALTER COLUMN.

CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    card_ref       TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    phone          TEXT NOT NULL,
    phone_sms      TEXT,
    urgency        TEXT NOT NULL CHECK (urgency IN ('P1', 'P2', 'P3')),
    status         TEXT NOT NULL CHECK (status IN ('OPEN', 'DONE', 'HUMAN', 'STOPPED', 'EXHAUSTED', 'CLOSED')),
    card_open      SMALLINT NOT NULL,
    alert_count    INTEGER NOT NULL DEFAULT 1,
    sms_sent       INTEGER NOT NULL DEFAULT 0,
    appointments   INTEGER NOT NULL DEFAULT 0,
    do_not_call    SMALLINT NOT NULL DEFAULT 0,
    blocked_phones TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cases_card ON cases (card_ref, status);
CREATE INDEX IF NOT EXISTS ix_cases_customer ON cases (customer_id, status);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases (case_id),
    card_ref     TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    amount_vnd   BIGINT NOT NULL,
    approved     SMALLINT NOT NULL,
    card_blocked SMALLINT NOT NULL,
    created_at   TEXT NOT NULL,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_attempts (
    attempt_id             BIGSERIAL PRIMARY KEY,
    call_id                TEXT NOT NULL UNIQUE,
    case_id                TEXT NOT NULL REFERENCES cases (case_id),
    phone                  TEXT NOT NULL,
    started_at             TEXT NOT NULL,
    ended_at               TEXT NOT NULL,
    pbx_status             TEXT NOT NULL,
    sip_code               INTEGER,
    answered_seconds       DOUBLE PRECISION,
    verified               SMALLINT NOT NULL DEFAULT 0,
    outcome                TEXT NOT NULL,
    counted                SMALLINT NOT NULL,
    -- Context cho ĐTV khi chuyển máy (docs: handoff screen-pop)
    transcript             TEXT,
    transcript_summary     TEXT,
    recording_id           TEXT,
    misunderstanding_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_attempts_case ON call_attempts (case_id, started_at);
CREATE INDEX IF NOT EXISTS ix_attempts_phone ON call_attempts (phone, started_at);

CREATE TABLE IF NOT EXISTS llm_labels (
    label_id       BIGSERIAL PRIMARY KEY,
    attempt_id     BIGINT NOT NULL UNIQUE REFERENCES call_attempts (attempt_id),
    label          TEXT NOT NULL,
    confidence     DOUBLE PRECISION,
    callback_at    TEXT,
    model          TEXT,
    prompt_version TEXT,
    raw_output     TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id    BIGSERIAL PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES cases (case_id),
    attempt_id     BIGINT REFERENCES call_attempts (attempt_id),
    kind           TEXT NOT NULL CHECK (kind IN ('alert', 'pre_dial', 'after_call', 'external')),
    action         TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    next_call_at   TEXT,
    config_version TEXT,
    decided_at     TEXT NOT NULL,
    detail         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_case ON decisions (case_id, decision_id);

CREATE TABLE IF NOT EXISTS schedule (
    schedule_id BIGSERIAL PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases (case_id),
    due_at      TEXT NOT NULL,
    phone       TEXT NOT NULL,
    appointment SMALLINT NOT NULL DEFAULT 0,
    status      TEXT NOT NULL CHECK (status IN ('PENDING', 'CLAIMED', 'DIALING', 'DONE', 'CANCELLED')),
    decision_id BIGINT REFERENCES decisions (decision_id),
    claimed_by  TEXT,
    claimed_at  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_schedule_active ON schedule (case_id)
    WHERE status IN ('PENDING', 'CLAIMED', 'DIALING');
CREATE INDEX IF NOT EXISTS ix_schedule_due ON schedule (status, due_at);

CREATE TABLE IF NOT EXISTS sms_log (
    sms_id      BIGSERIAL PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases (case_id),
    decision_id BIGINT NOT NULL UNIQUE REFERENCES decisions (decision_id),
    phone       TEXT NOT NULL,
    template    TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    event_id    BIGSERIAL PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases (case_id),
    decision_id BIGINT REFERENCES decisions (decision_id),
    event_type  TEXT NOT NULL CHECK (event_type IN ('SMS', 'DTV_HANDOFF', 'OPS_ALERT', 'CASE_FINAL')),
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT')),
    tries       INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox (status, event_id);
