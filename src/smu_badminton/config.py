"""
配置层（L1 / L2 / L4）—— 全项目唯一的默认值来源。

分层规范（权威说明见 ``Documents/docs/guide/config.md``）：

- **L1 进程环境变量**：``docker-compose environment:`` / ``docker run -e`` / shell。
  只放"每台机器不同"的部署参数。**进程环境优先于 ``.env``**（``load_dotenv`` 不覆盖已有变量）。
- **L2 项目根 ``.env``**：部署者手工维护，只放确实要偏离默认值的项。
- **L3 运行时可变配置**：``settings_store.py``（数据库 ``app_settings`` 表），界面可改、立即生效。
- **L4 代码默认值**：本模块的 ``_DEFAULT_SPEC``，唯一默认值来源。

规则：
1. **一个配置项只归属一层**，不在多处重复定义。
2. ``.env.example`` 逐键必须等于 ``code_defaults()``（由契约测试锁定）。
3. **派生值不单独配置**：``WF_HOME_URL`` / ``WF_CAPTCHA_URL`` 由 ``WF_ORIGIN`` 派生；
   CAS 的验证码 URL 与 ``Origin`` 头由实际解析出的登录页 URL 派生（见 ``cas_login``）。
4. L1/L2 是启动期配置，允许在模块导入时取值；**L3 必须每次读取**，不得快照。
5. 抢票时序常量（``PREFETCH_WINDOW_SEC`` 等）是**算法常量不是部署配置**，留在
   ``cas_manager.py``，不通过环境变量调整。
"""
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import load_dotenv

# 项目根目录（代码根 = 模板/静态资源/数据的锚点）
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 加载项目根 .env（不覆盖已有进程环境变量，从而保证 L1 > L2）
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH, override=False)

# ========== 代码默认值表（L4）==========
# 唯一默认值来源。派生项写成 "{BASE_DIR}/..." / "{WF_ORIGIN}/..." 占位形式，
# 由 code_defaults() 按插入顺序展开（派生项必须排在被依赖项之后）。
# 新增配置项只能加在这里，并同步 .env.example——否则契约测试会失败。
_DEFAULT_SPEC: dict[str, str] = {
    # 微服务平台
    "WF_ORIGIN": "https://wf.shmtu.edu.cn",
    "WF_API_URL": "https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys",
    "WF_HOME_URL": "{WF_ORIGIN}/yy-sys/pc/home",
    "WF_SSO_AUTHORIZE_PATH": "/sso/oauth2/authorize",
    "WF_CAPTCHA_URL": "{WF_ORIGIN}/yy-sys/captcha",
    # CAS 认证。登录页 host 由 cas_login 沿重定向链解析，验证码 URL 与 Origin 头
    # 都按其同源派生；这里的 CAS_CAPTCHA_URL 只在解析失败时兜底。
    "CAS_CAPTCHA_URL": "https://sso.shmtu.edu.cn/cas/captcha",
    # OAuth / 业务资源
    "OAUTH_CLIENT_ID": "kwxKbMKq3Nafw2mApFZz",
    "BADMINTON_TYPE_ID": "93c2a115-5c73-4e30-bb6a-dfcc5404e46f",
    # 运行参数
    "SERVER_PORT": "5002",
    "BOOKING_DEBUG": "0",
    "UVICORN_RELOAD": "0",
    # 缓存与保留期（秒）
    "TOKEN_CACHE_TTL_SEC": "900",
    "TOKEN_PROFILE_TTL_SEC": "3600",
    "JOB_RETENTION_SEC": "3600",
    # 数据目录
    "DATA_DIR": "{BASE_DIR}/data",
    # 安全
    "AUTHORIZED_USERS": "",
    "TRUSTED_PROXIES": "",
    # 限流
    "RATE_LIMIT_MAX": "30",
    "RATE_LIMIT_WINDOW": "10",
    "RATE_LIMIT_JOBS_MAX": "300",
    "RATE_LIMIT_JOBS_WINDOW": "60",
    # 用户信息默认值
    "DEFAULT_DEPT_CODE": "",
    "DEFAULT_DEPT_NAME": "",
    "DEFAULT_DEPT_NAME_EN": "",
    "DEFAULT_USER_EMAIL": "",
    "DEFAULT_USER_PHONE": "",
}

# 真值型开关的解析口径
_TRUTHY = {"1", "true", "yes", "on"}


def code_defaults() -> dict[str, str]:
    """纯代码默认值（不受任何环境变量 / .env 影响）。

    契约测试用它逐键比对 ``.env.example``，保证"模板即默认值"不再漂移。
    """
    resolved: dict[str, str] = {}
    for name, spec in _DEFAULT_SPEC.items():
        resolved[name] = spec.format(BASE_DIR=BASE_DIR, **resolved)
    return resolved


def _resolved_config() -> dict[str, str]:
    """展开后的有效配置：进程环境 / .env 优先，否则用代码默认值。

    空字符串视为"未设置"，回退到默认值——避免 ``KEY=`` 这类空行把默认值抹掉。
    """
    resolved: dict[str, str] = {}
    for name, spec in _DEFAULT_SPEC.items():
        env_value = os.environ.get(name, "")
        resolved[name] = env_value if env_value else spec.format(BASE_DIR=BASE_DIR, **resolved)
    return resolved


_CFG = _resolved_config()


