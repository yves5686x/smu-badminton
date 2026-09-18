#!/usr/bin/env python
"""真机验证登录链路（用于确认「Origin 头按登录页派生」的改动没有破坏登录）。

为什么要单独写一个脚本
----------------------
配置收敛时删掉了 ``CAS_ORIGIN`` 配置项，登录 POST 的 ``Origin`` 头改为按**解析出的
登录页 host** 派生（``cas_login._origin_of(cas_url)``）。此前它取自 ``.env``，值已漂移成
``cas.shmtu.edu.cn``，而真实登录页在 ``sso.shmtu.edu.cn``。这个改动理论上是修正，但
"学校侧是否校验 Origin" 只能靠真机验证——本脚本就是干这个的。

它验证的是**应用真实走的那条路径**：``cas_manager.get_token_cached`` →
``cas_login.login_with_retry`` → ``cas_login_stable``，``login_url`` 缺省时回退到
``settings_store.get_login_entry_url()``（与 ``routes_auth`` 的 ``req.login_url or ...`` 一致）。

会打印
------
1. 登录入口 URL（L3 数据库覆盖 → L4 代码默认）
2. 沿 WF → SSO 重定向链解析出的 CAS 登录页
3. 登录 POST **实际上 wire 的 ``Origin`` / ``Referer`` 头**（钩住 ``Session.post`` 抓真值）
4. 登录结果：成功与否、失败类型、token 是否存在（打码）、access_token 的 exp

用法
----
::

    # 只看链路、不提交凭据（推荐的第一次运行：确认入口与 Origin）
    .venv/bin/python scripts/verify_real_login.py --dry-run

    # 真机登录（只敲密码；学号自动取 BB_TEST_USER → AUTHORIZED_USERS 首个）
    .venv/bin/python scripts/verify_real_login.py

    # 真机登录（凭据全走环境变量，适合非交互场景）
    BB_TEST_USER=学号 BB_TEST_PASS=密码 .venv/bin/python scripts/verify_real_login.py

    # 指定登录入口（覆盖 L3/L4，用于对比不同入口）
    .venv/bin/python scripts/verify_real_login.py --login-url https://wf.shmtu.edu.cn/yy-sys/pc/home

安全说明
--------
- **只做登录，不提交任何预约**，因此不会消耗上游的「预约提交」频控额度。
- 密码只从环境变量或交互式隐藏输入读取，不落盘、不进日志、不回显。
- ``--dry-run`` 全程不发 POST，可以用它先确认网络与链路。
"""
import argparse
import getpass
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import requests  # noqa: E402
from requests.utils import getproxies  # noqa: E402

from smu_badminton import config as cfg  # noqa: E402  导入即触发 .env 加载
from smu_badminton.cas_login import (  # noqa: E402
    LoginErrorType,
    _origin_of,
    _resolve_cas_login_url,
    login_with_retry,
)
from smu_badminton.settings_store import get_login_entry_url  # noqa: E402
from smu_badminton.token_profile import decode_jwt_payload  # noqa: E402


def _mask(token: str | None, keep: int = 12) -> str:
    """token 打码：只留头尾，避免日志里出现可用凭据。"""
    if not token:
        return "(无)"
    if len(token) <= keep * 2:
        return f"{token[:keep]}...(len={len(token)})"
    return f"{token[:keep]}...{token[-6:]}(len={len(token)})"


def _hr(title: str = "") -> None:
    print("\n" + "=" * 68)
    if title:
        print(title)
        print("=" * 68)


def ensure_db_ready() -> None:
    """初始化数据库表，与应用启动流程一致。

    L3（``app_settings``）依赖建表。应用启动时 ``server_fastapi`` 的 lifespan 会调
    ``init_db_tables()``；脚本若跳过这一步，读 L3 会报 ``no such table: app_settings``
    并回退到默认值——虽然结果仍正确，但会让输出看起来像故障。
    """
    try:
        from smu_badminton.core_utils import init_db_tables

        init_db_tables()
    except Exception as e:
        print(f"⚠ 数据库初始化失败（继续，L3 将回退到 L4 默认）: {type(e).__name__}: {e}")


