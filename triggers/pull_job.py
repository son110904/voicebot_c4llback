"""Job pull theo lịch cho loan_due / statement_due.

Chạy mỗi sáng (cron/Task Scheduler). Đọc danh sách khoản đến hạn, giữ những khoản trong cửa sổ
[-max_overdue_days, +lookahead_days] so với hôm nay, tạo Alert với alert_id = CAMPAIGN-contract_ref-due_date
(chạy lại cùng ngày hay ngày sau không tạo case trùng vì ingest_alert bỏ qua alert_id đã có).

Nguồn hiện là CSV mock_data/*.csv với cột: contract_ref, customer_id, phone, phone_sms, amount_vnd, due_date.
Khi có bảng thật: viết hàm trả về list[dict] cùng cột rồi truyền vào run_pull - hợp đồng không đổi.

    python -m triggers.pull_job --campaign loan_due --csv mock_data/loan_due.csv [--date 2026-11-16] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from callback.config import Config, load_config
from callback.models import CampaignType

from .payload import AlertIn, alert_from_payload

_PULL_CAMPAIGNS = (CampaignType.LOAN_DUE, CampaignType.STATEMENT_DUE)


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def rows_to_alerts(campaign: CampaignType, rows: Iterable[Mapping[str, Any]], now: datetime,
                   cfg: Config) -> tuple[list[AlertIn], list[str]]:
    """Lọc theo cửa sổ ngày rồi dựng AlertIn. Trả (danh sách hợp lệ, danh sách lỗi theo dòng)."""
    if campaign not in _PULL_CAMPAIGNS:
        raise ValueError(f"{campaign.value} không phải campaign pull")
    camp = cfg.campaigns[campaign]
    today = now.astimezone(cfg.tz).date()
    kept: list[AlertIn] = []
    errors: list[str] = []
    for i, r in enumerate(rows, start=2):  # dòng 1 là header
        try:
            due = date.fromisoformat(str(r.get("due_date", "")).strip()[:10])
        except ValueError:
            errors.append(f"dòng {i}: due_date không hợp lệ {r.get('due_date')!r}")
            continue
        days_left = (due - today).days
        if days_left > camp.lookahead_days or -days_left > camp.max_overdue_days:
            continue
        ref = str(r.get("contract_ref", "")).strip()
        payload = {
            "campaign": campaign.value,
            "alert_id": f"{campaign.value.upper()}-{ref}-{due.isoformat()}",
            "card_ref": ref,
            "customer_id": r.get("customer_id"),
            "phone": r.get("phone"),
            "phone_sms": r.get("phone_sms"),
            "amount_vnd": r.get("amount_vnd", 0),
            "due_date": due.isoformat(),
            "created_at": now.isoformat(),
        }
        try:
            kept.append(alert_from_payload(payload, now))
        except ValueError as e:
            errors.append(f"dòng {i}: {e}")
    return kept, errors


def run_pull(svc, campaign: CampaignType, rows: Iterable[Mapping[str, Any]], now: datetime,
             cfg: Config) -> dict[str, Any]:
    """Đẩy từng AlertIn vào CallbackService. Trả thống kê để log."""
    alerts, errors = rows_to_alerts(campaign, rows, now, cfg)
    created = merged = duplicate = 0
    for a in alerts:
        r = svc.ingest_alert(a.alert, a.phone, now, phone_sms=a.phone_sms)
        if r.duplicate:
            duplicate += 1
        elif r.merged:
            merged += 1
        else:
            created += 1
    return {"campaign": campaign.value, "candidates": len(alerts), "created": created,
            "merged": merged, "duplicate": duplicate, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Job pull nhắc nợ / sao kê từ CSV mock")
    p.add_argument("--campaign", required=True, choices=[c.value for c in _PULL_CAMPAIGNS])
    p.add_argument("--csv", required=True, help="file CSV nguồn (mock_data/*.csv)")
    p.add_argument("--date", help="coi hôm nay là ngày này (YYYY-MM-DD), mặc định hôm nay")
    p.add_argument("--dry-run", action="store_true", help="chỉ in ra, không ghi DB")
    args = p.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config()
    campaign = CampaignType(args.campaign)
    if args.date:
        now = datetime.combine(date.fromisoformat(args.date), datetime.min.time().replace(hour=8), tzinfo=cfg.tz)
    else:
        now = datetime.now(timezone.utc)
    rows = read_csv(args.csv)

    if args.dry_run:
        alerts, errors = rows_to_alerts(campaign, rows, now, cfg)
        for a in alerts:
            print(f"  {a.alert.alert_id}  KH={a.alert.customer_id}  due={a.alert.due_date}  "
                  f"phone={a.phone}  amount={a.alert.amount_vnd:,}")
        for e in errors:
            print("  LỖI", e)
        print(f"{len(alerts)} khoản trong cửa sổ, {len(errors)} dòng lỗi (dry-run, không ghi DB)")
        return 0

    from db import CallbackService, connect
    conn = connect(os.environ.get("CALLBACK_DB_URL", ""))
    try:
        svc = CallbackService(conn, cfg, config_version="pull-job")
        stat = run_pull(svc, campaign, rows, now, cfg)
    finally:
        conn.close()
    print(f"{stat['campaign']}: {stat['candidates']} khoản trong cửa sổ -> tạo {stat['created']}, "
          f"gộp {stat['merged']}, trùng {stat['duplicate']}, lỗi {len(stat['errors'])}")
    for e in stat["errors"]:
        print("  LỖI", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