def _as_bool(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _as_positive_int(name: str, value: str) -> int:
    """解析正整数配置。非法值记警告并回退到代码默认值，避免半路抛异常起不来。"""
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    fallback = int(code_defaults()[name])
    logging.getLogger(__name__).warning("%s=%r 非法，回退到默认值 %d", name, value, fallback)
    return fallback


def _as_set(name: str) -> set[str]:
    """解析逗号分隔列表，统一去空白、丢空项。

    此前直接 ``split(",")`` 不 strip：``TRUSTED_PROXIES=127.0.0.1, 10.0.0.1``
    会得到带前导空格的 ``" 10.0.0.1"``，代理判定必然失配。
    """
    return {item.strip() for item in _CFG[name].split(",") if item.strip()}


# ========== 微服务平台 ==========
WF_ORIGIN = _CFG["WF_ORIGIN"]
WF_API_URL = _CFG["WF_API_URL"]
WF_HOME_URL = _CFG["WF_HOME_URL"]
WF_SSO_AUTHORIZE_PATH = _CFG["WF_SSO_AUTHORIZE_PATH"]
WF_CAPTCHA_URL = _CFG["WF_CAPTCHA_URL"]

# ========== CAS 认证 ==========
CAS_CAPTCHA_URL = _CFG["CAS_CAPTCHA_URL"]

# ========== OAuth / 业务资源 ==========
OAUTH_CLIENT_ID = _CFG["OAUTH_CLIENT_ID"]
BADMINTON_TYPE_ID = _CFG["BADMINTON_TYPE_ID"]

# ========== 运行参数 ==========
SERVER_PORT = _as_positive_int("SERVER_PORT", _CFG["SERVER_PORT"])
BOOKING_DEBUG = _as_bool(_CFG["BOOKING_DEBUG"])
UVICORN_RELOAD = _as_bool(_CFG["UVICORN_RELOAD"])

# ========== 缓存与保留期 ==========
TOKEN_CACHE_TTL_SEC = _as_positive_int("TOKEN_CACHE_TTL_SEC", _CFG["TOKEN_CACHE_TTL_SEC"])
TOKEN_PROFILE_TTL_SEC = _as_positive_int("TOKEN_PROFILE_TTL_SEC", _CFG["TOKEN_PROFILE_TTL_SEC"])
JOB_RETENTION_SEC = _as_positive_int("JOB_RETENTION_SEC", _CFG["JOB_RETENTION_SEC"])

# ========== 数据目录 ==========
# 只认环境变量与默认值，不再用 os.path.exists("/app/data") 做隐式环境探测：
# Docker 里 BASE_DIR 就是 /app，默认值已经等于 /app/data，那层探测是冗余的，
# 却把"环境身份"藏进了文件系统状态。
DATA_DIR = _CFG["DATA_DIR"]

# ========== 安全 ==========


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

# 授权用户列表（可访问任务监控页 / 更新运行时配置）。
# 默认空集合：不再把某个具体学号写进源码——那既把个人标识固化进代码，
# 也让"没配就等于配好了"这种误解放行所有人。未配置时启动日志给出告警。
AUTHORIZED_USERS = _as_set("AUTHORIZED_USERS")

# 可信代理 IP 列表（用于 X-Forwarded-For 验证）
# 只有来自可信代理的请求才会信任 X-Forwarded-For 头
# 示例：TRUSTED_PROXIES=127.0.0.1,10.0.0.1
TRUSTED_PROXIES = _as_set("TRUSTED_PROXIES")

# ========== 限流 ==========
RATE_LIMIT_MAX = _as_positive_int("RATE_LIMIT_MAX", _CFG["RATE_LIMIT_MAX"])
RATE_LIMIT_WINDOW = _as_positive_int("RATE_LIMIT_WINDOW", _CFG["RATE_LIMIT_WINDOW"])
RATE_LIMIT_JOBS_MAX = _as_positive_int("RATE_LIMIT_JOBS_MAX", _CFG["RATE_LIMIT_JOBS_MAX"])
RATE_LIMIT_JOBS_WINDOW = _as_positive_int("RATE_LIMIT_JOBS_WINDOW", _CFG["RATE_LIMIT_JOBS_WINDOW"])

# ========== 用户信息默认值 ==========
DEFAULT_DEPT_CODE = _CFG["DEFAULT_DEPT_CODE"]
DEFAULT_DEPT_NAME = _CFG["DEFAULT_DEPT_NAME"]
DEFAULT_DEPT_NAME_EN = _CFG["DEFAULT_DEPT_NAME_EN"]
DEFAULT_USER_EMAIL = _CFG["DEFAULT_USER_EMAIL"]
DEFAULT_USER_PHONE = _CFG["DEFAULT_USER_PHONE"]


def get_missing_required_settings() -> list[str]:
    """返回对生产部署有实际影响、但当前未配置的项（启动时告警用）。"""
    missing = []
    if not AUTHORIZED_USERS:
        missing.append(
            "AUTHORIZED_USERS 未设置：任务监控页与运行时配置更新对所有用户关闭"
        )
    return missing


# ========== 派生 URL ==========


def build_wf_authorize_url(ret_url: str | None = None, state: str | None = None, nonce: str | None = None) -> str:
    """构造 WF OAuth2 授权 URL（未认证时会跳转到 CAS 登录页）。

    全项目**唯一**的授权 URL 实现：``cas_login`` 的重定向链入口也用它。
    此前 ``cas_login`` 里那份硬编码 ``/sso/oauth2/authorize`` 的私有副本已删除，
    授权路径统一由 ``WF_SSO_AUTHORIZE_PATH`` 控制。
    """
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
