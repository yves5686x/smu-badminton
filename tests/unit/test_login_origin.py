"""登录请求的 ``Origin`` 头必须与**解析出的登录页 host** 同源。

背景（2026-09-18 配置收敛）：``Origin`` 此前取自定义项 ``CAS_ORIGIN``，而它的 ``.env``
取值已漂移成 ``cas.shmtu.edu.cn``，真实登录页却已迁到 ``sso.shmtu.edu.cn``。
现在改为 ``_origin_of(解析出的登录页 URL)`` 派生，并删除该配置项。

这些用例锁住两件事：
1. 行为上——登录 POST 的 ``Origin`` 跟随登录页 host，不做任何硬编码；
2. 结构上——``cas_login.py`` 里不得再出现硬编码的 ``Origin`` 字面量，
   也不得再 import ``CAS_ORIGIN``。
"""
import ast
from pathlib import Path

import pytest

from smu_badminton import cas_login
from smu_badminton.cas_login import LoginErrorType, _origin_of

CAS_LOGIN_SRC = Path(cas_login.__file__)


class _Resp:
    """最小响应对象：够 ``attempt_login_with_captcha`` 走完失败分支即可。"""

    def __init__(self, status_code: int = 200, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = ""


class _FakeSession:
    """记录请求头的假会话，不产生任何网络流量。"""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.gets.append((url, kw.get("headers") or {}))
        return _Resp(text="<html></html>")

    def post(self, url, **kw):
        self.posts.append((url, kw.get("headers") or {}))
        # 返回「认证失败」，让流程停在失败分支（不触发重定向与 token 交换）
        return _Resp(status_code=200, text="认证失败")


def _login_and_capture(cas_login_url: str) -> tuple[_FakeSession, object]:
    """用假会话跑一次登录提交，返回 (会话, 结果)。"""
    session = _FakeSession()
    result = cas_login.attempt_login_with_captcha(
        session,
        cas_login_url,
        "execution-value",
        "202540510004",
        "not-a-real-password",
        "1234",
        login_page_html="<html></html>",  # 传入 HTML，避免额外 GET
    )
    return session, result


# ============= 行为：Origin 跟随登录页 host =============


@pytest.mark.parametrize(
    "cas_login_url, expected_origin",
    [
        # 迁移后的真实登录页
        ("https://sso.shmtu.edu.cn/cas/login?service=abc", "https://sso.shmtu.edu.cn"),
        # 迁移前的旧主机名：Origin 应跟着走，而不是被任何硬编码值覆盖
        ("https://cas.shmtu.edu.cn/cas/login?service=abc", "https://cas.shmtu.edu.cn"),
        # 带端口的登录页：Origin 必须保留端口，否则跨源校验会失败
        ("http://127.0.0.1:8080/cas/login", "http://127.0.0.1:8080"),
    ],
)
def test_login_post_origin_follows_login_page_host(cas_login_url, expected_origin):
    session, result = _login_and_capture(cas_login_url)

    assert session.posts, "应当发起了一次登录 POST"
    url, headers = session.posts[-1]
    assert url == cas_login_url
    assert headers["Origin"] == expected_origin, "Origin 必须由登录页 URL 派生"
    assert headers["Referer"] == cas_login_url
    # 顺带确认用例真的走到了失败分支（否则断言可能落在未被执行的路径上）
    assert result.error_type == LoginErrorType.PASSWORD_ERROR


def test_origin_matches_host_even_when_login_page_is_redirect_target():
    """登录页 host 与请求入口 host 不同时，Origin 取的是**登录页**的 host。"""
    session, _ = _login_and_capture("https://sso.shmtu.edu.cn/cas/login?service=x")
    _, headers = session.posts[-1]
    assert headers["Origin"] == "https://sso.shmtu.edu.cn"
    assert "wf.shmtu.edu.cn" not in headers["Origin"]


# ============= _origin_of 本身 =============


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://sso.shmtu.edu.cn/cas/login", "https://sso.shmtu.edu.cn"),
        ("https://sso.shmtu.edu.cn:443/cas/login", "https://sso.shmtu.edu.cn:443"),
        ("http://host/cas/login", "http://host"),
        ("", ""),
        ("not-a-url", ""),
        ("/cas/login", ""),
    ],
)
def test_origin_of(url, expected):
    assert _origin_of(url) == expected


# ============= 结构：不得硬编码、不得复活旧配置项 =============


def _cas_login_tree() -> ast.Module:
    return ast.parse(CAS_LOGIN_SRC.read_text(encoding="utf-8"))


def test_origin_header_is_never_a_string_literal():
    """``Origin`` 的值必须是派生表达式，不能是写死的字符串。

    否则学校再换一次登录主机名，Origin 又会静默漂移——正是这次要修的毛病。
    """
    offenders = []
    for node in ast.walk(_cas_login_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "Origin":
                if isinstance(value, ast.Constant):
                    offenders.append((node.lineno, value.value))
    assert not offenders, f"cas_login.py 出现硬编码 Origin 字面量：{offenders}"


def test_cas_origin_config_is_not_imported():
    """``CAS_ORIGIN`` 已按分层规范删除，不得再被 import。"""
    imported: set[str] = set()
    for node in ast.walk(_cas_login_tree()):
        if isinstance(node, ast.ImportFrom) and node.module in ("config", ".config"):
            imported.update(alias.name for alias in node.names)
    assert "CAS_ORIGIN" not in imported
