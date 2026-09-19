# SMU Badminton

上海海事大学羽毛球场预约系统，提供 Web 界面与 REST API，支持 CAS 统一认证、即时预约、定时抢场、验证码 OCR、本地任务持久化与基础监控能力。

仓库内已有一套 VitePress 文档，`README.md` 的目标是让你先把服务跑起来，再快速找到更细的说明。

## 功能概览

- CAS 统一认证登录，支持自动 OCR 或手动输入验证码，token 支持静默续期（免反复重登）
- 即时预约：异步任务化提交，格子实时反馈「抢位中」，结果自动轮询刷新
- 定时预约抢场流水线：时钟偏移校准（T-0 前零对时请求）、验证码预取池、≤2 发并发上限、成功即停
- 取消即撤销：对接学校官方接口真实取消预约（非仅删除本地记录）
- 场地可用性查询，60 秒公共缓存 + single-flight 合并在途查询
- SQLite 持久化存储本地预约记录和任务状态
- Web 页面、REST API、健康检查与基础指标接口

## 技术栈

- Python >= 3.11
- FastAPI + Uvicorn
- SQLite（WAL 模式）
- Requests + BeautifulSoup + lxml
- ddddocr（算术验证码整图识别，纯本地推理，无需额外服务或模型文件）
- pycryptodome（captchaCode AES 加密复现）
- VitePress（项目文档站）

## 目录结构

```text
src/smu_badminton/      FastAPI 服务与核心业务逻辑
  routes_auth.py        认证路由（验证码/登录/登出/续期）
  routes_booking.py     预约路由（即时/定时/可用性/本地记录）
  routes_jobs.py        任务路由（任务列表/停止/metrics）
  routes_config.py      配置路由（读取/热更新）
  cas_login.py          CAS 认证流程（含验证码 JSON + token 同源抓取）
  cas_ocr.py            ddddocr 算术验证码识别
  booking_service.py    统一预约规则、占位与取消用例
  booking_store.py      任务/占位持久化与原子状态转换
  cas_manager.py        后台任务执行、等待、预取与提交
  credentials.py        预约凭据解析与 token 获取
  availability.py       公共缓存与个人预约合并
  booking_api.py        资源/时段/预约的 GraphQL 调用
  token_profile.py      token 与用户账号凭据缓存（含会话 exp 解析）
  core_utils.py         线程安全 SQLite 连接池、异常、密码混淆
  config.py             .env 配置加载
templates/              Web 页面模板（index.html / jobs.html）
static/                 前端静态资源
tests/                  单元测试与集成测试
scripts/                上游行为实测与链路验证脚本（验证码复用性/频控阈值/真机登录）
Documents/docs/         VitePress 文档
.env.example            示例环境变量
docker-compose.yml      Docker Compose 启动配置
Dockerfile              镜像构建文件
```

架构边界、状态与流程、数据库升级说明见[开发结构](Documents/docs/guide/architecture.md)。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/YahelLiu/smu-badminton.git
cd smu-badminton
```

### 2. 安装依赖

推荐使用可编辑安装：

```bash
pip install -e .
```

开发测试依赖：

```bash
pip install -e ".[dev]"
```

> 依赖清单统一维护在 `pyproject.toml`（运行依赖见 `[project].dependencies`），无单独的 requirements.txt。

> 若使用 [uv](https://github.com/astral-sh/uv) 管理环境，用 `uv sync` 同步运行依赖，`uv sync --extra dev` 同步含测试依赖。

> ddddocr 在部分国内镜像源（如清华 tuna）可能未收录，安装失败时改用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

### 3. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

`.env` 只需保留**要偏离默认值**的项——与默认值相同的键写了就是第二份副本，会随代码漂移。
`.env.example` 列出了全部键及其代码默认值。

最少建议确认这两项：

```env
# 授权用户（可访问任务监控页、更新运行时配置）。默认为空集合，必须显式配置
AUTHORIZED_USERS=你的学号

