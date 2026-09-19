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

# Offline regression for the whole rush pipeline (no network, no real account)
python scripts/verify_rush_pipeline.py               # all PASS = pipeline healthy

# Live measurement of upstream captcha/rate-limit behaviour (needs a real token)
python scripts/test_captcha_reuse.py --help

# Real-device login check: prints the actual Origin/Referer on the wire + both token shapes
python scripts/verify_real_login.py --dry-run        # chain + Origin only, no credentials
python scripts/verify_real_login.py                  # real login (logs in only, never books)

# Diagnose a login chain that fails on the first hop (prints per-hop host + getproxies())
python scripts/diag_login_chain.py
```

## Environment Setup

Config has a strict layer model — **one authoritative source per key**
(full spec: `Documents/docs/guide/config.md`, generated table checked by tests):

- **L1** process env (`docker-compose environment:` / `docker run -e`) — wins over L2
- **L2** project-root `.env` — deployer-maintained; **only keys that deviate from defaults**
- **L3** runtime-mutable config (DB `app_settings` table, `settings_store.py`) — UI-editable,
  currently just the login entry URL
- **L4** code defaults (`config.py` `_DEFAULT_SPEC`) — the single default source;
  `.env.example` mirrors it key-for-key

`cp .env.example .env`, then keep only the lines you actually want to change.
`AUTHORIZED_USERS` defaults to empty and **must be set explicitly** or the job-monitor
page and config-update endpoint stay closed to everyone.

Derived values are never configured separately: the CAS login-page host is resolved along
the redirect chain, and both the captcha URL and the login POST `Origin` header derive from
it (`CAS_ORIGIN` and `CAS_LOGIN_URL` were removed).

## Environment Traps

Two local-environment gotchas that look like code/upstream bugs but are neither:

- **Proxy env vars break the login chain.** `requests.Session` defaults to `trust_env=True`, so it
  reads `http_proxy` / `all_proxy` / etc. If such a variable holds a placeholder (`...`), urllib3
  uses it as the connect host and raises
  `LocationParseError: Failed to parse: '...', label empty or too long`. The `'...'` there is the
  **host literal**, not a truncated URL — do not misread it as upstream breakage. Diagnose with
  `python scripts/diag_login_chain.py` (dumps `getproxies()` and per-hop host).
  Contrast: a genuinely malformed entry URL raises `InvalidURL: URL has an invalid label.`
- **`ruff` is not in `.venv/bin`**, and `uv run ruff` fails here with an editable-build error
  (setuptools `project.license` deprecation + uv build-cache `EEXIST`) unrelated to code quality.
  Use `uvx ruff`, or check small edits with `python -m py_compile` plus actually running the script.

## Architecture

### Package Layout

`src/smu_badminton/` with src layout. Entry point: `smu_badminton.server_fastapi:main`.

### Module boundaries

See `Documents/docs/guide/architecture.md` for the current design and migration policy.

- `routes_*`: HTTP adapters; blocking calls use `run_in_threadpool`. No cross-route imports or booking SQL.
- `booking_service.py`: common reservation rules for all four booking endpoints; cancellation orchestration.
- `booking_store.py`: SQL, atomic status transitions and reservation ownership (`local_booking_id`).
- `cas_manager.py`: single-process thread executor, timed prefetch/submission; one shared immediate attempt.
- `credentials.py`: credential resolution and cached login; `token_profile.py` keeps token/account storage.
- `booking_api.py`: upstream GraphQL and captcha APIs. Query failures raise `UpstreamQueryError`, never imply empty bookings.
- `availability.py`: date-keyed public cache and single-flight queries; per-user appointments merged separately.
  Resources and capacity are identical across users. Caller cancellation does not cancel the shared query.
- `server_fastapi.py`: lifecycle and middleware wiring; cleanup delegates to the store.
- `locks.py` was removed: reservation transactions now enforce conflicts atomically.

Task cancellation is cooperative. A request already sent to the school may succeed; wait for it to return,
record success and retain the reservation. `stop_by_params` returns `upstream_status=pending` while a worker
is still active; callers should check completion and cancel the upstream booking afterward if needed.

### Database Schema

Four SQLite tables via `core_utils.DatabasePool`:
- `local_bookings`: Tracks active bookings (UNIQUE on bookdate, resources_name, kssj, jssj)
- `scheduled_jobs`: Persists booking jobs and reservation ownership across restarts
- `user_accounts`: Saved credentials
- `app_settings`: Runtime configuration

### Scheduled Booking Timing

Target time = `bookdate - 7 days + target_time_str`. E.g., booking for 2025-12-18 with target 21:00:00 means attempt at 2025-12-11 21:00:00.

### Upstream Constraints (measured 2026-08-27 via scripts/test_captcha_reuse.py)

| Constraint | Measured behavior | Design consequence |
|---|---|---|
| Slide captcha lifetime | Single-use: second save with same credentials returns 「验证码不能重复使用」; server still recognizes it ≥3 min later | Every shot needs its own captcha; solve pool during prefetch window |
| Captcha validation order | Server validates captcha BEFORE business rules; unverified captcha → 「系统异常」, valid captcha → business errors | Calibrated negative control possible; classifier keywords stable |
| Solver reliability | ~70% per attempt (fails with checkCaptcha 4001), retry succeeds | Retry redundancy belongs in prefetch window (cheap time), not at T-0 |
| Save rate limit | Per-account: ~2 rapid saves OK, 3rd immediately banned for exactly 3 minutes | `MAX_UPSTREAM_BURST = 2` hard cap on concurrent shots |
| Token shape (2026-09-18) | `access_token` is a 32-char **opaque** token (no JWT payload); `id_token` is a 1168-char JWT carrying `exp`, session lifetime **7200s** | Never parse JWT claims off `access_token`. Session expiry goes through `session_exp_epoch(tokens)` — reads `access_token` first, falls back to `id_token` |
| Official cancel API | `checkAppointmentCancelTime(id)` + `updateAppointmentInformationState(id, state="1")` works | Available for implementing real cancellation later |

### Rush Scheduling Pipeline (start_scheduled_booking)

T-75s wake → `ClockSync.sync()` measures network-vs-local offset → login (session expiry pre-checked against target time via `session_exp_epoch()`, refreshed early if needed — it reads `id_token`'s `exp` because `access_token` is opaque) → dedupe check + resource/time prefetch + user_info resolution all through one warmed `create_session()` → build captcha pool (`min(num_threads, 2)` credentials, retry until T-35s) → workers wait via local clock + offset (zero HTTP near T-0) → barrier → each shot fires single-shot (`allow_retry=False`, 4s timeout) with its own credential → first success stops the rest. Ban responses are detected and logged with the 3-minute unban implication.

Two details matter because slots are released at 21:00 for a date 7 days out:

- **Prefetch failure is normal, not fatal.** At T-75s the target slot does not exist yet, so
  `fetch_resource_time_id` returns nothing. The job must NOT be marked failed there — it falls
  through to a **T-0 fallback** where the firing threads resolve the resource/time ID once
  (3 retries, 0.15s apart) and submit immediately. Prefetch failure also leaves
  `open_captcha_verify` unknown, so the captcha pool is built **speculatively**; creds are only
  attached to the mutation when validation is confirmed to be on. Captcha credentials are
  token-scoped, not slot-scoped (`gen_slide_captcha(token)` takes no slot id), which is what makes
  speculation valid.
- **Failure releases only the owned reservation.** All Web booking entry points reserve through
  `BookingService`; `scheduled_jobs.local_booking_id` persists ownership. The store atomically updates
  terminal status and removes only that row. Legacy rows with unknown ownership stay NULL on migration.
  `rollback_local_on_fail` remains for old script callers; Web routes pass an explicit reservation ID.

### Booking result classification

The booking mutation's business fields live one level down, at
`data.<mutationName>.{code, messages}`. Always go through `unwrap_graphql_result()` /
`is_business_success()` / `business_messages()` in `booking_api.py` — reading `code` off the top
level silently reports every real success as a failure. `unwrap_graphql_result()` is idempotent
(returns an already-unwrapped body as-is), so it is safe to chain the helpers.

Offline regression for all of the above: `python scripts/verify_rush_pipeline.py` (8 paths,
no network, no real account, temp data dir).

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