# 明显是占位符的代理值：urllib3 会把它们当成 host，报 "label empty or too long"
_PROXY_PLACEHOLDERS = {"", "...", "://", "http://", "https://", "http://...", "https://..."}


def warn_env_proxy() -> None:
    """提示环境代理（``requests.Session`` 默认 ``trust_env=True``，会读取代理变量）。

    若代理变量的值只是占位符（如 ``...``），urllib3 在连接阶段会把它当 host 去做 IDNA
    编码，报出 ``Failed to parse: '...', label empty or too long``——看着像链路故障，
    实则与学校侧、与本脚本都无关。提前点明，省下一轮误判。
    """
    proxies = getproxies()
    if not proxies:
        return
    bad = {k: v for k, v in proxies.items() if str(v).strip() in _PROXY_PLACEHOLDERS}
    if bad:
        print(f"⚠ 环境代理值异常: {bad}")
        print("  这会让 urllib3 把 '...' 当成主机名，报 LocationParseError —— 与学校侧无关。")
        print("  清掉后重试：unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY")
    else:
        print(f"ℹ 检测到环境代理 {proxies}，请求将经由代理发出")


class ChainResolutionError(RuntimeError):
    """重定向链解析失败，并携带「已走过的跳 + 失败点」以便定位。

    裸抛异常时只能看到 urllib3 的天书，例如::

        LocationParseError: Failed to parse: '...', label empty or too long

    这条消息里引号内的是 **host 字面值**（urllib3 在 ``create_connection`` 里对
    host 做 IDNA 编码，host 为 ``...`` 就会失败），光看它无法判断是哪一跳出的问题。
    """

    def __init__(self, message: str, *, hops: list[str], failed_url: str = "", failed_host: str = ""):
        super().__init__(message)
        self.hops = hops
        self.failed_url = failed_url
        self.failed_host = failed_host


def trace_redirect_chain(entry_url: str, timeout: int = 20) -> str:
    """沿重定向链解析 CAS 登录页，并打印每一跳。"""
    session = requests.Session()
    hops: list[str] = []
    failure: dict[str, str] = {}

    # 包一层 session.get 记录跳转，再复用真实解析器，避免逻辑与生产不一致
    real_get = session.get

    def logged_get(url, **kw):
        host = urlparse(str(url)).hostname or ""
        try:
            resp = real_get(url, **kw)
        except Exception as e:
            # 记下失败点：连 host 一起带出来，畸形 host 一眼可见
            failure.update(url=str(url), host=host, exc=f"{type(e).__name__}: {e}")
            raise
        hops.append(f"{url}  ->  {resp.status_code}")
        return resp

    session.get = logged_get  # type: ignore[method-assign]
    try:
        resolved = _resolve_cas_login_url(session, entry_url, timeout=timeout)
    except Exception as e:
        raise ChainResolutionError(
            f"{type(e).__name__}: {e}",
            hops=hops,
            failed_url=failure.get("url", ""),
            failed_host=failure.get("host", ""),
        ) from e
    finally:
        session.get = real_get  # type: ignore[method-assign]

    print(f"入口 URL          : {entry_url}")
    print("重定向链：")
    for hop in hops:
        print(f"  · {hop}")
    print(f"解析到的登录页    : {resolved}")
    print(f"将发送的 Origin   : {_origin_of(resolved)}")
    print(f"登录页 host        : {urlparse(resolved).netloc}")
    return resolved