# 可信代理 IP（配置后才信任 X-Forwarded-For 头）
TRUSTED_PROXIES=127.0.0.1
```

> `SECRET_KEY` 无需手动设置：未配置时首次启动自动生成随机密钥并持久化到
> `DATA_DIR/secret_key`，重启复用；仅在多实例共享数据库等场景才需要显式指定。

> 登录入口 URL 不在 `.env` 里：它是运行时可变配置，存在数据库 `app_settings` 表，
> 可在任务监控页界面上修改、保存即生效。默认值为 WF 首页。
>
> CAS 登录页已由 `cas.shmtu.edu.cn` 迁至 `sso.shmtu.edu.cn`。登录页 host 由代码沿重定向链
> 解析，验证码 URL 与登录 POST 的 `Origin` 头都按其同源派生，无需手工配置这两个值。

### 4. 启动服务

本地开发默认端口为 `5002`：

```bash
python -m smu_badminton.server_fastapi
```

也可以直接使用 `uvicorn`：

```bash
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5002 --reload
```

启动后可访问：

- Web 首页：`http://localhost:5002/`
- 任务监控页：`http://localhost:5002/jobs`
- 健康检查：`http://localhost:5002/health`
- Swagger：`http://localhost:5002/docs`

开启详细日志：

```bash
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi
```

## Docker 部署

### Docker Compose

```bash
docker-compose up --build
```

默认会将服务暴露在 `http://localhost:5000`。

`docker-compose.yml` 中做了这些事情：

- 只读挂载 `./.env` 到容器内 `/app/.env`（运行时可变配置走数据库，不改这个文件）
- 使用 Docker volume 持久化 `/app/data`
- 对 `/health` 做健康检查（定义在 `Dockerfile`，端口跟随 `SERVER_PORT`）

端口只有一个旋钮：改 `SERVER_PORT` 会同时作用于监听端口、端口映射与健康检查。

### 手动构建

```bash
docker build -t smu-badminton .
docker run -d \
  --name smu-badminton \
  -p 5000:5000 \
  -v ./data:/app/data \
  -v ./.env:/app/.env:ro \
  -e TZ=Asia/Shanghai \
  -e SERVER_PORT=5000 \
  smu-badminton
```

## 配置

所有配置项、默认值与分层规则见 **[Documents/docs/guide/config.md](Documents/docs/guide/config.md)**。
这里只讲放哪儿：

| 想改什么 | 放哪里 | 生效时机 |
| --- | --- | --- |
| 每台机器不同的部署参数（端口、数据目录、时区、可信代理） | 进程环境变量（compose 的 `environment:` 或 `docker run -e`） | 启动时 |
| 学校侧地址、限流阈值、缓存 TTL | 项目根 `.env` | 启动时 |
| 登录入口 URL | 任务监控页界面（存数据库 `app_settings`） | 立即生效 |

本地开发常用：

```bash
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi   # 输出详细预约日志
SERVER_PORT=8080 python -m smu_badminton.server_fastapi  # 换端口
```

## API 概览

服务主要提供这些接口：

- `POST /api/captcha`：获取验证码图片（base64 + session_id）
- `POST /api/login`：登录并获取 Token（不带 captcha_code 则自动 OCR）
- `POST /api/auth/refresh`：用服务端保存的账号静默续期
- `GET /api/auth/check`：检查用户是否在授权列表
- `POST /api/logout`：清除 Token 缓存
- `POST /api/availability`：查询场地可用性
- `POST /api/book`：即时预约（同步语义，供程序化调用）
- `POST /api/jobs/immediate`：即时预约（异步任务，前端默认）
- `POST /api/book/schedule`：定时预约（统一后台执行）
- `POST /api/jobs/scheduled`：定时预约任务（异步）
- `GET /api/jobs`：查询任务列表，支持 `?username=` 过滤
- `GET /api/schedule/{job_id}`：查询单个任务状态
- `POST /api/jobs/{job_id}/stop`：停止指定任务
- `POST /api/jobs/stop_by_params`：按参数停止并尽力撤销学校侧预约
- `GET/POST /api/local_bookings`：查看/保存本地预约记录
- `GET /api/config` / `POST /api/config/update`：读取或热更新配置
- `GET /api/metrics`：查看指标
- `GET /health`：健康检查

