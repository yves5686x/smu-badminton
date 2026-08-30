# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SMU Badminton Court Booking System - Web app for booking badminton courts at Shanghai Maritime University. CAS authentication with OCR captcha solving, real-time availability checking, immediate/scheduled booking with multi-threaded concurrent requests. FastAPI backend, SQLite storage.

## Development Commands

```bash
# Install (editable)
pip install -e .

# Run dev server (port 5002, auto-reload)
python -m smu_badminton.server_fastapi

# Run with uvicorn directly
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5000 --reload

# Debug mode (verbose booking logs)
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi

# Production
docker-compose up --build

# Tests
python -m pytest tests/ -v                          # all
python -m pytest tests/unit/ -v                     # unit only
python -m pytest tests/integration/ -v              # integration only
python -m pytest tests/unit/test_obfuscate.py -v    # single file
python -m pytest tests/unit/test_obfuscate.py::test_roundtrip -v  # single test
```

## Environment Setup

Copy `.env.example` to `.env` and configure:
- `CAS_ORIGIN`, `WF_ORIGIN`, `WF_API_URL` - University platform URLs
- `OAUTH_CLIENT_ID` - OAuth client identifier
- `BADMINTON_TYPE_ID` - Resource type ID for badminton courts
- `SERVER_PORT` - (optional) Override dev server port, defaults to 5002

## Architecture

### Package Layout

`src/smu_badminton/` with src layout. Entry point: `smu_badminton.server_fastapi:main`.

### Core Modules

| Module | Purpose |
|--------|---------|
| `server_fastapi.py` | FastAPI app, lifespan, middleware wiring, static files, `main()` console-script 入口 |
| `schemas.py` | Pydantic 请求/响应模型（错误约定：`{ok:false, error, message}`） |
| `middleware.py` | 纯 ASGI Metrics/RateLimit 中间件 + `snapshot_metrics()` |
| `locks.py` | 资源锁（`get_resource_lock`）与可用性公共缓存（`avail_cache_get/put`） |
| `routes_auth.py` | 认证路由：验证码 / 登录 / 登出 / 静默续期 |
| `routes_booking.py` | 预约路由：即时 / 定时 / 可用性查询 / 本地记录；`booking_precheck` 共享前置校验 |
| `routes_jobs.py` | 任务路由：任务列表 / 停止 / 单任务状态 / metrics |
| `routes_config.py` | 配置路由：读取 / 热更新 |
| `cas_login.py` | CAS auth flow: URL resolution, captcha prep, login with auto/manual captcha, error detection |
| `http_utils.py` | HTTP retry helpers, network time sync, `ClockSync` offset calibration |
| `booking_api.py` | Resource queries, time slot queries, appointment creation/cancellation, availability computation (parallel via ThreadPoolExecutor + shared Session), thread-local session reuse (`get_thread_session`) |
| `cas_manager.py` | `BookingManager` singleton: job create/track/stop, DB persistence, scheduled/immediate booking orchestration, `get_token_cached`（凭据回退咽喉点） |
| `cas_ocr.py` | Arithmetic captcha OCR via ddddocr whole-image recognition (replaced deprecated NCNN ResNet pipeline after site font change) |
| `slide_captcha.py` | 滑块验证码缺口识别（alpha 边缘模板匹配 + 多方法兜底投票） |
| `token_profile.py` | Token/profile caches (JWT claim parsing), saved user accounts, silent re-login |
| `core_utils.py` | Thread-safe SQLite `DatabasePool`, custom exceptions (`BookingError`, `DatabaseError`), error handling decorators (`handle_errors`, `db_operation`), password obfuscation |
| `config.py` | Environment configuration from `.env`; SECRET_KEY 自动生成并持久化 |

### Module Dependencies

```
server_fastapi → routes_{auth,booking,jobs,config}, middleware, locks, cas_manager, core_utils, config
routes_*       → schemas, locks, cas_manager, booking_api, token_profile, core_utils
cas_manager    → cas_login, booking_api, http_utils, token_profile, core_utils
booking_api    → http_utils (retry helpers), token_profile, slide_captcha, config
cas_login      → cas_ocr, http_utils(间接), config
```