def do_real_login(entry_url: str, captcha_url: str, username: str, password: str,
                  max_retries: int = 2) -> dict | None:
    """走应用真实路径登录，并抓取登录 POST 实际发送的请求头。"""
    captured: list[dict[str, str]] = []

    real_post = requests.Session.post

    def spy_post(self, url, *args, **kwargs):  # noqa: ANN001
        headers = kwargs.get("headers") or {}
        captured.append(
            {
                "url": str(url),
                "Origin": str(headers.get("Origin", "(未发送)")),
                "Referer": str(headers.get("Referer", "(未发送)")),
                "Content-Type": str(headers.get("Content-Type", "(未发送)")),
            }
        )
        return real_post(self, url, *args, **kwargs)

    requests.Session.post = spy_post  # type: ignore[method-assign]
    try:
        tokens = login_with_retry(entry_url, captcha_url, username, password,
                                  max_retries=max_retries)
    finally:
        requests.Session.post = real_post  # type: ignore[method-assign]

    print(f"登录 POST 次数    : {len(captured)}")
    if captured:
        print("实际发送的请求头（抓自 requests.Session.post）：")
        for i, c in enumerate(captured, 1):
            print(f"  [{i}] POST {c['url']}")
            print(f"      Origin       = {c['Origin']}")
            print(f"      Referer      = {c['Referer']}")
            print(f"      Content-Type = {c['Content-Type']}")

        expect = _origin_of(captured[-1]["url"])
        origins = {c["Origin"] for c in captured if c["Origin"] != "(未发送)"}
        if not origins:
            print("  ⚠ 登录 POST 未带 Origin 头")
        elif len(origins) == 1:
            got = origins.pop()
            host = urlparse(captured[-1]["url"]).netloc
            if got.endswith(host):
                print(f"  ✓ Origin 与登录页同源（host={host}）")
            else:
                print(f"  ⚠ Origin={got} 与登录页 host={host} 不同源（expect={expect}）")
    return tokens


