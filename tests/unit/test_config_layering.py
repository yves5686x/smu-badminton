"""配置分层契约测试。

锁定 ``Documents/docs/guide/config.md`` 里定义的分层规范，防止配置再次散开：

- L4（``config.py`` 的 ``_DEFAULT_SPEC``）是唯一默认值来源
- ``.env.example`` 是它的镜像：激活行必须逐键等于默认值，且必须列全所有键
- ``.env`` 只放**确实偏离默认值**的项
- 派生值不单独配置（``CAS_ORIGIN`` 之类已删除，不得复活）
- 文档里的配置表由代码生成，不得落后于代码
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from smu_badminton import config

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_REAL = REPO_ROOT / ".env"
DOCS_TABLE = REPO_ROOT / "Documents" / "docs" / "guide" / "config.md"

# 已按分层规范删除的配置项：派生值或改由 L3 承载，文档不得再教人去配
REMOVED_CONFIG_KEYS = ("CAS_ORIGIN", "CAS_LOGIN_URL")

# .env 不入库（含部署方的真实取值），CI 上没有这个文件，相关用例跳过
needs_real_env = pytest.mark.skipif(
    not ENV_REAL.exists(), reason=".env 不存在（CI 环境），跳过部署配置校验"
)


def _parse_env_file(path: Path) -> tuple[dict[str, str], set[str]]:
    """解析 env 文件，返回 (激活键值对, 被注释掉的键集合)。"""
    active: dict[str, str] = {}
    commented: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"#\s*([A-Z][A-Z0-9_]*)=", line)
            if m:
                commented.add(m.group(1))
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            active[key.strip()] = value.strip()
    return active, commented


# ============= .env.example 是默认值的镜像 =============


def test_env_example_active_values_equal_code_defaults():
    """模板里激活的行，取值必须等于代码默认值。

    这是"文档不再手抄默认值"的执行点：模板一旦写出与代码不符的默认值，测试就失败。
    """
    defaults = config.code_defaults()
    active, _ = _parse_env_file(ENV_EXAMPLE)

    mismatched = {
        key: (value, defaults.get(key))
        for key, value in active.items()
        if key in defaults and value != defaults[key]
    }
    assert not mismatched, (
        f".env.example 激活行与 config.py 默认值不一致（键: (模板值, 默认值)）：{mismatched}"
    )


def test_env_example_covers_every_config_key():
    """每个配置项都必须在模板里出现（激活或注释掉），否则模板不再是完整清单。"""
    defaults = config.code_defaults()
    active, commented = _parse_env_file(ENV_EXAMPLE)
    listed = set(active) | commented

    missing = sorted(set(defaults) - listed)
    assert not missing, f".env.example 缺少以下配置项（既未激活也未注释）：{missing}"


# ============= .env 只放偏离默认值的项 =============


@needs_real_env
def test_env_real_keys_are_known():
    """``.env`` 里不允许出现配置清单之外的键（拼错键名会静默失效）。"""
    defaults = config.code_defaults()
    active, _ = _parse_env_file(ENV_REAL)

    unknown = sorted(set(active) - set(defaults))
    assert not unknown, f".env 出现未知配置项（拼写错误？）：{unknown}"


@needs_real_env
def test_env_real_only_contains_deviations():
    """``.env`` 里每个键都必须**偏离**默认值。

    与默认值相同的键写进 ``.env`` 就是第二份副本：代码改默认值后它不会跟着变，
    正是这次配置收敛要消除的漂移来源。
    """
    defaults = config.code_defaults()
    active, _ = _parse_env_file(ENV_REAL)

    redundant = sorted(k for k, v in active.items() if k in defaults and v == defaults[k])
    assert not redundant, (
        f".env 里以下键与代码默认值相同，属于多余副本，请删除：{redundant}"
    )


# ============= 派生值与已删配置项不得复活 =============


def test_derived_and_removed_config_symbols_stay_removed():
    """``CAS_ORIGIN`` / ``CAS_LOGIN_URL`` / ``get_frontend_config`` 已按分层规范删除。

    - ``CAS_ORIGIN``：派生值（登录 POST 的 ``Origin`` 头按解析出的登录页 host 派生）
    - ``CAS_LOGIN_URL``：已改为 L3 运行时可变配置（``settings_store``）
    - ``get_frontend_config``：会同时触碰 L3 与 L4，已移出 ``config.py``
    """
    for symbol in ("CAS_ORIGIN", "CAS_LOGIN_URL", "get_frontend_config"):
        assert not hasattr(config, symbol), f"{symbol} 已按分层规范移除，不应重新出现"


def test_routes_config_no_longer_writes_deploy_files():
    """配置更新走数据库，不得再改写 ``.env`` 或 reload 配置模块。

    旧实现写入 Docker 下只读挂载的 ``.env``（必然失败），且 reload 后
    ``from .config import X`` 的模块仍持有旧值。

    用 AST 判定"代码里真的没做"，避免被解释这段历史的注释/文档字符串误伤。
    """
    import ast

    path = REPO_ROOT / "src" / "smu_badminton" / "routes_config.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "importlib" not in imported, "配置更新不应再 reload 配置模块"
    assert "dotenv" not in imported, "配置更新不应再读写 .env"

    # 不得出现以写模式打开文件（读取/只读场景不受影响）
    write_modes = {"w", "a", "w+", "a+", "wb", "ab"}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open"):
            continue
        modes = [a.value for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        modes += [
            k.value.value
            for k in node.keywords
            if k.arg == "mode" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)
        ]
        assert not any(m in write_modes for m in modes), "配置更新不应再改写部署文件"


def test_data_dir_default_has_no_implicit_environment_probe():
    """``DATA_DIR`` 默认值不得再依赖 ``os.path.exists('/app/data')`` 这类隐式探测。

    Docker 里 BASE_DIR 就是 /app，默认值天然等于 /app/data，那层探测是冗余的，
    却把"环境身份"藏进了文件系统状态。用 AST 判定，避免被解释该决定的注释误伤。
    """
    import ast

    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    probes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "exists"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "path"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
    ]
    assert not probes, "config.py 不应再做基于文件系统存在的隐式环境探测"
    assert config.code_defaults()["DATA_DIR"] == str(config.BASE_DIR / "data")


def test_authorized_users_default_is_empty():
    """默认值里不得写死具体学号——那既是把个人标识固化进代码，也误导"没配就等于配好了"。"""
    assert config.code_defaults()["AUTHORIZED_USERS"] == ""
    assert "202540510004" not in Path(config.__file__).read_text(encoding="utf-8")


# ============= 解析口径 =============


def test_set_config_strips_whitespace_and_drops_empties(monkeypatch):
    """逗号分隔列表要 strip：``"a, b"`` 若留前导空格会让 ``TRUSTED_PROXIES`` 判定失配。"""
    monkeypatch.setitem(config._CFG, "TRUSTED_PROXIES", "127.0.0.1, 10.0.0.1 ,, ")
    assert config._as_set("TRUSTED_PROXIES") == {"127.0.0.1", "10.0.0.1"}


def test_invalid_int_falls_back_to_default(monkeypatch):
    """非法数值不抛异常，回退到代码默认值并告警——配置写错不该让服务起不来。"""
    assert config._as_positive_int("RATE_LIMIT_MAX", "abc") == int(config.code_defaults()["RATE_LIMIT_MAX"])
    assert config._as_positive_int("RATE_LIMIT_MAX", "0") == int(config.code_defaults()["RATE_LIMIT_MAX"])


# ============= 文档表与代码同步 =============


def test_config_doc_table_is_not_stale():
    """``config.md`` 的配置表由 ``scripts/gen_config_docs.py`` 生成，必须与代码同步。"""
    assert DOCS_TABLE.exists(), f"缺少配置权威文档：{DOCS_TABLE}"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_config_docs.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        "配置文档表已过期，请运行 python scripts/gen_config_docs.py\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "CLAUDE.md",
        "Documents/docs/guide/install.md",
        "Documents/docs/guide/cas-auth.md",
        "Documents/docs/guide/config.md",
        "Documents/docs/guide/quick-start.md",
        "Documents/docs/guide/faq.md",
    ],
)
def test_no_doc_presents_removed_keys_as_configurable(doc):
    """文档不得再把已删除的配置项当作"可配置项"介绍。

    判定的是**形态**而不是出现与否——记录"某键已删除"的迁移说明是应该保留的信息，
    真正的漂移是这些形态：赋值 ``CAS_ORIGIN=...``、列表项 ``- `CAS_ORIGIN` - ...``、
    表格行——它们都在提示读者去配置一个已不存在的键。
    """
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    offenders = []
    for line in text.splitlines():
        if not any(key in line for key in REMOVED_CONFIG_KEYS):
            continue
        stripped = line.strip()
        is_assignment = any(re.search(rf"\b{key}\s*=", line) for key in REMOVED_CONFIG_KEYS)
        is_list_or_table = bool(re.match(r"^[-*|]\s*", stripped))
        if is_assignment or is_list_or_table:
            offenders.append(stripped[:120])
    assert not offenders, (
        f"{doc} 仍把已删除的配置项当成可配置项介绍：{offenders}"
    )