### Data Flow

1. **Login**: `prepare_login_session()` / `login_with_auto_captcha()` → CAS login page → captcha OCR → POST credentials → follow redirects → extract OIDC tokens (access_token + id_token) from URL fragment
2. **Availability**: `POST /api/availability` → check 60s public cache (shared across users, keyed by bookdate) → cache HIT: only query appointments for `bookedByMe`; cache MISS: full query (resources + time slots + appointments) via shared `requests.Session` with connection pooling → store slots in public cache, merge `bookedByMe` per-user
3. **Immediate Booking**: `POST /api/book` → lock resource → insert `local_bookings` → `book_badminton_slot()` → single-threaded attempt
4. **Scheduled Booking**: `POST /api/book/schedule` → `BookingManager.start_scheduled_booking()` → wait until target time → login → prefetch → spawn N barrier-synchronized worker threads → fire booking requests simultaneously

### Key Design Patterns

- **Token Caching**: `get_token_cached()` per-user dict with configurable TTL (default 900s), thread-safe
- **Profile Caching**: `_TOKEN_PROFILE_CACHE` stores user profile from JWT claims
- **Server-side Credential Hosting**: 登录成功后服务端自动保存账号（`user_accounts` 表，XOR+base64 混淆，v2: 前缀）。预约/任务接口的 `password` 字段可省略，`get_token_cached()` 在缓存未命中时自动回退到保存的凭据重新登录；前端 localStorage 只存 `{username}` + token，密码不落地不重发。无凭据时路由返回 `no_saved_credentials`
- **Public Availability Cache**: `locks.avail_cache_get/put` keyed by bookdate (60s TTL), shared across all users — slots data is the same for everyone; only `bookedByMe` is queried per-user on cache HIT
- **Shared HTTP Session**: `_shared_session()` creates `requests.Session` with connection pool (20 conns) for reuse across parallel GraphQL queries within a single availability request
- **GraphQL Request Helper**: `_make_graphql_request()` centralizes retry logic (2 attempts, 0.3s backoff), SSL error detection, and slow-query logging (>500ms)
- **Resource Locking**: asyncio locks per `(resources_name, bookdate, kssj, jssj)` prevent duplicate concurrent bookings; separate thread locks for sync code
- **Local Booking Tracking**: SQLite `local_bookings` UNIQUE constraint `(bookdate, resources_name, kssj, jssj)` prevents race conditions at DB level
- **Job Persistence**: `scheduled_jobs` table survives server restarts; `load_pending_jobs()` restores on startup
- **Password Obfuscation**: XOR + base64 with `SECRET_KEY`（v2: 版本前缀；未配置时自动生成随机密钥持久化到 `DATA_DIR/secret_key`，换钥后旧数据按失效处理）
- **Thread-safe SQLite**: `DatabasePool` uses `threading.local()` for per-thread connections, WAL mode

### Database Schema

Two SQLite tables via `core_utils.DatabasePool`:
- `local_bookings`: Tracks active bookings (UNIQUE on bookdate, resources_name, kssj, jssj)
- `scheduled_jobs`: Persists booking jobs across restarts

### Scheduled Booking Timing

Target time = `bookdate - 7 days + target_time_str`. E.g., booking for 2025-12-18 with target 21:00:00 means attempt at 2025-12-11 21:00:00.

### Upstream Constraints (measured 2026-08-27 via scripts/test_captcha_reuse.py)

| Constraint | Measured behavior | Design consequence |
|---|---|---|
| Slide captcha lifetime | Single-use: second save with same credentials returns 「验证码不能重复使用」; server still recognizes it ≥3 min later | Every shot needs its own captcha; solve pool during prefetch window |
| Captcha validation order | Server validates captcha BEFORE business rules; unverified captcha → 「系统异常」, valid captcha → business errors | Calibrated negative control possible; classifier keywords stable |
| Solver reliability | ~70% per attempt (fails with checkCaptcha 4001), retry succeeds | Retry redundancy belongs in prefetch window (cheap time), not at T-0 |
| Save rate limit | Per-account: ~2 rapid saves OK, 3rd immediately banned for exactly 3 minutes | `MAX_UPSTREAM_BURST = 2` hard cap on concurrent shots |
| Official cancel API | `checkAppointmentCancelTime(id)` + `updateAppointmentInformationState(id, state="1")` works | Available for implementing real cancellation later |

