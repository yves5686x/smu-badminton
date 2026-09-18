# 常见问题

## 安装与部署

### Q: 启动时提示 "未找到 index.html"

A: 确保 `templates/` 目录存在且包含 `index.html` 文件。如果是从源码运行，请确认在项目根目录下启动服务。

### Q: 启动后第一次识别验证码比较慢

A: 验证码识别使用 ddddocr 本地整图推理，内置 onnx 模型在首次调用时才会惰性加载（随后常驻内存），
因此第一次登录会稍慢，属正常现象。无需预先下载或挂载任何模型文件，也不存在 `OCR_MODE` /
远程 OCR 之类的配置项（早期 NCNN 三模型管线已移除）。

### Q: 需要手动设置 SECRET_KEY 吗

A: 不需要。源码里不存在公开的默认密钥——未配置时首次启动会生成随机密钥并持久化到
`DATA_DIR/secret_key`，重启复用。仅在多实例共享同一数据库等场景才需要显式指定同一个值：

```env
SECRET_KEY=your-random-secret-key-here
```

换用新密钥后旧混淆数据按失效处理（`deobfuscate` 返回空），用户重新登录即可重建保存的凭据。

## 登录与认证

### Q: 验证码识别经常失败怎么办

A: 验证码识别准确率受图片质量与字体影响。可以尝试：

1. 系统已对识别失败做重试（`login_with_retry` 内部最多 `max_retries × 3` 次），偶发失败一般会自愈
2. 仍失败时前端切换为手动输入验证码模式（`/api/captcha` 取图，`/api/login` 传 `captcha_code`）
3. 一般不需要检查验证码地址：登录页 host 由代码沿重定向链解析，验证码 URL 与登录 POST 的
   `Origin` 头都按其同源派生，`.env` 里残留旧的 `cas.` 地址也会被自动纠正

### Q: Token 缓存多久过期

A: 由 `TOKEN_CACHE_TTL_SEC` 控制（默认值见[配置参数](./config.md)）。过期后系统会自动重新登录获取新 Token。
服务端 Token 缓存按用户名存储，调用 `/api/logout` 可手动清除。

> 实测补充（2026-09-18）：`access_token` 是 32 字符的 opaque token，**读不出 `exp`**；
> `id_token` 才是 1168 字符的 JWT（寿命 7200s）。会话剩余寿命统一用
> `session_exp_epoch(tokens)` 读取——先读 access_token，读不出回退 id_token。
> 详见 [CAS 认证](./cas-auth.md)。

### Q: 报 `LocationParseError: Failed to parse: '...', label empty or too long` 是怎么回事

A: 这是**终端里的代理环境变量**造成的，与学校侧无关。

这条消息由 `Failed to parse: '<host>', label empty or too long` 拼成，**引号里的 `...` 就是
host 字面值**——不是被截断的长 URL。请求 URL 的 host 明明是 `wf.shmtu.edu.cn`，连接阶段却
要连主机名 `...`，只可能是 `requests` 走了代理，而代理变量的值是个没填的占位符。
（`requests.Session` 默认 `trust_env=True`，会读取 `http_proxy` / `all_proxy` 等变量。）

```bash
# 确认
env | grep -i proxy

# 方案一：当前终端整个不用代理
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

# 方案二：代理要留着上外网，只让学校域名直连
export no_proxy="shmtu.edu.cn,localhost,127.0.0.1"
```

随后跑 `python scripts/diag_login_chain.py`——它会直接打印 `requests.getproxies()` 并逐跳给出
host，可确认问题已消除。注意 `unset` 只对当前终端会话有效。

对照：如果换成 `InvalidURL: URL has an invalid label.`，那才是**入口 URL 本身畸形**（配置问题），
两者不要混为一谈。

### Q: 怎么确认登录链路（Origin 头改动）没被改坏

A: 用 `scripts/verify_real_login.py`。它走应用真实路径登录一次，并钩住 `requests.Session.post`
打印**实际 wire 出去的 `Origin` / `Referer`**，同时剖析两个 token 的形态与剩余寿命：

```bash
python scripts/verify_real_login.py --dry-run   # 只解析链路与 Origin，不提交凭据
python scripts/verify_real_login.py             # 真机登录（只登录，不提交预约）
```

判定标准：`Origin` 应与解析出的登录页 host 同源（当前是 `https://sso.shmtu.edu.cn`）。
脚本只登录、不提交预约，不消耗上游的预约提交频控额度。

### Q: 登录失败 error_type 含义

| error_type | 原因 | 处理方式 |
|-----------|------|---------|
| `captcha_error` | 验证码识别错误 | 切换手动输入 |
| `password_error` | 用户名或密码错误 | 检查凭据 |
| `network_error` | 网络连接失败 | 检查网络 |
| `unknown_error` | 未知错误 | 查看服务端日志 |

## 预约功能

### Q: 提示 "每人每天只能预约一次"

A: 系统限制同一用户同一天只能有一个预约记录。这是数据库层面的 UNIQUE 约束，包括：
- `local_bookings` 表的 `(bookdate, resources_name, kssj, jssj)` 唯一约束
- 预约前检查当天是否已有预约任务或记录

### Q: 定时预约的目标时间怎么计算

A: 目标时间 = `预约日期 - 7 天 + 目标时间`。例如预约 12 月 18 日的场地，目标时间 21:00:00，则系统将在 12 月 11 日 21:00:00 发起预约。这对应学校系统提前 7 天开放预约的规则。

### Q: 定时预约线程数设置多少合适

A: 线程数范围为 1-5，默认 5。线程数越多并发请求越密集，但受限于服务器处理能力和网络条件。通常 3-5 即可满足抢场需求。

### Q: 服务重启后定时预约任务会丢失吗

