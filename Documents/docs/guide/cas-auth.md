# CAS 认证

## 概述

SMU Badminton 集成了上海海事大学 CAS（Central Authentication Service）统一认证平台，实现用户身份验证。整个认证流程涉及 CAS 登录和 OAuth2 授权两个阶段。

## 认证流程

```
用户请求登录
    |
    v
访问 CAS 登录页面 --> 获取验证码图片
    |
    v
识别验证码（自动 OCR / 手动输入）
    |
    v
提交用户名 + 密码 + 验证码
    |
    v
CAS 验证成功 --> 重定向到 WF OAuth2 授权
    |
    v
OAuth2 授权成功 --> 回调返回 access_token + id_token
    |
    v
Token 缓存（默认 TTL 900 秒）
```

## 验证码处理

系统支持两种验证码处理方式：

### 自动识别（默认）

调用 OCR 模块自动识别验证码，无需人工干预：

1. 获取 CAS 登录页面，提取验证码图片和 `execution` 令牌
2. 将验证码图片送入 OCR 引擎识别
3. 自动提交登录请求

### 手动输入

当 OCR 识别失败时，可切换为手动模式：

1. 先调用 `/api/captcha` 获取验证码图片（base64 编码）
2. 前端展示验证码，用户手动输入
3. 调用 `/api/login` 时传入 `captcha_code` 参数

## Token 形态（2026-09-18 真机实测）

两个 token 的形态完全不同，写任何涉及 token 的代码前先看这张表：

| Token | 长度 | 形态 | 能否读出 `exp` |
|-------|------|------|----------------|
| `access_token` | 32 字符 | **opaque token**（无 `.` 分隔，不含 payload） | ❌ 读不出 |
| `id_token` | 1168 字符 | 标准 JWT | ✅ 可读，寿命 **7200s（2 小时）** |

由此产生三条硬约束：

1. **任何"从 `access_token` 解析 JWT claim"的写法都拿不到东西**。需要用户信息时走
   `id_token`——`profile_from_claims` 已按 `id_token` 优先实现。
2. **判断会话剩余寿命必须用 `token_profile.session_exp_epoch(tokens)`**：先读
   `access_token`，读不出回退 `id_token`。抢票的 T-0 预检（`TOKEN_EXP_BUFFER_SEC`）走的就是它。
   早期实现只读 `access_token`，`exp_epoch is None` 会让整个判断被**静默跳过**，
   那段注释写着"杜绝 T-0 触发重新登录"的逻辑实际上从未生效。
3. 换算：会话寿命 7200s，而 Token 缓存 TTL 默认 900s，因此 T-0 时手上的 token 至少还剩
   ~6300s，远大于 120s 的预检缓冲——正常路径下预检不会触发刷新。

## Token 缓存机制

登录成功后获取的 Token 会被缓存，避免重复登录。缓存有效期由 `TOKEN_CACHE_TTL_SEC` 与
`TOKEN_PROFILE_TTL_SEC` 控制，默认值见[配置参数](./config.md)。

- Token 缓存按用户名存储，线程安全
- 缓存命中时直接返回，无需重新登录
- 调用 `/api/logout` 可手动清除指定用户的 Token 缓存

## 登录实现

登录采用单一稳定路径 `cas_login_stable`，不存在 `CAS_LOGIN_STABLE_FIRST` 之类的策略开关（早期文档提及的双策略 / 备选回退已不存在）。流程如下：

1. 解析 CAS 登录页 URL，获取登录页面 HTML
2. 从页面提取 `execution` 令牌与事件顺序（`_stable_detect_event_order`）
3. 抓取验证码图片与一次性 token（验证码接口现已返回 JSON `{image, token, expiresAt}`）
4. OCR 识别或用户手输验证码后，提交用户名 + 密码 + 验证码 + execution
5. 跟随重定向完成 OAuth2 授权，从回调 URL fragment 提取 `access_token` + `id_token`

