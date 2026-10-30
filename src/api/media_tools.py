"""创作工具（图片处理）API。

设计要点：
- 本地工具（拼图/九宫格/旋转/翻转/加水印/裁剪）不走这里，由小程序 canvas 直接完成。
- 服务端工具统一走「上传 -> 建任务 -> 轮询结果」，避免长请求超时。
- 任务结果落盘 24 小时，文件用不可猜的 UUID 命名，通过 /api/v1/media-tools/files/ 访问。
"""
from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from flask import Blueprint, current_app, request, send_file
from PIL import Image, ImageDraw, ImageOps

from configs.logging_config import get_logger
from src.api.access import authenticate_wx_token
from src.api.response import make_response
from src.db import (
    beijing_today,
    consume_daily_quota,
    get_db,
    refund_daily_quota,
    refund_user_credit,
    reserve_user_credit,
    utcnow,
)

bp = Blueprint("media_tools", __name__)
logger = get_logger(__name__)

UPLOAD_MAX_BYTES = 12 * 1024 * 1024
REMOTE_MAX_BYTES = 12 * 1024 * 1024
MAX_FILES = 12
MAX_DIMENSION = 4096
JOB_TTL_HOURS = 24
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|gif|bmp|tiff)$", re.I)

IMAGE_FORMATS = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
    "TIFF": "tiff",
}

