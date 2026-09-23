import base64
import hashlib
import json
import math
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, request, send_file
from configs.logging_config import get_logger

from src.api.access import authenticate_wx_token, consume_rate_limit, get_client_ip
from src.api.response import make_response
from src.db import (
    DEFAULT_CHECKIN_BONUS,
    beijing_now,
    beijing_today,
    daily_quota_cap,
    get_daily_usage,
    get_db,
    get_member_plan,
    is_member,
    parse_utc,
    setting,
    transaction,
    utcnow,
)
from src.wx_crypto import decrypt_session_key, encrypt_session_key

bp = Blueprint('wx', __name__)
logger = get_logger(__name__)

CODE2SESSION_URL = 'https://api.weixin.qq.com/sns/jscode2session'
TOKEN_TTL_DAYS = 30
AVATAR_MAX_BYTES = 2 * 1024 * 1024
LOGIN_RATE_LIMIT = 10
# 不可登录的占位 hash：username 是 NOT NULL，但小程序用户永远不走密码登录
UNUSABLE_PASSWORD_HASH = '!wx-silent-login-no-password'
IMAGE_MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'RIFF', 'image/webp'),
)


def wx_configured():
    return bool(current_app.config.get('WX_APPID')) and bool(current_app.config.get('WX_APPSECRET'))


