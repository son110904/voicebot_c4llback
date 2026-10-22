"""Dashboard Flask: xem danh sách case và timeline từng quyết định của rule engine.

Đọc DB (SQLite hoặc Postgres) qua db.driver.connect — URL lấy từ CALLBACK_DB_URL.
Chỉ ĐỌC - không gọi điện, không ghi.

Chạy: python app.py                                 (SQLite: demo.db)
      CALLBACK_DB_URL=postgresql://... python app.py (Postgres)
"""
import json
import os

from flask import Flask, abort, render_template

from db import connect as db_connect

DB_URL = os.environ.get("CALLBACK_DB_URL", "postgresql://postgres:cb@localhost:5432/callback")

GROUP_COLOR = {
    "XONG": "ok",
    "DỪNG BOT": "stop",
    "NGƯỜI THẬT XỬ LÝ": "human",
    "GỌI LẠI - LỖI PHÍA KHÁCH": "retry",
    "GỌI LẠI - LỖI PHÍA HỆ THỐNG": "sys",
    "KHÁCH HẸN": "appt",
}
ACTION_COLOR = {
    "DIAL_NOW": "retry", "SCHEDULE_CALL": "retry",
    "CLOSE": "ok", "HAND_TO_DTV": "human", "STOP": "stop", "EXHAUSTED": "stop",
}
STATUS_COLOR = {
    "OPEN": "retry", "DONE": "ok", "HUMAN": "human",
    "STOPPED": "stop", "EXHAUSTED": "stop", "CLOSED": "ok",
}

app = Flask(__name__)
app.jinja_env.globals.update(group_color=GROUP_COLOR, action_color=ACTION_COLOR,
                             status_color=STATUS_COLOR)


def db():
    return db_connect(DB_URL)


def dt(s):
    """ISO UTC -> hiển thị giờ VN."""
    if not s:
        return ""
    from datetime import datetime, timedelta, timezone
    return datetime.fromisoformat(s).astimezone(timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")


app.jinja_env.filters["dt"] = dt


@app.route("/")
def index():
    conn = db()
    try:
        rows = list(conn.execute("""
            SELECT c.*,
                   (SELECT COUNT(*) FROM call_attempts a WHERE a.case_id = c.case_id) AS calls,
                   (SELECT COUNT(*) FROM call_attempts a WHERE a.case_id = c.case_id AND a.counted = 1) AS counted,
                   (SELECT MIN(due_at) FROM schedule s WHERE s.case_id = c.case_id AND s.status = 'PENDING') AS next_at,
                   (SELECT COUNT(*) FROM alerts a WHERE a.case_id = c.case_id) AS alerts
            FROM cases c ORDER BY c.urgency, c.created_at DESC
        """))
    finally:
        conn.close()
    return render_template("index.html", cases=rows, db_url=DB_URL)


@app.route("/case/<case_id>")
def case(case_id):
    conn = db()
    try:
        c = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if c is None:
            abort(404)
        alerts = list(conn.execute("SELECT * FROM alerts WHERE case_id = ? ORDER BY received_at", (case_id,)))
        attempts = list(conn.execute("SELECT * FROM call_attempts WHERE case_id = ? ORDER BY started_at",
                                     (case_id,)))
        decisions = list(conn.execute("""
            SELECT d.*, l.label AS llm_label, l.confidence AS llm_conf
            FROM decisions d LEFT JOIN llm_labels l ON l.attempt_id = d.attempt_id
            WHERE d.case_id = ? ORDER BY d.decision_id
        """, (case_id,)))
        sms = list(conn.execute("SELECT * FROM sms_log WHERE case_id = ? ORDER BY sms_id", (case_id,)))
        outbox = list(conn.execute("SELECT * FROM outbox WHERE case_id = ? ORDER BY event_id", (case_id,)))
        schedule = list(conn.execute("SELECT * FROM schedule WHERE case_id = ? ORDER BY schedule_id",
                                     (case_id,)))
    finally:
        conn.close()

    decisions = [{**dict(d), "detail_parsed": json.loads(d["detail"])} for d in decisions]
    outbox = [{**dict(o), "payload_parsed": json.loads(o["payload"])} for o in outbox]
    blocked = json.loads(c["blocked_phones"])
    return render_template("case.html", c=c, alerts=alerts, attempts=attempts, decisions=decisions,
                           sms=sms, outbox=outbox, schedule=schedule, blocked=blocked)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
