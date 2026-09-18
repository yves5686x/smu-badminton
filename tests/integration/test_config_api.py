"""配置接口集成测试。

重点锁定一件事：``GET /api/config`` 与 ``POST /api/config/update`` 必须指向**同一个**值。

此前的实现读的是 ``WF_HOME_URL``、写的是 ``.env`` 里的 ``CAS_LOGIN_URL``——弹窗显示 A、
保存成 B、重开又显示 A，从界面看是空操作；而且 Docker 下 ``.env`` 是 ``:ro`` 挂载，
那次写入必然以 ``Permission denied`` 失败。
"""
from pathlib import Path

import pytest

from smu_badminton import routes_config
from smu_badminton.config import CAS_CAPTCHA_URL, WF_HOME_URL
from smu_badminton.settings_store import set_login_entry_url

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

TEST_ADMIN = "test_config_admin"
TEST_OVERRIDE_URL = "https://example.com/cas/login?service=test"


@pytest.fixture
def admin(monkeypatch):
    """把测试管理员加进授权列表，并在用例结束后清掉运行时覆盖值。"""
    monkeypatch.setattr(routes_config, "AUTHORIZED_USERS", {TEST_ADMIN})
    set_login_entry_url("")
    yield TEST_ADMIN
    set_login_entry_url("")


def _post(client, login_url, username=TEST_ADMIN):
    return client.post(
        "/api/config/update",
        json={"login_url": login_url, "current_username": username},
    )


def test_config_get_exposes_only_consumed_fields(client):
    """只下发前端真正消费的字段；曾经的 authorize_url 无消费方，已移除。"""
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == {"login_url", "captcha_url"}
    assert data["login_url"] == WF_HOME_URL
    assert data["captcha_url"] == CAS_CAPTCHA_URL


def test_config_update_is_readable_back(client, admin):
    """写入的值必须能从同一个接口读回来（读写对称）。"""
    r = _post(client, TEST_OVERRIDE_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["login_entry_url"] == TEST_OVERRIDE_URL
    assert body["data"]["is_override"] is True

    assert client.get("/api/config").json()["data"]["login_url"] == TEST_OVERRIDE_URL


def test_config_update_empty_value_restores_default(client, admin):
    """传空串清除覆盖，回到 WF 首页。"""
    _post(client, TEST_OVERRIDE_URL)
    r = _post(client, "")
    assert r.json()["ok"] is True
    assert r.json()["data"]["is_override"] is False
    assert client.get("/api/config").json()["data"]["login_url"] == WF_HOME_URL


def test_config_update_requires_authorized_user(client, admin):
    """非授权用户不得修改运行时配置。"""
    r = _post(client, TEST_OVERRIDE_URL, username="nobody")
    assert r.json()["ok"] is False
    assert r.json()["error"] == "permission_denied"
    assert client.get("/api/config").json()["data"]["login_url"] == WF_HOME_URL


def test_config_update_rejects_non_url(client, admin):
    """只接受完整 http(s) URL，避免把无效值写进权威配置。"""
    r = _post(client, "not-a-url")
    assert r.json()["ok"] is False
    assert r.json()["error"] == "invalid_url"
    assert client.get("/api/config").json()["data"]["login_url"] == WF_HOME_URL


def test_config_update_does_not_touch_env_file(client, admin, monkeypatch):
    """更新配置不得改写部署文件——写数据库即可立即生效。

    用一个**绝不该被创建**的哨兵路径来判定，而不是临时目录：旧实现会往 ``.env`` 写入，
    那么哨兵文件就会出现。这样判定不依赖系统临时目录（沙箱环境下 ``tmp_path`` 可能不可用）。
    """
    import smu_badminton.config as config_module

    sentinel = REPO_ROOT / "tests" / "_sentinel_should_not_be_written.env"
    sentinel.unlink(missing_ok=True)
    monkeypatch.setattr(config_module, "ENV_PATH", sentinel)
    # routes_config 已不再引用 BASE_DIR/ENV_PATH，写入路径根本不存在
    assert not hasattr(routes_config, "BASE_DIR")

    try:
        _post(client, TEST_OVERRIDE_URL)
        assert not sentinel.exists(), "配置更新不应改写部署文件"
    finally:
        sentinel.unlink(missing_ok=True)
