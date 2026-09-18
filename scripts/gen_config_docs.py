#!/usr/bin/env python
"""从 ``config.code_defaults()`` 生成 ``Documents/docs/guide/config.md`` 里的环境变量表。

存在意义：**文档不再手抄默认值**。此前的默认值散落在 README、config.md、
install.md、cas-auth.md、faq.md、quick-start.md 六处，改代码不改文档是常态。
现在权威表由代码生成，并由 ``tests/unit/test_config_layering.py`` 校验"是否过期"。

用法::

    python scripts/gen_config_docs.py          # 就地更新表格
    python scripts/gen_config_docs.py --check   # 只校验，落后则退出码 1
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

BEGIN = "<!-- BEGIN:GENERATED:ENV_TABLE (由 scripts/gen_config_docs.py 生成，请勿手改) -->"
END = "<!-- END:GENERATED:ENV_TABLE -->"

DOC_PATH = REPO_ROOT / "Documents" / "docs" / "guide" / "config.md"

# 每个键的说明。新增配置项时同步补充，否则生成器会报错提醒。
DESCRIPTIONS: dict[str, str] = {
    "WF_ORIGIN": "微服务平台地址",
    "WF_API_URL": "GraphQL 接口地址",
    "WF_HOME_URL": "微服务平台首页（派生自 `WF_ORIGIN`），默认登录入口",
    "WF_SSO_AUTHORIZE_PATH": "OAuth2 授权路径（拼装授权 URL 用）",
    "WF_CAPTCHA_URL": "滑块验证码接口（派生自 `WF_ORIGIN`）",
    "CAS_CAPTCHA_URL": "CAS 验证码接口兜底地址（登录页 host 解析失败时才用）",
    "OAUTH_CLIENT_ID": "OAuth2 客户端标识",
    "BADMINTON_TYPE_ID": "羽毛球场地资源类型 ID",
    "SERVER_PORT": "服务监听端口（Docker 内由 compose 覆盖为 `5000`）",
    "BOOKING_DEBUG": "预约调试日志开关，`1` 开启",
    "UVICORN_RELOAD": "uvicorn 热重载开关，`1` 开启",
    "TOKEN_CACHE_TTL_SEC": "Token 缓存有效期（秒）",
    "TOKEN_PROFILE_TTL_SEC": "用户 Profile 缓存有效期（秒）",
    "JOB_RETENTION_SEC": "已完成任务保留时长（秒），超期由后台清理任务删除",
    "DATA_DIR": "SQLite 数据目录（Docker 内由 compose 设为 `/app/data`）",
    "AUTHORIZED_USERS": "授权用户列表，逗号分隔；**默认为空，必须显式配置**",
    "TRUSTED_PROXIES": "可信代理 IP 列表，逗号分隔（配置后才信任 `X-Forwarded-For`）",
    "RATE_LIMIT_MAX": "受保护接口（`/api/book`、`/api/book/schedule`、`/api/availability`）限流上限",
    "RATE_LIMIT_WINDOW": "上述接口限流窗口（秒）",
    "RATE_LIMIT_JOBS_MAX": "任务接口（`/api/jobs` 前缀）限流上限",
    "RATE_LIMIT_JOBS_WINDOW": "任务接口限流窗口（秒）",
    "DEFAULT_DEPT_CODE": "JWT 缺少部门代码时的兜底值",
    "DEFAULT_DEPT_NAME": "JWT 缺少部门名称时的兜底值",
    "DEFAULT_DEPT_NAME_EN": "JWT 缺少英文部门名时的兜底值",
    "DEFAULT_USER_EMAIL": "JWT 缺少邮箱时的兜底值",
    "DEFAULT_USER_PHONE": "JWT 缺少电话时的兜底值",
}

# 不在 code_defaults() 里的特殊项（有独立解析逻辑）
EXTRA_ROWS: list[tuple[str, str, str]] = [
    ("SECRET_KEY", "自动生成", "密码混淆密钥。未配置时首次启动生成随机密钥并持久化到 `DATA_DIR/secret_key`，重启复用"),
]


def render_table() -> str:
    from smu_badminton import config  # noqa: PLC0415  延迟导入：先设好 sys.path

    defaults = config.code_defaults()
    missing = [k for k in defaults if k not in DESCRIPTIONS]
    if missing:
        raise SystemExit(
            "以下配置项在 config.py 里有默认值，但 scripts/gen_config_docs.py 的 "
            f"DESCRIPTIONS 缺说明，请补充：{missing}"
        )

    lines = [BEGIN, "", "| 变量名 | 默认值 | 说明 |", "| --- | --- | --- |"]
    for name, value in defaults.items():
        if name == "DATA_DIR":
            # 默认值是机器相关的绝对路径，文档里写成占位形式
            shown = "`<项目根>/data`"
        else:
            shown = f"`{value}`" if value else "（空）"
        lines.append(f"| `{name}` | {shown} | {DESCRIPTIONS[name]} |")
    for name, value, desc in EXTRA_ROWS:
        lines.append(f"| `{name}` | {value} | {desc} |")
    lines.append("")
    lines.append(END)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验表格是否是最新的")
    args = parser.parse_args()

    table = render_table()
    content = DOC_PATH.read_text(encoding="utf-8")

    if BEGIN not in content or END not in content:
        raise SystemExit(f"未在 {DOC_PATH} 找到生成块标记：\n  {BEGIN}\n  {END}")

    head = content.split(BEGIN, 1)[0]
    tail = content.split(END, 1)[1]
    updated = f"{head}{table}{tail}"

    if updated == content:
        print("配置表已是最新")
        return 0

    if args.check:
        print(
            "配置表已过期：config.py 的默认值与 Documents/docs/guide/config.md 不一致。\n"
            "运行 `python scripts/gen_config_docs.py` 重新生成。",
            file=sys.stderr,
        )
        return 1

    DOC_PATH.write_text(updated, encoding="utf-8")
    print(f"已更新 {DOC_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    # 导入 config 会触发 SECRET_KEY 落盘，指到临时目录避免在仓库里生成密钥文件
    import tempfile  # noqa: PLC0415

    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="bb_docgen_"))
    sys.exit(main())
