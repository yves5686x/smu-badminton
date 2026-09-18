# 快速开始

## 环境要求

- **Python** >= 3.11
- **pip** (Python 包管理器)
- **Git** (用于克隆仓库)
- 可选：**Docker** & **Docker Compose** (用于容器化部署)

## 安装步骤

### 1. 克隆项目

```bash
git clone https://github.com/YahelLiu/smu-badminton.git
cd smu-badminton
```

### 2. 安装依赖

推荐使用可编辑模式安装，方便开发调试：

```bash
pip install -e .
```

开发测试依赖（pytest / ruff 等）：

```bash
pip install -e ".[dev]"
```

> 若使用 [uv](https://github.com/astral-sh/uv) 管理环境，用 `uv sync` 同步依赖。
>
> ddddocr 在部分国内镜像源（如清华 tuna）可能未收录，安装失败时改用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

### 3. 配置环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

学校侧地址（WF 平台、OAuth 客户端 ID、场地资源类型 ID、CAS 验证码地址）都已有正确的
代码默认值，**不需要抄进 `.env`**——写进去就是第二份副本，将来改代码它不会跟着变。

`.env` 里只需要填**要偏离默认值**的项，通常就一项：

```env
# 授权用户（可访问任务监控页、更新运行时配置）。默认为空集合，必须显式配置
AUTHORIZED_USERS=你的学号
```

部署在反向代理后面时再加：

```env
# 可信代理 IP（配置后才信任 X-Forwarded-For 头）
TRUSTED_PROXIES=127.0.0.1
```

> `SECRET_KEY` 无需手动设置：未配置时首次启动自动生成随机密钥并持久化到
> `DATA_DIR/secret_key`，重启复用。仅在多实例共享数据库等场景才需要显式指定。
>
> 完整键清单、默认值与分层规则见[配置参数](./config.md)。

### 4. 启动服务

开发模式启动（端口 5002，可通过 `UVICORN_RELOAD=1` 开启自动重载）：

```bash
python -m smu_badminton.server_fastapi
```

或使用 uvicorn 直接启动：

```bash
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5002 --reload
```

启动成功后访问 `http://localhost:5002` 即可使用 Web 界面。

> 验证码识别使用 ddddocr 本地整图识别，首次调用时惰性加载 onnx 模型，无需预先准备任何模型文件。

### 5. 调试模式

设置环境变量 `BOOKING_DEBUG=1` 可开启详细的预约日志输出：

```bash
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi
```

## 验证安装

启动服务后，访问健康检查接口确认服务正常运行：

```bash
curl http://localhost:5002/health
```

返回 `{"ok": true}` 表示服务启动成功。

## 下一步

- [安装部署](/guide/install) — 了解 Docker 部署和生产环境配置
- [CAS 认证](/guide/cas-auth) — 了解统一认证流程和验证码处理
- [预约功能](/guide/booking) — 了解即时预约和定时预约的区别
- [API 文档](/guide/api) — 查看所有 REST API 端点详情
