"""凭据托管单元测试。

验证预约链路免密发送的核心：请求未携带密码/URL 时，
服务端自动回退到已保存的账号（登录成功后自动保存）。
"""
from unittest.mock import patch

import pytest

from smu_badminton.cas_manager import get_token_cached, resolve_login_credentials
from smu_badminton.token_profile import (
    clear_token_cache,
    delete_user_account,
    save_user_account,
)


@pytest.fixture(autouse=True)
def cleanup_account():
    """每个测试前后清理账号与 token 缓存，避免相互影响。"""
    username = "test_fallback_user"
    clear_token_cache(username)
    delete_user_account(username)
    yield username
    clear_token_cache(username)
    delete_user_account(username)


def test_resolve_with_saved_account(cleanup_account):
    """请求不带密码时回退到保存的凭据与 URL。"""
    username = cleanup_account
    save_user_account(username, "saved_pwd", "https://saved/login", "https://saved/captcha")

    resolved = resolve_login_credentials("", "", username, "")
    assert resolved == ("https://saved/login", "https://saved/captcha", "saved_pwd")


def test_resolve_explicit_password_wins(cleanup_account):
    """请求显式携带密码时优先使用（旧客户端兼容）。"""
    username = cleanup_account
    save_user_account(username, "saved_pwd", "https://saved/login", "https://saved/captcha")

    resolved = resolve_login_credentials("", "", username, "explicit_pwd")
    assert resolved == ("https://saved/login", "https://saved/captcha", "explicit_pwd")


def test_resolve_without_any_credentials(cleanup_account):
    """既无请求密码也无保存账号时返回 None。"""
    assert resolve_login_credentials("", "", cleanup_account, "") is None


def test_resolve_fills_urls_from_config(cleanup_account):
    """URL 缺省时用保存值/配置默认值兜底。"""
    username = cleanup_account
    save_user_account(username, "saved_pwd")

    resolved = resolve_login_credentials("", "", username, "")
    assert resolved is not None
    login_url, captcha_url, password = resolved
    assert password == "saved_pwd"
    assert login_url  # 配置默认值非空
    assert captcha_url


def test_get_token_cached_uses_saved_account(cleanup_account):
    """token 缓存未命中时，用保存的凭据登录而不是直接失败。"""
    username = cleanup_account
    save_user_account(username, "saved_pwd", "https://saved/login", "https://saved/captcha")

    with patch("smu_badminton.cas_manager.login_with_retry") as mock_login:
        mock_login.return_value = {"access_token": "at", "id_token": "it"}
        tokens = get_token_cached("", "", username, "")

    assert tokens == {"access_token": "at", "id_token": "it"}
    # 登录参数来自保存的账号，而非请求里的空值
    args, kwargs = mock_login.call_args
    assert args[:4] == ("https://saved/login", "https://saved/captcha", username, "saved_pwd")


def test_get_token_cached_without_credentials(cleanup_account):
    """无任何凭据时返回 None，不发起登录。"""
    with patch("smu_badminton.cas_manager.login_with_retry") as mock_login:
        assert get_token_cached("", "", cleanup_account, "") is None
    mock_login.assert_not_called()
