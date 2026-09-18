"""冒烟测试 - 验证基本导入和健康检查。"""


def test_import_server():
    from smu_badminton import server_fastapi
    assert hasattr(server_fastapi, 'app')


def test_import_cas_manager():
    from smu_badminton import cas_manager
    assert hasattr(cas_manager, 'BookingManager')
    assert hasattr(cas_manager, 'booking_manager')


def test_import_config():
    from smu_badminton import config
    assert hasattr(config, 'WF_ORIGIN')
    assert hasattr(config, 'code_defaults')


def test_app_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