A: 不会。所有定时任务都持久化到 SQLite 的 `scheduled_jobs` 表。服务启动时会自动调用 `load_pending_jobs()` 恢复状态为 `scheduled` 或 `running` 的任务。

### Q: 提示 "resource_locked_processing" 是什么意思

A: 表示该资源正在被其他预约请求处理中。这是系统的资源锁机制，防止同一资源被重复预约。等待几秒后重试即可。

## 可用性查询

### Q: 可用性查询响应头中的 X-Avail-Cache 含义

| 值 | 含义 |
|----|------|
| `HIT-PUBLIC` | 公共缓存命中，仅查询了用户自己的预约记录，响应更快 |
| `MISS` | 缓存未命中，执行了完整查询（资源 + 时间槽 + 预约记录） |

公共缓存 TTL 为 60 秒，所有用户共享场地时间槽数据。

### Q: 为什么可用性查询有时快有时慢

A: 首次查询（缓存 MISS）需要请求多个 GraphQL 接口获取完整数据，耗时较长。后续 60 秒内的查询（缓存 HIT-PUBLIC）仅需查询预约记录，速度显著提升。

## API 使用

### Q: 请求返回 429 状态码

A: 表示请求频率超过限流阈值。限流阈值由 `RATE_LIMIT_*` 环境变量控制，**各部署取值可能不同**
（默认值见[配置参数](./config.md)），所以这里不列具体数字。被限流时响应体里直接带着本次
生效的阈值，照着调即可：

```json
{"ok": false, "error": "请求过于频繁", "limit": 60, "window_sec": 60, "path": "/api/jobs"}
```

### Q: 如何配置可信代理

A: 如果服务部署在反向代理（如 Nginx）后面，需配置可信代理 IP 以正确获取客户端真实 IP：

```env
TRUSTED_PROXIES=127.0.0.1,10.0.0.1
```

未配置时，限流将使用直接连接的 IP，可能导致所有请求被视为同一 IP。

## Docker 部署

### Q: Docker 容器健康检查失败

A: 检查以下几点：
1. 确认服务已正常启动（查看容器日志 `docker logs smu-badminton`）
2. 确认端口映射正确（默认 5000）
3. 确认 `.env` 文件已正确挂载

### Q: Docker 部署后数据会丢失吗

A: 使用 docker-compose 部署时，数据存储在 Docker volume `smu-badminton-data` 中，不会因容器重建而丢失。但直接使用 `docker run` 且未挂载 volume 时，数据会在容器删除时丢失。

## 频控、验证码与取消（2026-08-27 实测补充）

### Q: 提示「频繁调用接口，禁用 3 分钟」是怎么回事

A: 上游对预约提交接口按账号限流：约 2 次连发内安全，第 3 次立即封禁 3 分钟（解禁时间精确
= 触发时刻 + 180 秒）。因此系统把并发枪数硬性限制为 **2**，且每枪单发不重试。如果手动高频
调用预约接口触发了封禁，等待 3 分钟即可恢复。

### Q: 滑块验证码能复用吗？有效期多久

A: 实测定案：**一次性消费**——同一凭证第二次提交会返回「验证码不能重复使用」。凭证本体
从签发起至少 3 分钟内有效，但第一次提交即被烧掉，所以每次预约都需要独立解一份。系统在定时
抢票的预取窗口（T-75s 唤醒、截止 T-35s）就为每一枪提前解好带重试的凭证；若预取阶段
还不知道该资源是否需要校验（上游 T-0 才放号时会这样），会**投机预热**一份池子备用。
复核脚本：`scripts/test_captcha_reuse.py`。

### Q: 到点了却提示预约失败，之后还一直说「当天已有预约记录」，怎么办

A: 这对应两个已修复的历史缺陷：

1. **预约结果判定读错了层级**。上游把业务字段放在 GraphQL 响应的 `data.<mutation>` 第二层，
   旧实现直接对顶层取 `code`，于是**真实抢到的预约也被判成失败**。
2. **失败后没有回滚本地预约占位记录**。`local_bookings` 的唯一约束是按**场次**的
   （`bookdate + 场地 + 时段`），留一条就同时挡住本人重试和所有其他同学选这个场次，
   于是会一直提示「您当天已有预约记录，每人每天只能预约一次」。

如果本地库里已经躺了失败任务的残留记录，可以先确认学校侧是否真有预约；确认没有的话，
删除 `data/data.db` 中对应的 `local_bookings` 行即可恢复可预约状态。

### Q: 点了「立即预订」，为什么结果过几秒才显示

A: 即时预订已改为异步任务：点击瞬间格子进入琥珀色旋转状态并返回 job_id，后台线程执行完整
预约链路；期间以 3 秒快轮询跟踪任务，到达终态后自动刷新网格并弹出成功/失败提示。这样既不
阻塞页面，也不会在等待期间误点其他格子。

### Q: 取消时提示「无可用登录凭据，仅取消了本地排队」是什么意思

A: 说明本地排队任务已经停止，但没能撤销学校侧的预约。撤销需要登录凭据：优先使用页面当前
会话的 access_token；没有则用服务端保存的账号静默重新登录。该账号从未通过网页成功登录过时
两者都不可用——先正常登录一次即可启用自动撤销。

### Q: 抢票时间可以改吗

A: 页面固定为 21:00（学校放号时间）。如遇学校调整放号时间，修改
`static/js/main.js` 顶部的 `RUSH_TIME` 常量一处即可。

### Q: 时钟不准会影响抢票吗

A: 系统在预取窗口会测量「网络时间 - 本地时间」偏移量（ClockSync，3 次采样取中位数），
关键等待全部使用本地钟 + 偏移换算，T-0 前不再发任何对时请求，唤醒精度不受网络抖动影响。
