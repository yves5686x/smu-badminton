"""Pytest 配置和共享 fixtures。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

# 在导入 smu_badminton 之前隔离运行环境（conftest 先于测试模块加载，
# config.py 在 import 时读取这些 env）：
# - DATA_DIR 指向临时目录：测试绝不读写开发库 data/data.db
# - SECRET_KEY 固定：不依赖自动生成的密钥文件，结果可复现
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bb_test_data_")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["BOOKING_DEBUG"] = "0"


@pytest.fixture(autouse=True, scope="session")
def init_db():
    """建表（幂等），全部测试共享同一个临时库。"""
    from smu_badminton.core_utils import init_db_tables
    init_db_tables()


@pytest.fixture(scope="session")
def client():
    """共享 TestClient。

    用 with 启动以执行 lifespan（数据库初始化、后台清理任务按真实启动路径运行），
    全部集成测试复用同一实例，避免重复构建 app 状态。
    """
    from fastapi.testclient import TestClient
    from smu_badminton.server_fastapi import app
    with TestClient(app) as c:
        yield c
