"""Nguồn trigger: biến dữ liệu bên ngoài (webhook TWO, bảng lịch trả nợ, sao kê) thành Alert.

    push: POST /api/alerts (app.py)  -> payload.alert_from_payload -> CallbackService.ingest_alert
    pull: triggers.pull_job          -> CSV/bảng đến hạn            -> CallbackService.ingest_alert

Nguồn thật chưa có; hiện đọc CSV trong mock_data/. Đổi nguồn = thay hàm đọc rows, hợp đồng dữ liệu giữ nguyên.
"""
from .payload import AlertIn, alert_from_payload