def _hash_token(token):
    """token 在服务端只用于比对，从不参与加密运算，所以只存哈希（§1.3）。"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def code2session(code):
    """用 code 换 openid / session_key。独立成函数，测试里 patch 它，绝不真打微信接口。"""
    query = urllib.parse.urlencode({
        'appid': current_app.config.get('WX_APPID'),
        'secret': current_app.config.get('WX_APPSECRET'),
        'js_code': code,
        'grant_type': 'authorization_code',
    })
    with urllib.request.urlopen(f'{CODE2SESSION_URL}?{query}', timeout=5) as response:
        return json.loads(response.read().decode('utf-8'))


def _default_qps():
    try:
        return max(1, int(setting('default_user_qps', 2)))
    except (TypeError, ValueError):
        return 2


def _sniff_image_mime(payload):
    if payload[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if payload[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _avatar_dir():
    """头像目录。默认 `app.instance_path/avatars`，测试用 `AVATAR_DIR` 指到临时目录。

    不能直接写 `current_app.instance_path`：它对**每个** app 都是 `<仓库>/data`
    （app.py 的 `data_dir` 是按 `__file__` 算出来的绝对路径，测试传的临时 DATABASE 改不了它）。
    临时库的用户 id 每次从 1 开始，于是 `data/avatars/1.jpg` 会跨用例、跨运行残留，
    让「没传过头像 → avatar 为空串」这条断言在第二次运行时就变红。
    """
    return current_app.config.get('AVATAR_DIR') or os.path.join(current_app.instance_path, 'avatars')


def _user_snapshot(user):
    """客户端唯一的数据来源（§3.4）。服务端把一切算好，客户端只渲染。"""
    today = beijing_today()
    member = is_member(user)
    plan = get_member_plan(user['member_plan']) if member else None
    cap = daily_quota_cap(user)
    used = get_daily_usage(user['id'], today)
    expires = parse_utc(user['member_expires_at']) if member else None
    days_left = None
    if expires is not None:
        days_left = max(0, math.ceil((expires - datetime.now(timezone.utc)).total_seconds() / 86400))
    # 头像是「有文件才给地址」：/wx/avatar/<id> 没落过盘时返回 404，小程序 <image>
    # 拿到 404 只会显示一片空白，wxml 里 wx:else 的默认头像分支反而永远走不到。
    # 按文件是否存在判断（而不是看 users.avatar 列）：换机器/删 instance 目录后
    # 列里可能还留着旧地址，那时按列判断就会又白屏一次。
    avatar_path = os.path.join(_avatar_dir(), f"{user['id']}.jpg")
    return {
        'id': user['id'],
        'nickname': user['nickname'] or '',
        'avatar': f"/api/v1/wx/avatar/{user['id']}" if os.path.exists(avatar_path) else '',
        'member': member,
        'member_plan': plan['code'] if plan else None,
        'member_plan_name': plan['name'] if plan else None,
        'member_expires_at': user['member_expires_at'] if plan else None,
        'member_days_left': days_left,
        'daily_quota': cap,
        'used_today': used,
        'remaining_today': max(0, cap - used),
        'balance': user['credits'],
    }


@bp.post('/v1/wx/login')
def login():
    if not wx_configured():
        return make_response(503, '服务端未配置微信登录凭证', None, False, 'WX_NOT_CONFIGURED'), 503
    # 登录接口本身不限流就会变成 code2Session 额度的放大器（§3.1 的同一风险）
    if not consume_rate_limit(f"wx_login:{get_client_ip()}", LOGIN_RATE_LIMIT, window_seconds=60):
        return make_response(429, '登录过于频繁，请稍后重试', None, False, 'RATE_LIMITED'), 429

    payload = request.get_json(silent=True)
    code = str((payload or {}).get('code', '')).strip() if isinstance(payload, dict) else ''
    if not code:
        return make_response(400, '缺少 code 参数', None, False, 'WX_CODE_INVALID'), 400

    try:
        session = code2session(code)
    except Exception:
        logger.exception('code2session 请求失败')
        return make_response(400, '微信登录凭证校验失败，请重试', None, False, 'WX_CODE_INVALID'), 400

    openid = (session or {}).get('openid')
    if not openid:
        logger.warning(f'code2session 未返回 openid: {session}')
        return make_response(400, '微信登录凭证无效或已过期，请重试', None, False, 'WX_CODE_INVALID'), 400

    now = utcnow()
    with transaction(immediate=True) as db:
        user = db.execute('SELECT * FROM users WHERE openid=?', (openid,)).fetchone()
        if user is None:
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (f'wx_{openid}', UNUSABLE_PASSWORD_HASH, 'user', 1,
                 _default_qps(), 0, openid, now),
            )
            user = db.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()

        # 与网页登录故意不同：小程序没有「登进去看看」的场景，停用要在这一步就说清（§2.3）
        if not user['active']:
            return make_response(403, '账号已被停用，请联系客服', None, False, 'ACCOUNT_DISABLED'), 403

        raw_token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat(timespec='seconds')
        db.execute(
            "INSERT INTO wx_sessions(token_hash,user_id,session_key,created_at,expires_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?)",
            (_hash_token(raw_token),
             user['id'],
             encrypt_session_key((session or {}).get('session_key', ''), current_app.config['SECRET_KEY']),
             now, expires_at, now),
        )
        db.execute('DELETE FROM wx_sessions WHERE expires_at < ?', (now,))
        snapshot = _user_snapshot(user)

    logger.info(f'微信登录成功 user_id={snapshot["id"]}')
    return make_response(200, '成功', {'token': raw_token, 'expires_at': expires_at, 'user': snapshot}, True), 200


def _auth_or_error():
    """共用的鉴权前置：返回 (access, None) 或 (None, response)。"""
    access, error = authenticate_wx_token()
    if error:
        status, message, code = error
        return None, (make_response(status, message, None, False, code), status)
    if access is None:
        return None, (make_response(401, '缺少登录令牌', None, False, 'WX_TOKEN_INVALID'), 401)
    return access, None


@bp.get('/v1/wx/me')
def me():
    access, failure = _auth_or_error()
    if failure:
        return failure
    user = get_db().execute('SELECT * FROM users WHERE id=?', (access['user_id'],)).fetchone()
    return make_response(200, '成功', {'user': _user_snapshot(user)}, True), 200


@bp.post('/v1/wx/checkin')
def checkin():
    access, failure = _auth_or_error()
    if failure:
        return failure

    today = beijing_today()
    yesterday = (beijing_now().date() - timedelta(days=1)).isoformat()
    try:
        bonus = max(1, int(setting('wx_checkin_bonus', DEFAULT_CHECKIN_BONUS)))
    except (TypeError, ValueError):
        bonus = DEFAULT_CHECKIN_BONUS

    with transaction(immediate=True) as db:
        user = db.execute('SELECT * FROM users WHERE id=?', (access['user_id'],)).fetchone()
        if user['last_checkin_date'] == today:
            return make_response(409, '今天已经签到过了', None, False, 'ALREADY_CHECKED_IN'), 409
        streak = (int(user['checkin_streak'] or 0) + 1) if user['last_checkin_date'] == yesterday else 1
        db.execute(
            'UPDATE users SET last_checkin_date=?, checkin_streak=?, credits=credits+? WHERE id=?',
            (today, streak, bonus, user['id']),
        )
        balance = db.execute('SELECT credits FROM users WHERE id=?', (user['id'],)).fetchone()['credits']

    return make_response(200, '签到成功',
                         {'streak': streak, 'bonus': bonus, 'balance': balance}, True), 200


def _store_avatar(user_id, file_storage):
    """保存客户端上传的头像字节（§1.7）。

    注意：收的是**文件字节**，不是 URL —— chooseAvatar 交给客户端的是 wxfile://
    本机临时路径，服务端下载不了它。
    """
    payload = file_storage.read(AVATAR_MAX_BYTES + 1)
    if not payload:
        return False, '头像文件为空'
    if len(payload) > AVATAR_MAX_BYTES:
        return False, '头像文件超过 2MB'
    if _sniff_image_mime(payload) is None:
        return False, '请上传 jpg / png / webp 格式的图片'

    directory = _avatar_dir()
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f'{user_id}.jpg'), 'wb') as handle:
        handle.write(payload)

    db = get_db()
    db.execute('UPDATE users SET avatar=? WHERE id=?', (f'/api/v1/wx/avatar/{user_id}', user_id))
    db.commit()
    return True, 'ok'


@bp.post('/v1/wx/profile')
def profile():
    """更新昵称与头像。multipart（带头像）与 JSON（只改昵称）两种提交都收。"""
    access, failure = _auth_or_error()
    if failure:
        return failure

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        nickname = str(payload.get('nickname') or '')
        upload = None
    else:
        nickname = str(request.form.get('nickname') or '')
        upload = request.files.get('avatar')

    user_id = access['user_id']
    nickname = nickname.strip()[:32]
    if nickname:
        db = get_db()
        db.execute('UPDATE users SET nickname=? WHERE id=?', (nickname, user_id))
        db.commit()

    if upload is not None:
        ok, message = _store_avatar(user_id, upload)
        if not ok:
            return make_response(400, message, None, False, 'AVATAR_SAVE_FAILED'), 400

    user = get_db().execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    return make_response(200, '成功', {'user': _user_snapshot(user)}, True), 200


@bp.get('/v1/wx/avatar/<int:user_id>')
def avatar(user_id):
    """头像字节流。本身就是公开材料，无需 token（§2.1）。"""
    path = os.path.join(_avatar_dir(), f'{user_id}.jpg')
    if not os.path.exists(path):
        return make_response(404, '头像不存在', None, False, 'AVATAR_NOT_FOUND'), 404
    with open(path, 'rb') as handle:
        head = handle.read(16)
    return send_file(path, mimetype=_sniff_image_mime(head) or 'application/octet-stream')


@bp.get('/v1/wx/member-plans')
def member_plans():
    """四档卡的展示数据。不含价格与任何敏感信息，无需 token（§3.3）。"""
    rows = get_db().execute(
        "SELECT code, name, days, daily_quota FROM member_plans WHERE active=1 ORDER BY sort_order, code"
    ).fetchall()
    return make_response(200, '成功', {
        'plans': [dict(row) for row in rows]
    }, True), 200


# ---------------------------------------------------------------------------
# 插件扫码登录（设备码流程）
#
#   插件                后端                        小程序
#   -- POST /pair/start --> 生成配对码 + 小程序码
#   <-- 二维图 + 配对码 --
#   -- GET /pair/status 轮询 -->
#                        <-- pending --
#                          用户扫码打开 pairing 页（scene 携带配对码）
#                          -- POST /pair/confirm + X-WX-Token -->
#                        铸一枚**独立**的 30 天会话写进 wx_sessions
#                          <-- confirmed --
#   -- GET /pair/status -->
#                        <-- confirmed + token（只发这一次）--
#
# 与小程序登录同一个 wx_sessions 表、同一个 X-WX-Token 头、同一套额度：
# 插件解析消耗的就是用户本人的每日额度。独立成行是为了能单独吊销
#（插掉插件不影响手机上的登录）。gunicorn 跑 2 个 worker、内存不共享，
# 所以配对状态必须落库（ext_pairings），不能放进程内字典。
# ---------------------------------------------------------------------------

ACCESS_TOKEN_URL = 'https://api.weixin.qq.com/cgi-bin/token'
QRCODE_URL = 'https://api.weixin.qq.com/wxa/getwxacodeunlimit'
PAIRING_PAGE = 'pages/pairing/pairing'   # 扫码后落地的小程序页面（本仓库 miniprogram/ 侧同名校验）
PAIRING_TTL_SECONDS = 5 * 60
PAIRING_START_RATE_LIMIT = 10
PAIRING_STATUS_RATE_LIMIT = 60
# 31 字符表：去掉 0/O、1/I/L 这几对肉眼分不清的字符，扫码场景里抄错码没有重试机会
PAIRING_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'

# access_token 的进程内缓存。微信的门票 7200s 有效，但**新票一出旧票立刻作废**，
# 且获取接口有频率上限 —— 必须缓存复用，绝不能每次生码都去换一张。
# 两个 gunicorn worker 各存各的：最坏情况是每 2 小时多换一次票，可接受。
_access_token_state = {'value': None, 'good_until': 0.0}
_access_token_lock = threading.Lock()


def _request_access_token():
    """真打微信换门票。独立成函数，测试 patch 它，绝不真连（与 code2session 同一规矩）。"""
    query = urllib.parse.urlencode({
        'grant_type': 'client_credential',
        'appid': current_app.config.get('WX_APPID'),
        'secret': current_app.config.get('WX_APPSECRET'),
    })
    with urllib.request.urlopen(f'{ACCESS_TOKEN_URL}?{query}', timeout=5) as response:
        return json.loads(response.read().decode('utf-8'))


def get_access_token(force_refresh=False):
    """getwxacodeunlimit 要的 access_token（7200s 有效的微信接口门票）。

    到期前 5 分钟就视为过期：宁可有 5 分钟的余量，也不拿着一张在微信侧
    可能已被新票顶掉的旧票去生码（那会稳定地 40001）。
    """
    now = time.time()
    with _access_token_lock:
        cached = _access_token_state['value']
        if cached and not force_refresh and now < _access_token_state['good_until']:
            return cached
        payload = _request_access_token()
        token = (payload or {}).get('access_token')
        if not token:
            logger.warning(f'access_token 获取失败: {payload}')
            raise RuntimeError('access_token 获取失败')
        expires_in = int(payload.get('expires_in') or 7200)
        _access_token_state['value'] = token
        _access_token_state['good_until'] = now + max(60, expires_in - 300)
        return token


def fetch_pairing_qrcode(scene):
    """拿一张不限量小程序码的 PNG 字节。scene 里嵌配对码（≤32 字符限制）。

    成功时微信回 image 字节，失败时回 JSON（errcode/errmsg）—— 用首字节
    是不是 `{` 分流。40001/42001 表示门票失效：强制换票重试**一次**，
    再失败就把异常抛给调用方转 502。
    """
    body = json.dumps({
        'scene': scene,
        'page': PAIRING_PAGE,
        # 页面还没发正式版时路径校验必然失败，check_path 必须关
        'check_path': False,
        # release=正式版 / trial=体验版 / develop=开发版。默认 release（生产行为），
        # 联调期由 WX_QR_ENV_VERSION 指到 trial —— 小程序没发布前只有它能被扫开。
        'env_version': current_app.config.get('WX_QR_ENV_VERSION', 'release'),
        'width': 430,
    }).encode('utf-8')
    for attempt in (1, 2):
        token = get_access_token(force_refresh=(attempt == 2))
        req = urllib.request.Request(
            f'{QRCODE_URL}?access_token={token}',
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = response.read()
        if payload[:1] != b'{':
            return payload
        info = json.loads(payload.decode('utf-8'))
        if attempt == 1 and info.get('errcode') in (40001, 42001):
            continue
        raise RuntimeError(f'小程序码生成失败: {info}')
    raise RuntimeError('小程序码生成失败')


def _new_pairing_code():
    return ''.join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))


@bp.post('/v1/wx/pair/start')
def pair_start():
    """插件点「立即登录」时调用：返回小程序码 + 配对码，5 分钟内有效。"""
    if not wx_configured():
        return make_response(503, '服务端未配置微信登录凭证', None, False, 'WX_NOT_CONFIGURED'), 503
    # 每次调用都真打一次微信的生码接口，和 /wx/login 一样必须限流防放大
    if not consume_rate_limit(f"wx_pair_start:{get_client_ip()}", PAIRING_START_RATE_LIMIT, window_seconds=60):
        return make_response(429, '请求过于频繁，请稍后重试', None, False, 'RATE_LIMITED'), 429

    code = _new_pairing_code()
    now = utcnow()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat(timespec='seconds')
    try:
        png = fetch_pairing_qrcode(f'c={code}')
    except Exception:
        logger.exception('getwxacodeunlimit 请求失败')
        return make_response(502, '小程序码生成失败，请稍后重试', None, False, 'WX_QR_FAILED'), 502

    with transaction(immediate=True) as db:
        db.execute(
            "INSERT INTO ext_pairings(code_hash,status,created_at,expires_at) VALUES(?,?,?,?)",
            (_hash_token(code), 'pending', now, expires_at),
        )
        # 顺手清掉过期的配对行：这表每 5 分钟一批新行，不清会无限长
        db.execute('DELETE FROM ext_pairings WHERE expires_at < ?', (now,))

    return make_response(200, '成功', {
        'pairing_code': code,
        'qr_png': base64.b64encode(png).decode('ascii'),
        'expires_in': PAIRING_TTL_SECONDS,
    }, True), 200


@bp.post('/v1/wx/pair/confirm')
def pair_confirm():
    """小程序 pairing 页调用：带自己的 X-WX-Token 确认配对码，为插件铸一枚独立会话。"""
    access, failure = _auth_or_error()
    if failure:
        return failure

    payload = request.get_json(silent=True)
    code = str((payload or {}).get('pairing_code', '')).strip() if isinstance(payload, dict) else ''
    if not code:
        return make_response(400, '缺少 pairing_code 参数', None, False, 'PAIRING_CODE_INVALID'), 400

    now = utcnow()
    code_hash = _hash_token(code)
    with transaction(immediate=True) as db:
        row = db.execute('SELECT * FROM ext_pairings WHERE code_hash=?', (code_hash,)).fetchone()
        if row is None or row['expires_at'] <= now:
            return make_response(404, '配对码不存在或已过期', None, False, 'PAIRING_NOT_FOUND'), 404
        if row['status'] != 'pending':
            return make_response(409, '该配对码已被使用，请在插件上重新发起', None, False, 'PAIRING_ALREADY_CONFIRMED'), 409

        raw_token = secrets.token_urlsafe(32)
        token_expires_at = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat(timespec='seconds')
        # 插件会话没有 session_key（那东西来自 wx.login 的 code，插件拿不到），
        # 存空串：加密函数对空串恒等往返（tests 里已钉死），列又是 NOT NULL。
        db.execute(
            "INSERT INTO wx_sessions(token_hash,user_id,session_key,created_at,expires_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?)",
            (_hash_token(raw_token), access['user_id'], '', now, token_expires_at, now),
        )
        # 令牌明文要经 status 轮询交给插件，但 DB 只存哈希的规矩不能破 —— 加密短存，
        # 发放（consumed）时随行清除
        db.execute(
            "UPDATE ext_pairings SET status='confirmed', user_id=?, token_hash=?, token_enc=? WHERE code_hash=?",
            (access['user_id'], _hash_token(raw_token),
             encrypt_session_key(raw_token, current_app.config['SECRET_KEY']), code_hash),
        )

    logger.info(f'插件配对确认 user_id={access["user_id"]}')
    return make_response(200, '成功', {'status': 'confirmed'}, True), 200


@bp.get('/v1/wx/pair/status')
def pair_status():
    """插件每 2 秒轮询：pending / confirmed(附令牌，仅一次) / consumed。"""
    code = str(request.args.get('pairing_code', '')).strip()
    if not code:
        return make_response(400, '缺少 pairing_code 参数', None, False, 'PAIRING_CODE_INVALID'), 400
    # 配对码只有 8 位，轮询口不限流就是枚举口
    if not consume_rate_limit(f"wx_pair_status:{get_client_ip()}", PAIRING_STATUS_RATE_LIMIT, window_seconds=60):
        return make_response(429, '请求过于频繁，请稍后重试', None, False, 'RATE_LIMITED'), 429

    code_hash = _hash_token(code)
    row = get_db().execute('SELECT * FROM ext_pairings WHERE code_hash=?', (code_hash,)).fetchone()
    if row is None:
        return make_response(404, '配对码不存在或已失效', None, False, 'PAIRING_NOT_FOUND'), 404
    if row['expires_at'] <= utcnow():
        return make_response(410, '配对码已过期，请在插件上重新发起', None, False, 'PAIRING_EXPIRED'), 410
    if row['status'] in ('pending', 'consumed'):
        return make_response(200, '成功', {'status': row['status']}, True), 200

    # confirmed：令牌只发这一次。UPDATE ... WHERE status='confirmed' 的 rowcount
    # 是并发闸门 —— 两个轮询同时到达时只有一个能领到，另一个看到 consumed。
    with transaction(immediate=True) as db:
        claimed = db.execute(
            "UPDATE ext_pairings SET status='consumed', token_enc=NULL WHERE code_hash=? AND status='confirmed'",
            (code_hash,),
        )
        if claimed.rowcount != 1:
            return make_response(200, '成功', {'status': 'consumed'}, True), 200

    token = decrypt_session_key(row['token_enc'], current_app.config['SECRET_KEY'])
    session_row = get_db().execute(
        'SELECT expires_at FROM wx_sessions WHERE token_hash=?', (row['token_hash'],)
    ).fetchone()
    user = get_db().execute('SELECT * FROM users WHERE id=?', (row['user_id'],)).fetchone()
    return make_response(200, '成功', {
        'status': 'confirmed',
        'token': token,
        'expires_at': session_row['expires_at'] if session_row else None,
        'user': _user_snapshot(user),
    }, True), 200
