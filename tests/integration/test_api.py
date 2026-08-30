"""API 集成测试。"""
import pytest


def test_health(client):
    """测试健康检查端点。"""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_config(client):
    """测试配置端点。"""
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "login_url" in r.json()["data"]


def test_jobs_list(client):
    """测试任务列表端点。"""
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_metrics(client):
    """测试指标端点。"""
    # 先请求一个端点产生指标
    client.get("/health")
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_auth_check(client):
    """测试权限检查端点。"""
    r = client.get("/api/auth/check?username=test_user")
    assert r.status_code == 200
    assert "authorized" in r.json()


def test_index(client):
    """测试首页。"""
    r = client.get("/")
    # 可能返回 HTML 或 404（如果模板不存在）
    assert r.status_code in [200, 404]


def test_book_without_credentials(client):
    """免密预约：请求不带密码且服务端无保存账号时应明确报错。"""
    from smu_badminton.token_profile import get_user_account, delete_user_account

    username = "test_nocred_user"
    delete_user_account(username)  # 确保无保存账号
    assert get_user_account(username) is None

    r = client.post("/api/book", json={
        "username": username,
        "bookdate": "2026-12-18",
        "kssj": "21:00",
        "jssj": "22:00",
        "resources_name": "羽毛球13号场地",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["error"] == "no_saved_credentials"


@pytest.mark.network
def test_captcha_api(client):
    """测试验证码获取 API（依赖外部网络，CI 默认跳过）。"""
    r = client.post("/api/captcha", json={})
    # 验证码获取可能会因为网络原因失败，所以只检查返回格式
    data = r.json()
    assert "ok" in data
    # 如果成功，检查数据格式
    if data["ok"]:
        assert "captcha_image" in data["data"]
        assert "session_id" in data["data"]
        assert data["data"]["captcha_image"].startswith("data:image/png;base64,")