# tool_type -> 处理函数名（见 _PROCESSORS）
SERVER_TOOL_TYPES = {
    "image_batch_ratio": "_process_batch_ratio",
    "image_long_stitch": "_process_long_stitch",
    "image_round_corners": "_process_round_corners",
    "image_compress": "_process_compress",
    "image_convert": "_process_convert",
    "image_pad": "_process_pad",
    "image_resize": "_process_resize",
    "image_to_pdf": "_process_to_pdf",
    "image_to_gif": "_process_to_gif",
    "image_qr_code": "_process_qr_code",
}

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _base_dir() -> str:
    root = current_app.config.get("MEDIA_TOOLS_DIR") or os.path.join(current_app.instance_path, "media_tools")
    os.makedirs(os.path.join(root, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    return root


def _auth():
    access, error = authenticate_wx_token()
    if error:
        status, message, code = error
        return None, (make_response(status, message, None, False, code), status)
    if access is None and not current_app.config.get("API_ONLY"):
        return None, (make_response(401, "请先登录小程序", None, False, "WX_TOKEN_REQUIRED"), 401)
    return access, None


def _reserve_quota(access):
    """返回 (daily_day, daily_reserved, credit_reserved)；不足时抛 402。"""
    if not access or not access.get("user_id"):
        return None, False, False
    user_id = access["user_id"]
    cap = access.get("quota_cap")
    day = beijing_today()
    if consume_daily_quota(user_id, day, cap):
        return day, cap is not None, False
    reservation = reserve_user_credit(user_id)
    if reservation is None:
        from src.api.parse import ParseFailure
        raise ParseFailure(402, "DAILY_QUOTA_EXCEEDED", "今日额度已用完，可签到或开通会员获取更多")
    return day, False, reservation


def _refund_quota(access, day, daily_reserved, credit_reserved):
    if not access or not access.get("user_id"):
        return
    if daily_reserved and day:
        refund_daily_quota(access["user_id"], day)
    if credit_reserved:
        refund_user_credit(access["user_id"])


def _detect_format(raw: bytes):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            return image.format, image.size
    except Exception:
        return None, None


@bp.post("/v1/media-tools/uploads")
def upload_media():
    access, error = _auth()
    if error:
        return error
    file = request.files.get("file")
    if file is None:
        return make_response(400, "缺少文件", None, False, "FILE_REQUIRED"), 400
    raw = file.read(UPLOAD_MAX_BYTES + 1)
    if len(raw) > UPLOAD_MAX_BYTES:
        return make_response(400, "图片不能超过 12MB", None, False, "FILE_TOO_LARGE"), 400
    fmt, size = _detect_format(raw)
    if not fmt or fmt.upper() not in IMAGE_FORMATS:
        return make_response(400, "仅支持 JPG/PNG/WebP/GIF 图片", None, False, "UNSUPPORTED_IMAGE"), 400
    ext = IMAGE_FORMATS[fmt.upper()]
    file_id = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(_base_dir(), "uploads", file_id)
    with open(path, "wb") as handle:
        handle.write(raw)
    return make_response(200, "上传成功", {
        "fileId": file_id,
        "width": size[0],
        "height": size[1],
        "size": len(raw),
    }, True), 200


def _load_uploaded(file_id: str):
    if not FILE_ID_RE.match(file_id or ""):
        raise ValueError("非法文件标识")
    path = os.path.join(_base_dir(), "uploads", file_id)
    if not os.path.isfile(path):
        raise ValueError("上传文件不存在或已过期")
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGBA")


def _download_remote(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("非法图片链接")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("图片链接无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("不支持访问内网图片地址")
    response = requests.get(url, timeout=15, stream=True, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    response.raise_for_status()
    chunks, total = [], 0
    for chunk in response.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > REMOTE_MAX_BYTES:
            raise ValueError("远程图片超过 12MB")
        chunks.append(chunk)
    raw = b"".join(chunks)
    fmt, _ = _detect_format(raw)
    if not fmt:
        raise ValueError("远程链接不是有效图片")
    with Image.open(io.BytesIO(raw)) as image:
        return ImageOps.exif_transpose(image).convert("RGBA")


def _load_inputs(payload):
    images = []
    for file_id in payload.get("sourceFiles") or []:
        images.append(_load_uploaded(str(file_id)))
    for url in payload.get("sourceUrls") or []:
        images.append(_download_remote(str(url)))
    if not images:
        raise ValueError("请先选择图片")
    if len(images) > MAX_FILES:
        raise ValueError(f"一次最多处理 {MAX_FILES} 张图片")
    return images


def _fit_max(image: Image.Image) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= MAX_DIMENSION:
        return image
    scale = MAX_DIMENSION / longest
    return image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)


def _result_dir(job_id: str) -> str:
    path = os.path.join(_base_dir(), "results", job_id)
    os.makedirs(path, exist_ok=True)
    return path


def _save_result(image: Image.Image, job_id: str, index: int, fmt: str, *, quality: int | None = None):
    directory = _result_dir(job_id)
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(fmt.upper(), fmt.lower())
    name = f"{index}.{ext}"
    path = os.path.join(directory, name)
    if fmt.upper() == "JPEG":
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "RGBA":
            background.paste(image, mask=image.split()[-1])
        else:
            background.paste(image)
        background.save(path, "JPEG", quality=quality or 88, optimize=True)
    elif fmt.upper() == "GIF":
        image.convert("RGBA").save(path, "GIF")
    else:
        image.save(path, fmt.upper(), optimize=True)
    return {
        "file": name,
        "width": image.size[0],
        "height": image.size[1],
        "size": os.path.getsize(path),
    }


def _ratio_size(value: str, width: int, height: int):
    ratios = {
        "square_11": (1, 1),
        "portrait_3_4": (3, 4),
        "portrait_4_5": (4, 5),
        "portrait_9_16": (9, 16),
        "landscape_16_9": (16, 9),
    }
    return ratios.get(value, (1, 1))


def _fit_ratio(image: Image.Image, ratio, mode="crop"):
    rw, rh = ratio
    width, height = image.size
    target = rw / rh
    current = width / height
    if mode == "pad":
        if current > target:
            canvas_h = round(width / target)
            canvas = Image.new("RGBA", (width, canvas_h), (255, 255, 255, 255))
            canvas.paste(image, (0, (canvas_h - height) // 2))
            return canvas
        canvas_w = round(height * target)
        canvas = Image.new("RGBA", (canvas_w, height), (255, 255, 255, 255))
        canvas.paste(image, ((canvas_w - width) // 2, 0))
        return canvas
    if current > target:
        new_w = round(height * target)
        left = (width - new_w) // 2
        return image.crop((left, 0, left + new_w, height))
    new_h = round(width / target)
    top = (height - new_h) // 2
    return image.crop((0, top, width, top + new_h))


def _process_batch_ratio(images, options, job_id):
    mode = options.get("fitMode") or "crop"
    ratio = _ratio_size(options.get("ratio") or "square_11", *images[0].size)
    results = []
    for index, image in enumerate(images):
        fitted = _fit_max(_fit_ratio(image, ratio, mode))
        results.append(_save_result(fitted, job_id, index, "JPEG", quality=90))
    return results


def _process_long_stitch(images, options, job_id):
    direction = options.get("direction") or "vertical"
    gap = max(0, min(100, int(options.get("gap") or 0)))
    scaled = [_fit_max(image) for image in images]
    total_main = sum(image.size[0] if direction == "horizontal" else image.size[1] for image in scaled)
    total_main += gap * (len(scaled) - 1)
    if total_main > 30000:
        factor = 30000 / total_main
        scaled = [
            image.resize((max(1, round(image.size[0] * factor)), max(1, round(image.size[1] * factor))), Image.Resampling.LANCZOS)
            for image in scaled
        ]
        gap = max(0, round(gap * factor))
    if direction == "horizontal":
        width = sum(image.size[0] for image in scaled) + gap * (len(scaled) - 1)
        height = max(image.size[1] for image in scaled)
    else:
        width = max(image.size[0] for image in scaled)
        height = sum(image.size[1] for image in scaled) + gap * (len(scaled) - 1)
    canvas = Image.new("RGBA", (max(1, width), max(1, height)), (255, 255, 255, 255))
    cursor = 0
    for image in scaled:
        if direction == "horizontal":
            canvas.paste(image, (cursor, (height - image.size[1]) // 2))
            cursor += image.size[0] + gap
        else:
            canvas.paste(image, ((width - image.size[0]) // 2, cursor))
            cursor += image.size[1] + gap
    return [_save_result(canvas, job_id, 0, "JPEG", quality=90)]


def _process_round_corners(images, options, job_id):
    percent = max(1, min(50, int(options.get("radiusPercent") or 10)))
    results = []
    for index, image in enumerate(images):
        image = _fit_max(image)
        width, height = image.size
        radius = max(1, round(min(width, height) * percent / 100))
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
        output = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        output.paste(image, (0, 0), mask)
        results.append(_save_result(output, job_id, index, "PNG"))
    return results


def _process_compress(images, options, job_id):
    quality_map = {"high": 90, "standard": 75, "small": 55}
    quality = quality_map.get(options.get("quality") or "standard", 75)
    results = []
    for index, image in enumerate(images):
        image = _fit_max(image)
        results.append(_save_result(image, job_id, index, "JPEG", quality=quality))
    return results


def _process_convert(images, options, job_id):
    target = str(options.get("format") or "jpg").lower()
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF"}.get(target)
    if not fmt:
        raise ValueError("不支持的输出格式")
    results = []
    for index, image in enumerate(images):
        results.append(_save_result(_fit_max(image), job_id, index, fmt, quality=90))
    return results


def _process_pad(images, options, job_id):
    ratio = _ratio_size(options.get("ratio") or "square_11", *images[0].size)
    color = str(options.get("background") or "#ffffff")
    if not re.match(r"^#[0-9a-f]{6}$", color, re.I):
        color = "#ffffff"
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
    results = []
    for index, image in enumerate(images):
        fitted = _fit_ratio(_fit_max(image), ratio, "pad")
        canvas = Image.new("RGBA", fitted.size, rgb)
        canvas.paste(fitted, (0, 0), fitted)
        results.append(_save_result(canvas, job_id, index, "JPEG", quality=90))
    return results


def _process_resize(images, options, job_id):
    width = int(options.get("width") or 0)
    percent = int(options.get("percent") or 100)
    results = []
    for index, image in enumerate(images):
        image = _fit_max(image)
        if 100 <= width <= MAX_DIMENSION:
            scale = width / image.size[0]
            target = (width, max(1, round(image.size[1] * scale)))
        else:
            percent = max(5, min(400, percent))
            target = (max(1, round(image.size[0] * percent / 100)), max(1, round(image.size[1] * percent / 100)))
        results.append(_save_result(image.resize(target, Image.Resampling.LANCZOS), job_id, index, "JPEG", quality=92))
    return results


def _process_to_pdf(images, options, job_id):
    pages = []
    for image in images:
        rgb = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "RGBA":
            rgb.paste(image, mask=image.split()[-1])
        else:
            rgb.paste(image)
        pages.append(rgb)
    directory = _result_dir(job_id)
    path = os.path.join(directory, "0.pdf")
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:], resolution=144.0)
    return [{
        "file": "0.pdf",
        "width": pages[0].size[0],
        "height": pages[0].size[1],
        "size": os.path.getsize(path),
        "pages": len(pages),
    }]


def _process_to_gif(images, options, job_id):
    duration = max(40, min(2000, int(options.get("duration") or 200)))
    frames = []
    for image in images:
        image = _fit_max(image).convert("RGBA")
        frame = Image.new("RGB", image.size, (255, 255, 255))
        frame.paste(image, mask=image.split()[-1])
        frames.append(frame)
    target = frames[0].size
    frames = [frame.resize(target, Image.Resampling.LANCZOS) if frame.size != target else frame for frame in frames]
    directory = _result_dir(job_id)
    path = os.path.join(directory, "0.gif")
    frames[0].save(path, "GIF", save_all=True, append_images=frames[1:], duration=duration, loop=0, optimize=True)
    return [{
        "file": "0.gif",
        "width": frames[0].size[0],
        "height": frames[0].size[1],
        "size": os.path.getsize(path),
        "frames": len(frames),
    }]


def _process_qr_code(images, options, job_id):
    import qrcode
    text = str(options.get("text") or "").strip()
    if not text:
        raise ValueError("请填写要生成二维码的内容")
    if len(text) > 1000:
        raise ValueError("二维码内容不能超过 1000 字")
    size = max(256, min(1024, int(options.get("size") or 512)))
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    image = image.resize((size, size), Image.NEAREST)
    return [_save_result(image, job_id, 0, "PNG")]


def _get_processor(tool_type: str):
    name = SERVER_TOOL_TYPES.get(tool_type)
    if not name:
        raise ValueError("不支持的工具类型")
    return globals()[name]


def _set_job(job_id, **fields):
    db = get_db()
    keys = list(fields.keys())
    assignments = ", ".join(f"{key}=?" for key in keys)
    db.execute(
        f"UPDATE media_tool_jobs SET {assignments}, updated_at=? WHERE id=?",
        [fields[key] for key in keys] + [utcnow(), job_id],
    )
    db.commit()


def _run_job(app, job_id, tool_type, payload, quota=None):
    with app.app_context():
        try:
            _set_job(job_id, status="processing", stage="正在读取图片", progress=10)
            images = [] if tool_type == "image_qr_code" else _load_inputs(payload)
            _set_job(job_id, stage="正在处理", progress=40)
            processor = _get_processor(tool_type)
            results = processor(images, payload.get("options") or {}, job_id)
            _set_job(
                job_id,
                status="done",
                stage="处理完成",
                progress=100,
                result_json=json.dumps(results, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("media tool job failed: %s", job_id)
            _set_job(
                job_id,
                status="failed",
                stage="处理失败",
                progress=100,
                error_code="TOOL_PROCESS_FAILED",
                error_message=str(exc) or "处理失败，请重试",
            )
            if quota:
                _refund_quota(quota.get("access"), quota.get("day"), quota.get("daily"), quota.get("credit"))


def _executor_pool():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-tool")
    return _executor


@bp.post("/v1/media-tools/jobs")
def create_job():
    access, error = _auth()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    tool_type = str(data.get("toolType") or "").strip()
    if tool_type not in SERVER_TOOL_TYPES:
        return make_response(400, "不支持的工具类型", None, False, "UNSUPPORTED_TOOL"), 400
    source_files = data.get("sourceFiles") or []
    source_urls = data.get("sourceUrls") or []
    if not isinstance(source_files, list) or not isinstance(source_urls, list):
        return make_response(400, "素材格式不正确", None, False, "INVALID_SOURCE"), 400
    if tool_type != "image_qr_code" and not source_files and not source_urls:
        return make_response(400, "请先选择图片", None, False, "SOURCE_REQUIRED"), 400
    if len(source_files) + len(source_urls) > MAX_FILES:
        return make_response(400, f"一次最多处理 {MAX_FILES} 张图片", None, False, "TOO_MANY_FILES"), 400

    day = daily = credit = False
    try:
        day, daily, credit = _reserve_quota(access)
    except Exception as exc:
        status = getattr(exc, "status", 500)
        code = getattr(exc, "code", "INTERNAL_ERROR")
        message = getattr(exc, "message", str(exc) or "请求失败")
        return make_response(status, message, None, False, code), status

    _cleanup_expired()
    job_id = uuid.uuid4().hex
    payload = {
        "sourceFiles": source_files,
        "sourceUrls": source_urls,
        "options": data.get("options") or {},
        "sourceMode": data.get("sourceMode") or "local",
    }
    db = get_db()
    now = utcnow()
    expires = (datetime.now(timezone.utc) + timedelta(hours=JOB_TTL_HOURS)).isoformat()
    db.execute(
        "INSERT INTO media_tool_jobs(id,user_id,tool_type,status,progress,stage,input_json,created_at,updated_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (job_id, access["user_id"] if access else None, tool_type, "pending", 0, "排队中",
         json.dumps(payload, ensure_ascii=False), now, now, expires),
    )
    db.commit()

    quota = {"access": access, "day": day, "daily": daily, "credit": credit}
    if current_app.config.get("TESTING"):
        _run_job(current_app._get_current_object(), job_id, tool_type, payload, quota)
    else:
        _executor_pool().submit(_run_job, current_app._get_current_object(), job_id, tool_type, payload, quota)
    return make_response(200, "任务已创建", {"id": job_id, "status": "pending"}, True), 200


def _job_payload(row):
    results = []
    for item in json.loads(row["result_json"] or "[]"):
        item = dict(item)
        item["url"] = f"/api/v1/media-tools/files/results/{row['id']}/{item['file']}"
        item["fileName"] = item.get("fileName") or item["file"]
        results.append(item)
    return {
        "id": row["id"],
        "toolType": row["tool_type"],
        "status": row["status"],
        "progress": row["progress"],
        "stage": row["stage"] or "",
        "errorCode": row["error_code"],
        "errorMessage": row["error_message"],
        "results": results,
    }


@bp.get("/v1/media-tools/jobs/<job_id>")
def get_job(job_id):
    access, error = _auth()
    if error:
        return error
    db = get_db()
    row = db.execute("SELECT * FROM media_tool_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return make_response(404, "任务不存在", None, False, "JOB_NOT_FOUND"), 404
    if access and access.get("user_id") and row["user_id"] not in (None, access["user_id"]):
        return make_response(403, "无权查看该任务", None, False, "FORBIDDEN"), 403
    return make_response(200, "ok", _job_payload(row), True), 200


@bp.get("/v1/media-tools/files/<path:filename>")
def serve_file(filename):
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        return make_response(400, "非法路径", None, False, "INVALID_PATH"), 400
    path = os.path.join(_base_dir(), filename.replace("/", os.sep))
    if not os.path.isfile(path):
        return make_response(404, "文件不存在或已过期", None, False, "FILE_NOT_FOUND"), 404
    return send_file(path)


def _cleanup_expired():
    """懒清理：每次建任务时清掉超过 TTL 的任务与文件。"""
    try:
        db = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=JOB_TTL_HOURS)).isoformat()
        rows = db.execute("SELECT id FROM media_tool_jobs WHERE created_at < ?", (cutoff,)).fetchall()
        for row in rows:
            shutil.rmtree(os.path.join(_base_dir(), "results", row["id"]), ignore_errors=True)
        db.execute("DELETE FROM media_tool_jobs WHERE created_at < ?", (cutoff,))
        upload_cutoff = time.time() - JOB_TTL_HOURS * 3600
        for name in os.listdir(os.path.join(_base_dir(), "uploads")):
            path = os.path.join(_base_dir(), "uploads", name)
            try:
                if os.path.getmtime(path) < upload_cutoff:
                    os.remove(path)
            except OSError:
                pass
        db.commit()
    except Exception:
        logger.exception("media tool cleanup failed")
