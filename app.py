"""Dashboard Flask: xem danh sách case và timeline từng quyết định của rule engine.

Đọc DB (Postgres) qua db.driver.connect — URL lấy từ CALLBACK_DB_URL.
Chỉ ĐỌC - không gọi điện, không ghi.

Chạy: CALLBACK_DB_URL=postgresql://... python app.py
"""
import json
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from flask import Flask, abort, jsonify, render_template, request

from callback.config import load_config
from db import connect as db_connect
from db.service import CallbackService

DB_URL = os.environ.get("CALLBACK_DB_URL", "postgresql://postgres:cb@localhost:5432/callback")

# ---- Priority model ----
_MODEL_DIR = Path(__file__).parent / "models"
_priority_rf = None
_priority_info = None


def _load_priority_model():
    global _priority_rf, _priority_info
    if _priority_rf is not None:
        return
    with open(_MODEL_DIR / "priority_rf.pkl", "rb") as f:
        _priority_rf = pickle.load(f)
    with open(_MODEL_DIR / "priority_info.json", encoding="utf-8") as f:
        _priority_info = json.load(f)

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
        zns = list(conn.execute("SELECT * FROM zns_log WHERE case_id = ? ORDER BY zns_id", (case_id,)))
        outbox = list(conn.execute("SELECT * FROM outbox WHERE case_id = ? ORDER BY event_id", (case_id,)))
        schedule = list(conn.execute("SELECT * FROM schedule WHERE case_id = ? ORDER BY schedule_id",
                                     (case_id,)))
    finally:
        conn.close()

    decisions = [{**dict(d), "detail_parsed": json.loads(d["detail"])} for d in decisions]
    outbox = [{**dict(o), "payload_parsed": json.loads(o["payload"])} for o in outbox]
    blocked = json.loads(c["blocked_phones"])
    return render_template("case.html", c=c, alerts=alerts, attempts=attempts, decisions=decisions,
                           zns=zns, outbox=outbox, schedule=schedule, blocked=blocked)


@app.route("/customer/<customer_id>")
def customer_journey(customer_id):
    conn = db()
    try:
        cases = list(conn.execute(
            "SELECT * FROM cases WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,)))
        if not cases:
            abort(404)
        case_ids = tuple(c["case_id"] for c in cases)
        marks = ", ".join("?" * len(case_ids))

        attempts = list(conn.execute(
            f"SELECT * FROM call_attempts WHERE case_id IN ({marks}) ORDER BY started_at",
            case_ids))
        decisions = list(conn.execute(
            f"SELECT d.*, l.label AS llm_label, l.confidence AS llm_conf "
            f"FROM decisions d LEFT JOIN llm_labels l ON l.attempt_id = d.attempt_id "
            f"WHERE d.case_id IN ({marks}) ORDER BY d.decided_at, d.decision_id",
            case_ids))
        zns = list(conn.execute(
            f"SELECT * FROM zns_log WHERE case_id IN ({marks}) ORDER BY created_at",
            case_ids))
    finally:
        conn.close()

    decisions = [{**dict(d), "detail_parsed": json.loads(d["detail"])} for d in decisions]
    return render_template("customer.html", customer_id=customer_id, cases=cases,
                           decisions=decisions, attempts=attempts, zns=zns)


@app.route("/api/webhook/zns-status", methods=["POST"])
def zns_status_webhook():
    """Nhận callback từ ZNS gateway khi KH nhận/đọc ZNS.

    Body JSON: {"msg_id": "...", "status": "delivered"|"read", "timestamp": "ISO8601"}
    """
    data = request.get_json(silent=True)
    if not data or "msg_id" not in data or "status" not in data:
        return jsonify({"error": "missing msg_id or status"}), 400
    status = data["status"]
    if status not in ("delivered", "read"):
        return jsonify({"error": "status must be 'delivered' or 'read'"}), 400
    ts_str = data.get("timestamp")
    at = datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
    if at.utcoffset() is None:
        at = at.replace(tzinfo=timezone.utc)

    conn = db()
    try:
        cfg = load_config()
        svc = CallbackService(conn, cfg) #truyen ham checker that vao khi deploy
        found = svc.handle_zns_webhook(data["msg_id"], status, at)
    finally:
        conn.close()
    if not found:
        return jsonify({"error": "unknown msg_id"}), 404
    return jsonify({"ok": True})


@app.route("/priority")
def priority_page():
    _load_priority_model()
    return render_template("priority.html", info=_priority_info)


@app.route("/api/priority", methods=["POST"])
def api_priority():
    _load_priority_model()
    data = request.get_json(silent=True) or {}
    cats = _priority_info["merchant_categories"]

    merchant_cat = data.get("merchant_category", "other")
    mcat_idx = cats.index(merchant_cat) if merchant_cat in cats else cats.index("other")

    hour = int(data.get("hour_of_day", 12))
    dow = int(data.get("day_of_week", 0))

    if hour >= 22 or hour < 6:
        period = 0
    elif hour < 12:
        period = 1
    elif hour < 18:
        period = 2
    else:
        period = 3

    cust_rate = float(data.get("customer_prev_reachable_rate", 0.5))
    cust_count = min(int(data.get("customer_ticket_count", 0)), 20)
    is_nnldl = 1 if data.get("is_nnldl") else 0
    has_note = 1 if data.get("has_note") else 0

    features = np.array([[mcat_idx, hour, dow, period, cust_rate, cust_count, is_nnldl, has_note]],
                        dtype=np.float32)
    prob = float(_priority_rf.predict_proba(features)[0, 1])

    if prob >= 0.6:
        bucket, action, color = "HIGH", "Gọi ngay", "ok"
    elif prob >= 0.4:
        bucket, action, color = "MEDIUM", "Gọi bình thường", "retry"
    elif prob >= 0.2:
        bucket, action, color = "LOW", "Hoãn lại / đợi giờ tốt hơn", "human"
    else:
        bucket, action, color = "SKIP", "Gửi ZNS thay vì gọi", "stop"

    return jsonify({
        "probability": round(prob, 3),
        "bucket": bucket,
        "action": action,
        "color": color,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
