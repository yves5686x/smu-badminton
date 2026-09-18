#!/usr/bin/env python
"""诊断登录链路解析失败：逐跳打印 host、Location，以及 requests 实际使用的代理。

为什么要单独诊断
----------------
``verify_real_login.py`` 在部分终端里第一跳就报::

    LocationParseError: Failed to parse: '...', label empty or too long

这条消息来自 ``urllib3/util/connection.py``::

    raise LocationParseError(f"'{host}', label empty or too long")

而 ``LocationParseError`` 的消息格式是 ``Failed to parse: {location}``。两段拼起来就是
``Failed to parse: '<host>', label empty or too long``——**引号里的 ``...`` 是 host
字面值**，不是被截断的长 URL。也就是说，连接阶段拿到的 host 就是三个点。

host 变成 ``...`` 只有两种来源：

1. 链路中某一跳的 URL（或它返回的 ``Location`` 头）本身就是 ``https://...`` 这类占位符；
2. requests 走了环境代理，而某个代理变量的值就是 ``...``。

本脚本把两条线索都摊开：先打印 ``requests.getproxies()``（requests 真正会用的代理），
再包一层 ``session.get`` 逐跳打印 URL / host / status / Location，异常时直接指出是
**第几跳、哪个 URL、哪个 host** 出的问题。失败后会自动用 ``trust_env=False`` 再跑一遍做
对比——如果关掉环境变量信任就成功，说明是代理/证书类环境变量的锅。

用法
----
::

    .venv/bin/python scripts/diag_login_chain.py

    # 指定入口 URL（覆盖 L3/L4，用于对比不同入口）
    .venv/bin/python scripts/diag_login_chain.py --login-url https://wf.shmtu.edu.cn/yy-sys/pc/home

本脚本**只做 GET 探测，不提交任何凭据**。
"""
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import requests  # noqa: E402
import urllib3  # noqa: E402
from requests.utils import getproxies  # noqa: E402

from smu_badminton.cas_login import _resolve_cas_login_url  # noqa: E402
from smu_badminton.settings_store import get_login_entry_url  # noqa: E402

PROXY_KEYS = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)

# 一眼可疑的代理值：占位符没填、空串、只有 scheme
SUSPECT_VALUES = {"", "...", "://", "http://", "https://", "http://...", "https://..."}


def _short(url: str, limit: int = 100) -> str:
    """长 URL 截断显示，但保留真实长度，避免"看起来是 ..."的误判。"""
    text = str(url)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字符）"


def _host_of(url: str) -> str:
    """取 host；解析失败时返回带说明的字符串而不是抛异常。"""
    try:
        return urlparse(str(url)).hostname or ""
    except Exception as e:
        return f"<urlparse 失败: {type(e).__name__}: {e}>"


def show_env() -> None:
    print("=" * 68)
    print("1. requests 会使用的环境代理")
    print("=" * 68)
    print(f"getproxies() → {getproxies() or '（空，不走代理）'}")
    for key in PROXY_KEYS:
        value = os.environ.get(key)
        if value is None:
            continue
        mark = "   ← 畸形值，会污染 host！" if value.strip() in SUSPECT_VALUES else ""
        print(f"  {key} = {value!r}{mark}")
    print(f"  requests={requests.__version__}  urllib3={urllib3.__version__}")
    print()


def walk_chain(entry_url: str, *, trust_env: bool, timeout: int = 20) -> tuple[bool, dict]:
    """走一遍真实解析路径，逐跳打印。返回 (是否成功, 失败详情)。"""
    session = requests.Session()
    session.trust_env = trust_env
    failure: dict = {}
    real_get = session.get

    def spy_get(url, **kwargs):  # noqa: ANN001
        host = _host_of(url)
        print(f"  GET {_short(url)}")
        print(f"      host = {host!r}")
        try:
            resp = real_get(url, **kwargs)
        except Exception as e:
            failure.update({"url": str(url), "host": host, "exc": f"{type(e).__name__}: {e}"})
            print(f"      ✗ {type(e).__name__}: {e}")
            raise
        location = resp.headers.get("Location", "")
        shown = _short(location) if location else "（无）"
        print(f"      → {resp.status_code}   Location = {shown}")
        if location:
            absolute = urljoin(str(url), location)
            print(f"         Location 绝对化后 host = {_host_of(absolute)!r}")
        return resp

    session.get = spy_get  # type: ignore[method-assign]
    try:
        resolved = _resolve_cas_login_url(session, entry_url, timeout=timeout)
    except Exception as e:
        failure.setdefault("exc", f"{type(e).__name__}: {e}")
        print(f"  ✗ 解析失败: {type(e).__name__}: {e}")
        return False, failure
    print(f"  ✓ 解析到 CAS 登录页: {_short(resolved, 140)}")
    print(f"     Origin 将为: {_origin_hint(resolved)}")
    return True, failure


def _origin_hint(url: str) -> str:
    parsed = urlparse(str(url))
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "（无法派生：URL 缺 host）"


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断登录链路解析失败（只 GET 探测，不提交凭据）")
    parser.add_argument("--login-url", default="", help="登录入口 URL；缺省用 L3→L4 权威值")
    args = parser.parse_args()

    show_env()

    entry_url = args.login_url.strip() or get_login_entry_url()
    print("=" * 68)
    print("2. 登录入口")
    print("=" * 68)
    print(f"入口 URL  = {entry_url!r}")
    print(f"入口 host = {_host_of(entry_url)!r}")
    print()

    print("=" * 68)
    print("3. 逐跳跟踪（trust_env=True，与生产一致）")
    print("=" * 68)
    ok, failure = walk_chain(entry_url, trust_env=True)
    print()
    if ok:
        print("结论：链路正常。若 verify_real_login.py 仍失败，请对照上面打印的代理值与环境变量。")
        return 0

    print("=" * 68)
    print("4. 对比：关闭环境变量信任（trust_env=False）")
    print("=" * 68)
    ok_isolated, _ = walk_chain(entry_url, trust_env=False)
    print()

    print("=" * 68)
    print("结论")
    print("=" * 68)
    if failure.get("url"):
        print(f"失败跳    : {_short(failure['url'], 140)}")
        print(f"失败 host : {failure['host']!r}")
    print(f"失败原因  : {failure.get('exc', '(未知)')}")
    if ok_isolated:
        print()
        print("→ 关闭 trust_env 后成功：**环境变量导致的**。检查上面的 proxy 变量")
        print("  （值为 '...'、空串或只有 scheme 都会被 urllib3 当成畸形 host）。")
    else:
        print()
        print("→ 关闭 trust_env 后仍失败：**链路本身有问题**。")
        print("  请把上面 3 的完整输出贴出来，重点看哪一跳的 Location 是占位符或畸形值。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
