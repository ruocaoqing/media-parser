import os
import secrets
import fcntl
from datetime import timedelta
from flask import Flask
from src.api.parse import bp as api_bp
from src.api.wx import bp as wx_bp
from src.api.media_tools import bp as media_tools_bp
from src.web.views import bp as web_bp
from src.auth import bp as auth_bp, register_template_helpers
from src.web.portal import bp as portal_bp
from src.web.admin import bp as admin_bp
from src.db import init_app as init_database
from src.utils.proxy_manager import ProxyManager
from configs.logging_config import get_logger

logger = get_logger(__name__)


def _load_or_create_secret(data_dir):
    """为零配置部署生成可持久化的随机会话密钥。"""
    secret_path = os.path.join(data_dir, ".secret_key")
    try:
        descriptor = os.open(secret_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as secret_file:
            fcntl.flock(secret_file.fileno(), fcntl.LOCK_EX)
            saved = secret_file.read().strip()
            if saved:
                return saved
            generated = secrets.token_hex(32)
            secret_file.seek(0)
            secret_file.write(generated)
            secret_file.flush()
            os.fsync(secret_file.fileno())
            return generated
    except (PermissionError, OSError) as e:
        logger.warning(f"无法读写密钥文件 {secret_path}，使用临时随机会话密钥: {e}")
        return secrets.token_hex(32)


def create_app(config=None):
    """应用工厂函数"""
    data_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data')
    app = Flask(__name__, instance_path=data_dir, template_folder='templates', static_folder='static')
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY') or None
    app.config['DATABASE'] = os.getenv(
        'DATABASE_PATH', os.path.join(app.instance_path, 'media_parser.db')
    )
    app.config['JULIANG_PROXY_API_URL'] = os.getenv('JULIANG_PROXY_API_URL', '').strip()
    # 16MB 是多媒体工具上传的上限闸门（单图 12MB + multipart 余量），
    # 各接口仍保留自己的精确校验（头像 2MB、工具图片 12MB）。
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
    app.config['JSON_SORT_KEYS'] = False
    app.config['TRUST_PROXY_HEADERS'] = os.getenv(
        'TRUST_PROXY_HEADERS', ''
    ).strip().lower() in {'1', 'true', 'yes', 'on'}
    app.config['API_ONLY'] = os.getenv(
        'API_ONLY', ''
    ).strip().lower() in {'1', 'true', 'yes', 'on'}
    app.config['WX_APPID'] = os.getenv('WX_APPID', '').strip()
    app.config['WX_APPSECRET'] = os.getenv('WX_APPSECRET', '').strip()
    # 插件扫码登录的小程序码指向哪个版本：release（默认，生产）/ trial / develop。
    # 小程序未发布前只有 trial 的码能被开发者扫开。
    app.config['WX_QR_ENV_VERSION'] = os.getenv('WX_QR_ENV_VERSION', 'release').strip().lower()
    if hasattr(app, 'json'):
        app.json.sort_keys = False
    if config:
        app.config.update(config)

    database_dir = os.path.dirname(app.config['DATABASE'])
    if database_dir:
        os.makedirs(database_dir, exist_ok=True)
    if not app.config.get('SECRET_KEY'):
        app.config['SECRET_KEY'] = _load_or_create_secret(database_dir or app.instance_path)
    init_database(app)
    manager = ProxyManager(app.config["DATABASE"], app.config["JULIANG_PROXY_API_URL"])
    app.extensions["proxy_manager"] = manager
    if not app.testing:
        manager.start()

    # 注册蓝图
    app.register_blueprint(api_bp, url_prefix='/api')
    # wx_bp 必须在外层：小程序是**纯 API 调用方**，API_ONLY=true 时它照样要能登录（§1.1）
    app.register_blueprint(wx_bp, url_prefix='/api')
    # 创作工具是纯 API，网页管理端关闭（API_ONLY）时也应照常可用
    app.register_blueprint(media_tools_bp, url_prefix='/api')
    if not app.config.get('API_ONLY'):
        register_template_helpers(app)
        app.register_blueprint(web_bp)
        app.register_blueprint(auth_bp)
        app.register_blueprint(portal_bp)
        app.register_blueprint(admin_bp)

    if app.config.get('TRUST_PROXY_HEADERS'):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8051)
