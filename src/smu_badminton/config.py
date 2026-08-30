"""
Configuration utilities loaded from .env.
"""

import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import load_dotenv


# Load .env in project root (src/smu_badminton/../../.env)
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent

# WF platform config
WF_ORIGIN = os.getenv("WF_ORIGIN", "https://wf.shmtu.edu.cn")
WF_API_URL = os.getenv("WF_API_URL", "https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys")
WF_HOME_URL = os.getenv("WF_HOME_URL", f"{WF_ORIGIN}/yy-sys/pc/home")
WF_SSO_AUTHORIZE_PATH = os.getenv("WF_SSO_AUTHORIZE_PATH", "/sso/oauth2/authorize")
WF_CAPTCHA_URL = os.getenv("WF_CAPTCHA_URL", f"{WF_ORIGIN}/yy-sys/captcha")

# CAS config
# 登录页已由 cas.shmtu.edu.cn 迁至 sso.shmtu.edu.cn；验证码 /cas/captcha 也随之迁到 sso.，
# 且返回 JSON {image, token, expiresAt}（见 cas_login._fetch_captcha_challenge）。
CAS_ORIGIN = os.getenv("CAS_ORIGIN", "https://sso.shmtu.edu.cn")
CAS_CAPTCHA_URL = os.getenv("CAS_CAPTCHA_URL", "https://sso.shmtu.edu.cn/cas/captcha")
# Backward compatibility: keep field name, but default entry is WF home now.
CAS_LOGIN_URL = os.getenv("CAS_LOGIN_URL", WF_HOME_URL)

# OAuth config
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "kwxKbMKq3Nafw2mApFZz")

# Resource type id
BADMINTON_TYPE_ID = os.getenv("BADMINTON_TYPE_ID", "93c2a115-5c73-4e30-bb6a-dfcc5404e46f")

# ========== Runtime config ==========
BOOKING_DEBUG = os.getenv("BOOKING_DEBUG", "0").lower() in {"1", "true", "yes", "on"}
TOKEN_PROFILE_TTL_SEC = int(os.getenv("TOKEN_PROFILE_TTL_SEC", "3600"))
TOKEN_CACHE_TTL_SEC = int(os.getenv("TOKEN_CACHE_TTL_SEC", "900"))
JOB_RETENTION_SEC = int(os.getenv("JOB_RETENTION_SEC", "3600"))

# ========== Data path config ==========
# Docker 环境使用 /app/data，本地开发使用项目目录下的 data
DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app/data") else str(BASE_DIR / "data"))

# ========== Security config ==========


def _load_or_create_secret_key() -> str:
    """解析 SECRET_KEY：env 优先；否则自动生成随机密钥并持久化到 DATA_DIR/secret_key。

    持久化后重启复用同一密钥，无需手动配置即可避免源码里的公开默认钥；
    持久化失败（目录只读等）时退回进程内随机密钥并警告（重启后保存的凭据失效，
    用户重新登录即可重建）。密钥变更后旧混淆数据按失效处理（deobfuscate 返回空）。
    """
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key

    key_path = Path(DATA_DIR) / "secret_key"
    try:
        if key_path.exists():
            cached = key_path.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        key_path.parent.mkdir(parents=True, exist_ok=True)
        new_key = secrets.token_hex(32)
        key_path.write_text(new_key, encoding="utf-8")
        os.chmod(key_path, 0o600)
        logging.getLogger(__name__).info("已自动生成 SECRET_KEY 并持久化到 %s", key_path)
        return new_key
    except OSError as e:
        logging.getLogger(__name__).warning(
            "SECRET_KEY 持久化失败（%s），使用进程内随机密钥；重启后已保存凭据将失效", e
        )
        return secrets.token_hex(32)


SECRET_KEY = _load_or_create_secret_key()

AUTHORIZED_USERS = set(os.getenv("AUTHORIZED_USERS", "202540510004").split(","))

# 可信代理 IP 列表（用于 X-Forwarded-For 验证）
# 只有来自可信代理的请求才会信任 X-Forwarded-For 头
# 示例：TRUSTED_PROXIES=127.0.0.1,10.0.0.1
TRUSTED_PROXIES = set(os.getenv("TRUSTED_PROXIES", "").split(",")) if os.getenv("TRUSTED_PROXIES") else set()

# ========== Rate limit config ==========
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "10"))
RATE_LIMIT_JOBS_MAX = int(os.getenv("RATE_LIMIT_JOBS_MAX", "300"))
RATE_LIMIT_JOBS_WINDOW = int(os.getenv("RATE_LIMIT_JOBS_WINDOW", "60"))

# ========== Server config ==========
UVICORN_RELOAD = os.getenv("UVICORN_RELOAD", "0").lower() in {"1", "true", "yes", "on"}

# ========== User info defaults ==========
DEFAULT_DEPT_CODE = os.getenv("DEFAULT_DEPT_CODE", "")
DEFAULT_DEPT_NAME = os.getenv("DEFAULT_DEPT_NAME", "")
DEFAULT_DEPT_NAME_EN = os.getenv("DEFAULT_DEPT_NAME_EN", "")
DEFAULT_USER_EMAIL = os.getenv("DEFAULT_USER_EMAIL", "")
DEFAULT_USER_PHONE = os.getenv("DEFAULT_USER_PHONE", "")


def build_wf_authorize_url(ret_url: str | None = None, state: str | None = None, nonce: str | None = None) -> str:
    """Build WF oauth2 authorize URL. If not authenticated it will redirect to CAS login."""
    ret = ret_url or WF_HOME_URL
    callback = f"{WF_ORIGIN}/yy-sys/oidc-callback?retUrl={ret}"
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "id_token token",
        "scope": "data openid process task app submit process_edit start profile",
        "state": state or os.urandom(16).hex(),
        "nonce": nonce or os.urandom(16).hex(),
    }
    return f"{WF_ORIGIN}{WF_SSO_AUTHORIZE_PATH}?{urlencode(params, quote_via=quote)}"


def get_frontend_config():
    """Return frontend-required settings."""
    return {
        "login_url": WF_HOME_URL,
        "authorize_url": build_wf_authorize_url(WF_HOME_URL),
        "captcha_url": CAS_CAPTCHA_URL,
    }