### Rush Scheduling Pipeline (start_scheduled_booking)

T-75s wake → `ClockSync.sync()` measures network-vs-local offset → login (JWT exp pre-checked against target time, refreshed early if needed) → dedupe check + resource/time prefetch + user_info resolution all through one warmed `_shared_session()` → build captcha pool (`min(num_threads, 2)` credentials, retry until T-35s) → workers wait via local clock + offset (zero HTTP near T-0) → barrier → each shot fires single-shot (`allow_retry=False`, 4s timeout) with its own credential → first success stops the rest. Ban responses are detected and logged with the 3-minute unban implication.

### OCR Models (deprecated)

`model/` directory (gitignored) previously held the NCNN ResNet triplet used by the old captcha
pipeline (`resnet34_digit_*`, `resnet18_operator_*`, `resnet18_equal_symbol_*`). After the CAS site
changed its captcha font these self-trained models could no longer recognize the operators/digits and
could not be retrained (no training infra). Captcha recognition now uses **ddddocr** whole-image
classification + regex parse (see `cas_ocr.py`); the NCNN models + `ncnn` dependency have been removed.
The `model/` files may still exist on disk but are no longer loaded.

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/availability` | POST | Check court availability (token + bookdate) |
| `/api/book` | POST | Immediate booking attempt |
| `/api/book/schedule` | POST | Schedule booking for specific time |
| `/api/jobs` | GET | List all booking jobs（支持 `?username=` 过滤，前端轮询用） |
| `/api/jobs/{job_id}/stop` | POST | Cancel a scheduled job |
| `/api/jobs/stop_by_params` | POST | Cancel job by booking params（同时尽力撤销学校侧预约：调用方传 `access_token` 或凭服务端保存账号静默重登） |
| `/api/local_bookings` | GET | List local booking records |
| `/api/login` | POST | CAS login with captcha |
| `/api/captcha` | POST | Get captcha image |
| `/api/logout` | POST | Clear token cache |
| `/api/auth/refresh` | POST | Silent re-login with saved account, returns fresh access_token |
| `/api/auth/check` | GET | Check auth status |
| `/api/config` | GET | Frontend configuration |
| `/api/metrics` | GET | Request metrics |
| `/health` | GET | Health check |

### Port Convention

Dev server defaults to port 5002 (`__main__` block, overridable via `SERVER_PORT` env var). Docker/production uses port 5000 (`SERVER_PORT=5000` in docker-compose).

## Code Maintenance

### Recently Removed Dead Code (2026-05-28)

The following unused code was removed after verification:

| File | Removed Items |
|------|---------------|
| `core_utils.py` | `error_response()`, `error_response_from_exception()`, `retry_on_error()`, `PermissionDeniedError`, `ResourceAlreadyBookedError`, `JobNotFoundError` |
| `config.py` | `LOCK_MAX_AGE_SEC` (not wired to any code) |
| `cas_ocr.py` | `draw_split_lines_on_image()` (debug helper) |
| `booking_api.py` | `check_resource_availability_on_date()`, `find_resources_id_by_name()`, `demo_check_availability()` (old query functions) |
| `cas_login_requests.py` | `book_task_with_network_date()`, `run_concurrent_booking_threads()`, `test_user_info()`, `num_threads`, `barrier`, `__main__` block (legacy booking logic replaced by cas_manager) |
| `server_models.py` | `_availability_cache`, `_availability_locks`, `_availability_guard`, `_availability_ttl_sec`, `_availability_cleanup()`, `_get_avail_lock()`, `_convert_to_minimal()` (legacy per-user cache system) |

2026-08-30 追加清理：`APIResult`、`config.get_config()`、`LoginError`/`ResourceLockedError`、
`_save_job_record`（并入 `_persist_job_row`）、locks 线程锁注册表（无调用方）；
`server_models.py` 整体拆分为 schemas/middleware/locks 后删除。
