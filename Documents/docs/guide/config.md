# 配置参数

本页是**配置的唯一权威说明**。其它文档（README、安装、CAS 认证等）只讲"怎么改"，
不再抄写默认值——抄一份就多一处会漂移的副本。

## 分层与优先级

配置分四层，**优先级 L1 > L2 > L4**；L3 是独立的运行时通道：

| 层 | 载体 | 谁维护 | 生效时机 |
|----|------|--------|----------|
| **L1 进程环境变量** | `docker-compose environment:`、`docker run -e`、shell 变量 | 部署者 | 启动时 |
| **L2 项目根 `.env`** | 项目根目录的 `.env` | 部署者 | 启动时 |
| **L3 运行时可变配置** | 数据库 `app_settings` 表 | 授权用户在界面上改 | **立即生效** |
| **L4 代码默认值** | `src/smu_badminton/config.py` 的 `_DEFAULT_SPEC` | 开发者 | 随代码 |

规则：

1. **一个配置项只归属一个覆盖通道。** 除下面的 L3 项之外，所有键只认 L1/L2；
   登录入口 URL 只认 L3（不读 `.env`）。
2. **默认值只在 L4 定义一处**，`.env.example` 是它的镜像，由
   `tests/unit/test_config_layering.py` 逐键比对锁定。
3. **`.env` 只放确实要偏离默认值的项。** 与默认值相同的键写进 `.env` 就是第二份副本，
   代码改了它不会跟着变——那正是这份收敛要消除的问题。
4. **派生值不单独配置**：`WF_HOME_URL` / `WF_CAPTCHA_URL` 由 `WF_ORIGIN` 派生；
   CAS 验证码 URL 与登录 POST 的 `Origin` 头由实际解析出的登录页 host 派生。
5. **进程环境优先于 `.env`**（`load_dotenv(..., override=False)`）。

### 哪些项适合放 L1

只有"每台机器不同"的部署参数：`SERVER_PORT`、`DATA_DIR`、`TZ`、`TRUSTED_PROXIES`。
其余项放 L2 `.env` 即可。

## 环境变量

<!-- BEGIN:GENERATED:ENV_TABLE (由 scripts/gen_config_docs.py 生成，请勿手改) -->

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `WF_ORIGIN` | `https://wf.shmtu.edu.cn` | 微服务平台地址 |
| `WF_API_URL` | `https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys` | GraphQL 接口地址 |
| `WF_HOME_URL` | `https://wf.shmtu.edu.cn/yy-sys/pc/home` | 微服务平台首页（派生自 `WF_ORIGIN`），默认登录入口 |
| `WF_SSO_AUTHORIZE_PATH` | `/sso/oauth2/authorize` | OAuth2 授权路径（拼装授权 URL 用） |
| `WF_CAPTCHA_URL` | `https://wf.shmtu.edu.cn/yy-sys/captcha` | 滑块验证码接口（派生自 `WF_ORIGIN`） |
| `CAS_CAPTCHA_URL` | `https://sso.shmtu.edu.cn/cas/captcha` | CAS 验证码接口兜底地址（登录页 host 解析失败时才用） |
| `OAUTH_CLIENT_ID` | `kwxKbMKq3Nafw2mApFZz` | OAuth2 客户端标识 |
| `BADMINTON_TYPE_ID` | `93c2a115-5c73-4e30-bb6a-dfcc5404e46f` | 羽毛球场地资源类型 ID |
| `SERVER_PORT` | `5002` | 服务监听端口（Docker 内由 compose 覆盖为 `5000`） |
| `BOOKING_DEBUG` | `0` | 预约调试日志开关，`1` 开启 |
| `UVICORN_RELOAD` | `0` | uvicorn 热重载开关，`1` 开启 |
| `TOKEN_CACHE_TTL_SEC` | `900` | Token 缓存有效期（秒） |
| `TOKEN_PROFILE_TTL_SEC` | `3600` | 用户 Profile 缓存有效期（秒） |
| `JOB_RETENTION_SEC` | `3600` | 已完成任务保留时长（秒），超期由后台清理任务删除 |
| `DATA_DIR` | `<项目根>/data` | SQLite 数据目录（Docker 内由 compose 设为 `/app/data`） |
| `AUTHORIZED_USERS` | （空） | 授权用户列表，逗号分隔；**默认为空，必须显式配置** |
| `TRUSTED_PROXIES` | （空） | 可信代理 IP 列表，逗号分隔（配置后才信任 `X-Forwarded-For`） |
| `RATE_LIMIT_MAX` | `30` | 受保护接口（`/api/book`、`/api/book/schedule`、`/api/availability`）限流上限 |
| `RATE_LIMIT_WINDOW` | `10` | 上述接口限流窗口（秒） |
| `RATE_LIMIT_JOBS_MAX` | `300` | 任务接口（`/api/jobs` 前缀）限流上限 |
| `RATE_LIMIT_JOBS_WINDOW` | `60` | 任务接口限流窗口（秒） |
| `DEFAULT_DEPT_CODE` | （空） | JWT 缺少部门代码时的兜底值 |
| `DEFAULT_DEPT_NAME` | （空） | JWT 缺少部门名称时的兜底值 |
| `DEFAULT_DEPT_NAME_EN` | （空） | JWT 缺少英文部门名时的兜底值 |
| `DEFAULT_USER_EMAIL` | （空） | JWT 缺少邮箱时的兜底值 |
| `DEFAULT_USER_PHONE` | （空） | JWT 缺少电话时的兜底值 |
| `SECRET_KEY` | 自动生成 | 密码混淆密钥。未配置时首次启动生成随机密钥并持久化到 `DATA_DIR/secret_key`，重启复用 |

