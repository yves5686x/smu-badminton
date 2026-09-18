# 安装部署

## pip 安装（本地开发）

### 前置依赖

- Python >= 3.11
- 系统级依赖：OpenCV 运行时库（libgl1, libglib2.0-0, libgomp1 等）

### 安装步骤

```bash
# 克隆项目
git clone https://github.com/YahelLiu/smu-badminton.git
cd smu-badminton

# 可编辑模式安装（推荐开发使用）
pip install -e .

# 或安装开发依赖
pip install -e ".[dev]"
```

> 若使用 [uv](https://github.com/astral-sh/uv) 管理环境，用 `uv sync` 同步运行依赖，
> `uv sync --extra dev` 同步含测试依赖。
>
> ddddocr 在部分国内镜像源（如清华 tuna）可能未收录，安装失败时改用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

### 启动开发服务器

```bash
# 默认端口 5002
python -m smu_badminton.server_fastapi

# 自定义端口
SERVER_PORT=8080 python -m smu_badminton.server_fastapi

# 调试模式
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi

# 自动重载（开发用）
UVICORN_RELOAD=1 python -m smu_badminton.server_fastapi
```

## Docker 部署（生产环境）

### 使用 docker-compose（推荐）

这是最简单的部署方式，一条命令即可启动：

```bash
docker-compose up --build
```

服务将在 `http://localhost:5000` 启动。

### docker-compose 配置说明

编排文件就是仓库根目录的 [`docker-compose.yml`](../../../docker-compose.yml)，不再在文档里抄一份
（抄一份就会随实际文件漂移）。它做了三件事：

- 只读挂载 `./.env` 到容器内 `/app/.env`（运行时可变配置走数据库，不改这个文件）
- 用 Docker volume `smu-badminton-data` 持久化 `/app/data`
- 端口、数据目录通过 `environment:` 显式传入（L1）

端口只有一个旋钮：`SERVER_PORT` 同时作用于监听端口、端口映射与健康检查，默认 `5000`。

```bash
SERVER_PORT=8080 docker-compose up -d    # 换成 8080
```

### 手动 Docker 构建

```bash
# 构建镜像
docker build -t smu-badminton .

# 运行容器
docker run -d \
  --name smu-badminton \
  -p 5000:5000 \
  -v ./data:/app/data \
  -v ./.env:/app/.env:ro \
  -e TZ=Asia/Shanghai \
  -e SERVER_PORT=5000 \
  smu-badminton
```

### Docker 镜像说明

Dockerfile 基于 `python:3.11-slim`，安装了以下系统级依赖：

| 依赖包 | 用途 |
|--------|------|
| libgl1, libglib2.0-0, libgomp1 | OpenCV 运行时 |
| libsm6, libxext6, libxrender1 | OpenCV GUI 支持 |
| libxml2-dev, libxslt-dev | lxml 解析 |
| gcc, g++ | 编译 Python C 扩展 |

镜像内置了健康检查（`/health` 端点），每 30 秒检查一次。

## 端口说明

由 `SERVER_PORT` 决定，默认值：

| 环境 | 端口 | 来源 |
|------|------|------|
| 本地开发 | 5002 | 代码默认值（L4） |
| Docker/生产 | 5000 | `docker-compose.yml` 的 `environment:`（L1） |

端口同时作用于监听端口、端口映射与健康检查，改一处就够。详见[配置参数](./config.md)。

## 数据存储

### 数据库

系统使用 SQLite 存储数据，数据库文件位置：

- **Docker 环境**：`/app/data/`（通过 Docker volume 持久化）
- **本地开发**：项目根目录下的 `data/` 目录

SQLite 以 WAL（Write-Ahead Logging）模式运行，支持并发读写。

## 运行测试

```bash
# 运行全部测试
python -m pytest tests/ -v

# 仅运行单元测试
python -m pytest tests/unit/ -v

# 仅运行集成测试
python -m pytest tests/integration/ -v

# 运行单个测试文件
python -m pytest tests/unit/test_obfuscate.py -v

# 运行单个测试用例
python -m pytest tests/unit/test_obfuscate.py::test_roundtrip -v
```

## 验证登录链路

部署完成后，建议先用真机脚本确认登录链路可用——脚本**只登录、不提交任何预约**，
不消耗上游的预约提交频控额度：

```bash
# 1) 只解析重定向链与 Origin，不提交凭据
python scripts/verify_real_login.py --dry-run

# 2) 真机登录（学号自动取 AUTHORIZED_USERS 首个，密码交互输入不回显）
python scripts/verify_real_login.py
```

预期输出：解析到的登录页为 `https://sso.shmtu.edu.cn/cas/login?...`，登录 POST 的 `Origin`
与登录页 host 同源，最后打印 `✓ 登录成功` 及 token 剩余寿命。

若第一跳就报 `LocationParseError: Failed to parse: '...', label empty or too long`，说明运行
环境里有**值异常的代理变量**（`requests` 默认信任 `http_proxy` / `all_proxy`）——报错里的
`'...'` 是主机名字面值，不是学校侧故障。跑 `python scripts/diag_login_chain.py` 定位，
详见 [CAS 认证](./cas-auth.md)。
