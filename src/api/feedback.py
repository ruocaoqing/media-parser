import os

import requests
from flask import Blueprint, request

from src.api.access import consume_rate_limit, get_client_ip
from src.api.response import make_response

bp = Blueprint("feedback", __name__)

FEEDBACK_TYPES = {
    "parse": "解析失败",
    "download": "下载失败",
    "advice": "功能建议",
    "other": "其他问题",
}


@bp.post("/v1/feedback")
def submit_feedback():
    webhook_url = os.getenv("FEEDBACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return make_response(503, "反馈服务暂未配置", None, False, "FEEDBACK_NOT_CONFIGURED"), 503
    if not consume_rate_limit(f"feedback:{get_client_ip()}", 5, 60):
        return make_response(429, "反馈提交过于频繁，请稍后重试", None, False, "RATE_LIMITED"), 429

    data = request.get_json(silent=True) or {}
    type_key = str(data.get("type") or "").strip()
    description = str(data.get("description") or "").strip()
    if type_key not in FEEDBACK_TYPES or not description or len(description) > 300:
        return make_response(400, "请选择问题类型，并填写 1–300 字的问题描述", None, False, "INVALID_FEEDBACK"), 400

    client_id = str(data.get("clientId") or "").strip()[:80]
    version = str(data.get("version") or "").strip()[:40]
    content = "\n".join([
        "【硬核去水印王 · 意见反馈】",
        "来源：浏览器插件",
        f"问题类型：{FEEDBACK_TYPES[type_key]}",
        f"问题描述：{description}",
        f"客户端 ID：{client_id or '未提供'}",
        f"客户端版本：v{version or '未知'}",
    ])
    try:
        response = requests.post(
            webhook_url,
            json={"msgtype": "text", "text": {"content": content}},
            timeout=10,
        )
        payload = response.json()
        sent = response.status_code == 200 and payload.get("errcode") == 0
    except Exception:
        sent = False
    if not sent:
        return make_response(502, "反馈暂未送达，请稍后重试", None, False, "FEEDBACK_DELIVERY_FAILED"), 502
    return make_response(200, "成功", {"sent": True}, True), 200
