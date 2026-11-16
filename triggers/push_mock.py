"""Giả lập TWO/TWFA đẩy cảnh báo thẻ vào POST /api/alerts.

Đọc mock_data/two_alerts.csv (cột: alert_id, card_ref, customer_id, phone, phone_sms, amount_vnd,
approved, card_blocked, created_at) và gửi từng dòng. Khi bên trung tâm viết phía gửi thật, họ gửi
đúng JSON này.

    python -m triggers.push_mock [--url http://localhost:5000/api/alerts] [--csv mock_data/two_alerts.csv]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .pull_job import read_csv


def post(url: str, payload: dict, api_key: str | None) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if api_key:
        req.add_header("X-Api-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Giả lập TWO đẩy cảnh báo thẻ")
    p.add_argument("--url", default="http://localhost:5000/api/alerts")
    p.add_argument("--csv", default="mock_data/two_alerts.csv")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    api_key = os.environ.get("ALERT_API_KEY")

    for row in read_csv(args.csv):
        payload = {k: v for k, v in row.items() if v != ""}
        payload["campaign"] = "card_fraud"
        status, data = post(args.url, payload, api_key)
        print(f"{row['alert_id']}: HTTP {status} {json.dumps(data, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