`login_with_retry` 在此基础上做 `max_retries × 3` 的整体重试。

> CAS 登录页已由 `cas.shmtu.edu.cn` 迁至 `sso.shmtu.edu.cn`，代码按登录页 host 推导同源验证码
> URL，无需手动维护两套地址，`.env` 中残留旧 `cas.` 地址也不会跨 host 抓取失败。

## 密码安全

密码在存储和传输过程中采用混淆处理：

- **XOR + Base64 混淆**：使用 `SECRET_KEY` 环境变量对密码进行 XOR 运算后 Base64 编码
- **非加密**：混淆仅防止明文暴露，不等于加密
- **安全警告**：使用默认 `SECRET_KEY` 时会发出警告，生产环境务必设置自定义密钥

## 登录错误处理

| 错误类型 | 说明 | 处理方式 |
|----------|------|----------|
| `captcha_error` | 验证码识别错误 | 前端提示手动输入验证码 |
| `password_error` | 用户名或密码错误 | 提示用户检查凭据 |
| `network_error` | 网络连接失败 | 提示检查网络连接 |
| `unknown_error` | 未知错误 | 查看服务端日志排查 |

## OAuth2 授权 URL 构建

系统自动构建 OAuth2 授权 URL，包含以下参数：

- `client_id`：OAuth 客户端标识（`OAUTH_CLIENT_ID`）
- `redirect_uri`：回调地址（WF 平台的 OIDC 回调）
- `response_type`：`id_token token`
- `scope`：`data openid process task app submit process_edit start profile`
- `state` / `nonce`：随机生成的安全参数

## 登录链路验证（真机）

`Origin` 头与验证码 URL 都按**解析出的登录页 host** 派生，学校侧是否接受只能靠真机验证。
项目提供两个脚本：

| 脚本 | 用途 |
|------|------|
| `scripts/verify_real_login.py` | 走**应用真实路径**（`get_token_cached` → `login_with_retry`）登录一次，打印实际 wire 出去的 `Origin` / `Referer`，以及两个 token 的形态与剩余寿命 |
| `scripts/diag_login_chain.py` | 链路第一跳就失败时的诊断工具：逐跳打印 host / status / `Location`，并打印 `requests.getproxies()`；失败后自动用 `trust_env=False` 复跑对比 |

```bash
# 1) 只解析重定向链、不提交凭据（推荐的第一次运行）
python scripts/verify_real_login.py --dry-run

# 2) 真机登录：学号自动取 AUTHORIZED_USERS 首个，只敲密码（不回显）
python scripts/verify_real_login.py

# 3) 第一跳就报错时诊断
python scripts/diag_login_chain.py
```

判定标准：`Origin` 应与解析出的登录页 host 同源（当前为 `https://sso.shmtu.edu.cn`）。
脚本**只登录、不提交任何预约**，因此不消耗上游的预约提交频控额度。

### 代理变量陷阱

`requests.Session` 默认 `trust_env=True`，会读取 `http_proxy` / `all_proxy` 等环境变量。
若某个代理变量的值是占位符（如 `...`），urllib3 在**连接阶段**会把它当成 host，报出：

```
LocationParseError: Failed to parse: '...', label empty or too long
```

这条消息由 `Failed to parse: '<host>', label empty or too long` 拼成，**引号里的 `...` 是
host 字面值**，不是被截断的长 URL——很容易被误判成"学校侧登录链路改版"。区分方法：

| 现象 | 含义 |
|------|------|
| `LocationParseError: ... '...', label empty or too long` | 连接阶段 host 异常 → **代理变量**问题 |
| `InvalidURL: URL has an invalid label.` | 请求 URL 本身畸形 → 入口 URL 配置问题 |

处理：先跑 `python scripts/diag_login_chain.py` 看 `getproxies()` 输出；确认是占位符后
`unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY`（仅当前终端），
或保留代理但让学校域名直连：`export no_proxy="shmtu.edu.cn,localhost,127.0.0.1"`。
