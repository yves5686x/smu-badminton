"""预约所需凭据解析与 token 获取；不管理任务或场地状态。"""

import logging
import time

from .cas_login import login_with_retry
from .config import CAS_CAPTCHA_URL, TOKEN_CACHE_TTL_SEC
from .settings_store import get_login_entry_url
from .token_profile import cache_token_for_user, get_cached_token, get_user_account

logger = logging.getLogger(__name__)


def resolve_login_credentials(
    login_url: str,
    captcha_url: str,
    username: str,
    password: str,
) -> tuple[str, str, str] | None:
    """合并请求携带的凭据与服务端保存的账号，返回 (login_url, captcha_url, password)。

    请求未带密码时自动回退到服务端保存的凭据（登录成功后自动保存），
    这是预约链路免密发送的唯一入口；URL 缺省时依次用保存值、登录入口权威值兜底。
    无任何可用凭据时返回 None。
    """
    resolved_password = password
    resolved_login_url = login_url
    resolved_captcha_url = captcha_url

    if not resolved_password or not resolved_login_url or not resolved_captcha_url:
        account = get_user_account(username)
        if account:
            resolved_password = resolved_password or account.get("password") or ""
            resolved_login_url = resolved_login_url or account.get("login_url") or ""
            resolved_captcha_url = resolved_captcha_url or account.get("captcha_url") or ""

    # 登录入口 URL 是 L3 运行时可变配置：必须每次读取，不能在导入期取快照
    resolved_login_url = resolved_login_url or get_login_entry_url()
    resolved_captcha_url = resolved_captcha_url or CAS_CAPTCHA_URL

    if not resolved_password:
        logger.warning("无可用于登录的凭据: username=%s（请求未携带密码且服务端无保存账号）", username)
        return None
    return resolved_login_url, resolved_captcha_url, resolved_password


def get_token_cached(
    login_url: str,
    captcha_url: str,
    username: str,
    password: str,
    ttl_seconds: int = TOKEN_CACHE_TTL_SEC,
    *,
    force_refresh: bool = False,
) -> dict[str, str] | None:
    """
    获取缓存的 token 或重新登录。

    Args:
        login_url: CAS 登录 URL（可传空，自动兜底）
        captcha_url: 验证码 URL（可传空，自动兜底）
        username: 用户名
        password: 密码（可传空：回退到服务端保存的凭据）
        ttl_seconds: 缓存 TTL

    Returns:
        token 字典（包含 access_token 和 id_token），失败返回 None
    """
    t0 = time.time()

    tokens_cached = None if force_refresh else get_cached_token(username, ttl_seconds)
    if tokens_cached:
        logger.debug("[性能] Token 缓存命中: %.0fms", (time.time() - t0) * 1000)
        return tokens_cached

    resolved = resolve_login_credentials(login_url, captcha_url, username, password)
    if resolved is None:
        return None
    login_url, captcha_url, password = resolved

    logger.info("[性能] Token 缓存未命中，开始登录...")
    t1 = time.time()
    tokens = login_with_retry(login_url, captcha_url, username, password, max_retries=3)
    t2 = time.time()
    logger.info("[性能] CAS 登录耗时: %.0fms", (t2 - t1) * 1000)

    if not tokens or not tokens.get("access_token"):
        return None

    cache_token_for_user(username, tokens)
    logger.info("[性能] get_token_cached 总耗时: %.0fms", (time.time() - t0) * 1000)
    return tokens
