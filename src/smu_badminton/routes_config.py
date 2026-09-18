"""
配置相关路由模块。

包含：配置读取、运行时配置更新。

分层规范的落点：本模块是唯一把各层**组合**成对外响应的地方。

- ``GET  /api/config``        读：L3（数据库覆盖）→ L4（代码默认），下发给前端
- ``POST /api/config/update`` 写：写入 L3（数据库 ``app_settings``）

历史问题（已修）：更新写的是 ``.env`` 里的 ``CAS_LOGIN_URL``，而读取返回的是
``WF_HOME_URL``——弹窗显示 A、保存成 B、重开又显示 A，从界面看是空操作；
且 Docker 下 ``.env`` 是 ``:ro`` 挂载，那次写入必然以 ``Permission denied`` 失败。
现在读写都指向 ``settings_store`` 的同一个权威值，且不再改动部署文件。
"""
import logging

from fastapi import APIRouter

from .config import AUTHORIZED_USERS, CAS_CAPTCHA_URL
from .schemas import UpdateConfigRequest
from .settings_store import get_login_entry_url, set_login_entry_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])


# ============= 路由定义 =============

@router.get("/config")
async def get_config():
    """获取前端所需配置。

    只下发前端真正消费的字段：``login_url`` 与 ``captcha_url``。
    此前响应里的 ``authorize_url`` 没有任何消费方，已移除。

    不做 importlib.reload：登录入口 URL 每次从 L3 直读，天然是最新值。
    """
    return {
        "ok": True,
        "data": {
            "login_url": get_login_entry_url(),
            "captcha_url": CAS_CAPTCHA_URL,
        },
    }


@router.post("/config/update")
async def update_config(req: UpdateConfigRequest):
    """更新运行时配置（需要授权用户）。

    写入 L3（数据库），保存即生效——不需要重启，也不依赖 ``.env`` 可写。
    权限检查：current_username 必须在 AUTHORIZED_USERS 中。
    """
    if req.current_username not in AUTHORIZED_USERS:
        logger.warning("权限拒绝：用户试图更新运行时配置")
        return {"ok": False, "error": "permission_denied", "message": "无权更新配置"}

    new_url = (req.login_url or "").strip()
    if new_url and not new_url.lower().startswith(("http://", "https://")):
        return {
            "ok": False,
            "error": "invalid_url",
            "message": "登录地址必须是以 http(s):// 开头的完整 URL",
        }

    if not set_login_entry_url(new_url):
        return {"ok": False, "error": "write_failed", "message": "配置写入失败，请查看服务端日志"}

    effective = get_login_entry_url()
    logger.info("运行时配置已更新: 登录入口 URL -> %s", effective)
    return {
        "ok": True,
        "data": {
            "message": "配置已保存并立即生效",
            "login_entry_url": effective,
            "is_override": bool(new_url),
        },
    }