详细请求与响应格式见 [Documents/docs/guide/api.md](Documents/docs/guide/api.md)。

## 测试

运行全部测试：

```bash
python -m pytest tests/ -v
```

仅运行单元测试：

```bash
python -m pytest tests/unit/ -v
```

仅运行集成测试：

```bash
python -m pytest tests/integration/ -v
```

### 验证脚本

上述测试均不触网。需要验证真实登录或上游行为时用这些脚本：

```bash
python scripts/verify_real_login.py --dry-run   # 登录链路 + Origin，不提交凭据
python scripts/verify_real_login.py             # 真机登录（只登录，不提交预约）
python scripts/diag_login_chain.py              # 登录链路逐跳诊断（含代理环境变量）
python scripts/test_captcha_reuse.py --help     # 上游验证码复用性 / 频控实测
```

不联网的抢票管线回归（8 条路径，不需要真实账号）：

```bash
python scripts/verify_rush_pipeline.py          # 全部 PASS 即管线正常
```

## 文档站

项目文档基于 VitePress，源码位于 `Documents/docs/`。

本地预览：

```bash
npm install
npm run docs:dev
```

构建静态文档：

```bash
npm run docs:build
```

建议先读：

- [Documents/docs/guide/quick-start.md](Documents/docs/guide/quick-start.md)
- [Documents/docs/guide/install.md](Documents/docs/guide/install.md)
- [Documents/docs/guide/config.md](Documents/docs/guide/config.md)
- [Documents/docs/guide/cas-auth.md](Documents/docs/guide/cas-auth.md)
- [Documents/docs/guide/booking.md](Documents/docs/guide/booking.md)
- [Documents/docs/guide/ocr-captcha.md](Documents/docs/guide/ocr-captcha.md)
- [Documents/docs/guide/api.md](Documents/docs/guide/api.md)
- [Documents/docs/guide/faq.md](Documents/docs/guide/faq.md)

## 注意事项

- 验证码识别使用 ddddocr 本地整图识别，无需额外模型文件或远程 OCR 服务。
- **密码安全**：登录成功后服务端自动保存凭据（XOR+base64 混淆，密钥自动生成），
  预约/任务接口不再随请求发送密码；前端 localStorage 只存用户名与 token。
  登录请求仍含一次明文密码（CAS 上游协议决定），公网部署请置于 HTTPS 反代之后。
- 定时预约任务会落到 SQLite，服务重启后会自动恢复待执行任务。
- 同一用户同一天只能有一个预约，系统会做本地去重和资源锁保护。
- 上游对预约接口按账号限流：约 2 连发内安全，第 3 发触发 3 分钟封禁，
  因此并发开火数硬上限为 2（实测方法见 `scripts/test_captcha_reuse.py`）。
- 滑块验证码为一次性凭证（复用会被拒绝），抢票流水线在预取窗口为每枪各解一份。
- 「取消」会尽力同步撤销学校侧预约；该账号从未在本系统登录过时仅能清除本地排队。
- **代理环境变量会打挂登录**：`requests` 默认信任 `http_proxy` / `all_proxy` 等变量，若其值是
  占位符（如 `...`），登录会报 `LocationParseError: Failed to parse: '...'`。报错里的 `'...'`
  是**主机名字面值**，不是学校侧故障。先用 `python scripts/diag_login_chain.py` 排查，或
  `unset http_proxy https_proxy all_proxy` 后重试。

## License

MIT
