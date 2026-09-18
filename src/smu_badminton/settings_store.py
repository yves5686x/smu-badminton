"""
L3 运行时可变配置层（数据库 ``app_settings`` 表）。

分层规范（权威说明见 ``Documents/docs/guide/config.md``）：

- **L1 进程环境变量**：``docker-compose environment:`` / ``docker run -e`` / shell。
  只放"每台机器不同"的部署参数。进程环境优先于 ``.env``（``load_dotenv`` 不覆盖已有变量）。
- **L2 项目根 ``.env``**：部署者手工维护。所有 ``config.py`` 里声明的键都归这一层。
- **L3 本模块**：授权用户经界面修改、需要立即生效、且不能依赖写 ``.env`` 的项。
- **L4 ``config.py`` 的代码默认值**：唯一默认值来源；``.env.example`` 逐键必须等于它。

**每个配置项只归属一层**。本层目前只有「登录入口 URL」一项——它此前被塞在 ``.env`` 里，
由 ``POST /api/config/update`` 原地改写 ``.env`` 再 ``importlib.reload``，而 Docker 下 ``.env``
是 ``:ro`` 挂载，那条写入必然失败，且 reload 只更新 config 模块字典、
``from .config import X`` 的模块仍持有旧值。
"""
import logging
import time

logger = logging.getLogger(__name__)

# 登录入口 URL 在 app_settings 表中的键名
LOGIN_ENTRY_URL_KEY = "login_entry_url"


def get_setting(key: str) -> str:
    """读取 L3 覆盖值。未设置返回空串（由调用方回退到默认层）。"""
    from .core_utils import get_db_pool

    try:
        with get_db_pool().get_connection() as conn:
            cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cur.fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception as e:
        logger.warning("读取运行时配置 %s 失败: %s", key, e)
        return ""


def set_setting(key: str, value: str) -> bool:
    """写入 L3 覆盖值。传空串等价于删除该覆盖（回退到默认层）。"""
    from .core_utils import get_db_pool

    try:
        with get_db_pool().get_connection() as conn:
            if value:
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, time.time()),
                )
            else:
                conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        return True
    except Exception as e:
        logger.error("写入运行时配置 %s 失败: %s", key, e)
        return False


def get_login_entry_url() -> str:
    """登录入口 URL 的唯一权威解析：L3 覆盖 → L4 默认（WF 首页）。

    ``/api/config`` 下发与 ``/api/config/update`` 写入都走这里，读与写指向同一个值——
    修掉此前"弹窗显示 WF 首页、保存进 CAS_LOGIN_URL、重开又显示 WF 首页"的不对称。

    每次调用直读数据库（本地 SQLite 读，微秒级）：与 ``get_user_account`` 等一致，
    换来的是"界面改完立即生效"，不需要任何缓存失效协调。
    """
    from .config import WF_HOME_URL

    return get_setting(LOGIN_ENTRY_URL_KEY) or WF_HOME_URL


def set_login_entry_url(url: str) -> bool:
    """设置登录入口 URL。传空串则清除覆盖，回到 L4 默认值。"""
    return set_setting(LOGIN_ENTRY_URL_KEY, (url or "").strip())