<!-- END:GENERATED:ENV_TABLE -->

> 修改默认值后运行 `python scripts/gen_config_docs.py` 重新生成本表。
> 测试会校验本表是否落后于代码。

## L3：运行时可变配置

目前只有一项——**登录入口 URL**（`app_settings.login_entry_url`）：

- 默认值：`WF_HOME_URL`（即 WF 首页），登录时由 WF → SSO → CAS 走完整重定向链。
- 通过 `POST /api/config/update` 修改，保存即生效，**无需重启**。
- 传空串可清除覆盖、恢复默认值。
- 只有 `AUTHORIZED_USERS` 里的用户可以修改。

历史实现把这一项写进 `.env` 并 `importlib.reload` 配置模块，存在两个问题：Docker 下
`.env` 是 `:ro` 挂载，写入必然以 `Permission denied` 失败；且 `reload` 只原地更新 config
模块字典，用 `from .config import X` 取过值的模块仍持有旧值。改为数据库存储后两个问题都不存在。

## 不适合用环境变量调整的常量

以下属于**算法行为**，不是部署配置，刻意留在代码里（`cas_manager.py`），改它们等于改抢票策略：

| 常量 | 值 | 含义 |
|------|----|------|
| `MAX_UPSTREAM_BURST` | 2 | 并发提交枪数上限（受上游账号级频控约束） |
| `PREFETCH_WINDOW_SEC` | 75 | 距目标时刻多少秒进入预取窗口 |
| `CAPTCHA_HARD_STOP_BEFORE_SEC` | 35 | 验证码池构建的硬截止点 |
| `RUSH_REQUEST_TIMEOUT_SEC` | 4 | 抢票请求超时（单发快断） |
| `TOKEN_EXP_BUFFER_SEC` | 120 | token exp 距目标不足该值时提前刷新 |

抢票时序与这些常量的实测依据见[预约流程](./booking.md)。

## 验证码识别

验证码 OCR 由 [ddddocr](https://github.com/sml2h3/ddddocr) 在本地整图识别完成，无需额外模型文件，
也不存在 `OCR_MODE` / `OCR_HTTP_*` / `OCR_TCP_*` 等远程 OCR 配置（这些选项已随 NCNN 管线一并移除）。
首次调用时 ddddocr 会惰性加载内置 onnx 模型，无需任何运行参数。若要改用手动输入验证码，
前端直接传 `captcha_code` 即可，与配置无关。

## 安全说明

`SECRET_KEY` 用于密码混淆（不是加密，作用是避免密码明文落库）。换用新密钥后旧混淆数据
按失效处理（`deobfuscate` 返回空），用户重新登录即可重建保存的凭据。

`AUTHORIZED_USERS` 默认为**空集合**：不配置时任务监控页与运行时配置更新对所有用户关闭。
启动日志会给出对应告警。