def report_tokens(tokens: dict) -> None:
    """打印 token 形态与 exp，并指出它对抢票预检的影响。

    生产代码 T-0 前的 token 预检用 ``token_profile.session_exp_epoch(tokens)``：先读
    access_token 的 JWT exp，读不出再回退 id_token（2026-09-18 真机实测：SMU 的
    access_token 是 32 字符 opaque token，恒读不出 exp）。这里把两个 token 分别剖析，
    便于确认回退落在 id_token 上、以及会话还剩多久。
    """
    import datetime as _dt

    access = tokens.get("access_token", "")
    id_token = tokens.get("id_token", "")

    print("✓ 登录成功")
    print(f"  access_token : {_mask(access)}")
    print(f"  id_token     : {_mask(id_token)}")

    def _describe(label: str, token: str) -> float | None:
        claims = decode_jwt_payload(token)
        if not claims:
            print(f"  {label:<16}: 无法读 exp（不是 JWT，没有 payload）")
            return None
        exp = claims.get("exp")
        try:
            exp_float = float(exp) if exp else None
        except (TypeError, ValueError):
            exp_float = None
        if exp_float is None:
            print(f"  {label:<16}: JWT 里没有 exp 声明")
            return None
        local = _dt.datetime.fromtimestamp(exp_float).strftime("%Y-%m-%d %H:%M:%S")
        remain = int(exp_float - _dt.datetime.now().timestamp())
        print(f"  {label:<16}: {local}（剩余 {remain}s）")
        return exp_float

    print()
    print("  token 剖析：")
    access_exp = _describe("access_token exp", access)
    id_exp = _describe("id_token exp", id_token)

    print()
    # 预检的风险阈值：缓存 TTL + 预检缓冲。会话寿命低于此值时，T-0 手上的 token 才可能临近过期。
    cache_ttl, exp_buffer = 900, 120
    if access_exp is None and id_exp is not None:
        remain_id = int(id_exp - _dt.datetime.now().timestamp())
        print("  ⓘ access_token 不是 JWT（无 exp），抢票预检会回退读 id_token 的 exp。")
        print(f"    id_token 剩余 {remain_id}s。")
        if remain_id <= cache_ttl + exp_buffer:
            print(f"    ⚠ 会话寿命偏短（≤ {cache_ttl + exp_buffer}s = 缓存 TTL {cache_ttl}s + 缓冲 {exp_buffer}s）：")
            print("      T-0 时手上的 token 可能已临近过期，建议缩小 get_token_cached 的 ttl_seconds。")
        else:
            print(f"    换算：缓存内 token 最多 {cache_ttl}s 旧 → T-0 时至少还剩 {remain_id - cache_ttl}s，"
                  f"远大于缓冲 {exp_buffer}s，预检不会误触发刷新。")
    elif access_exp is not None:
        print("  ✓ access_token 本身就是 JWT，预检直接读它的 exp，无需回退。")
    else:
        print("  ⚠ 两个 token 都读不到 exp：预检无法生效，T-0 有撞上过期 token 的风险。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="真机验证登录链路（只登录，不提交预约）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--login-url", default="",
                        help="登录入口 URL；缺省用 L3→L4 权威值（与线上一致）")
    parser.add_argument("--captcha-url", default=cfg.CAS_CAPTCHA_URL,
                        help="验证码兜底 URL；缺省用 CAS_CAPTCHA_URL")
    parser.add_argument("--user", default=os.environ.get("BB_TEST_USER", "")
                        or next(iter(sorted(cfg.AUTHORIZED_USERS)), ""),
                        help="学号；缺省依次取 BB_TEST_USER → AUTHORIZED_USERS 首个")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析重定向链与 Origin，不提交凭据")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="login_with_retry 的重试轮数（默认 2）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打开 cas_login 的调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    _hr("1. 登录入口与重定向链")
    warn_env_proxy()
    ensure_db_ready()
    entry_url = args.login_url.strip() or get_login_entry_url()
    source = "命令行指定" if args.login_url.strip() else "L3 数据库 → L4 代码默认（与线上一致）"
    print(f"入口 URL 来源      : {source}")
    try:
        trace_redirect_chain(entry_url)
    except ChainResolutionError as e:
        print(f"✗ 重定向链解析失败: {e}")
        print(f"  已成功走过的跳数 : {len(e.hops)}")
        for hop in e.hops:
            print(f"    · {hop}")
        if e.failed_url:
            print(f"  失败 URL         : {e.failed_url}")
            print(f"  失败 host        : {e.failed_host!r}")
        print("  排查建议：")
        print("   · host 是 '...' 这类畸形值 → 跑 scripts/diag_login_chain.py，")
        print("     它会打印 requests 实际使用的代理变量（值为 '...' 的代理变量会让")
        print("     urllib3 在连接阶段报 'label empty or too long'）")
        print("   · host 是 shmtu.edu.cn 之外的外域 → 学校侧登录链路改版，需更新 WF_* 配置")
        return 2
    except Exception as e:
        print(f"✗ 重定向链解析失败（网络或学校侧改版？）: {type(e).__name__}: {e}")
        return 2

    if args.dry_run:
        _hr("结果")
        print("dry-run：链路可达，未提交凭据。")
        print("确认上面的 Origin 与登录页 host 同源后，去掉 --dry-run 跑真机登录。")
        return 0

    _hr("2. 真机登录")
    username = args.user.strip()
    if not username:
        username = input("学号: ").strip()
    password = os.environ.get("BB_TEST_PASS", "")
    if not password:
        password = getpass.getpass("密码（不回显）: ")
    if not username or not password:
        print("✗ 缺少用户名或密码，已中止")
        return 2

    print(f"学号              : {username}")
    print(f"入口 URL          : {entry_url}")
    print(f"验证码兜底 URL    : {args.captcha_url}")
    print("开始登录（只登录，不提交预约）...")

    try:
        tokens = do_real_login(entry_url, args.captcha_url, username, password,
                               max_retries=args.max_retries)
    except Exception as e:
        print(f"✗ 登录过程异常: {type(e).__name__}: {e}")
        return 2

    _hr("结果")
    if not tokens or not tokens.get("access_token"):
        print("✗ 登录失败")
        print(f"  失败类型     : {LoginErrorType.UNKNOWN_ERROR.value}（login_with_retry 返回 None）")
        print("  排查建议：")
        print("   · 密码是否写错（脚本无法区分，login_with_retry 对密码错误不重试）")
        print("   · 是否被上游临时限制（稍后重试）")
        print("   · 若仅 Origin 改动后开始失败，回填 .env 里的 CAS_CAPTCHA_URL 或检查登录页 host")
        return 1

    report_tokens(tokens)
    print()
    print("结论：登录链路正常，Origin 头按登录页 host 派生**未破坏登录**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
