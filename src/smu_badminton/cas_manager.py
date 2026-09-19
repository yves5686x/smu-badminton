import logging
import threading
import time
import uuid
from typing import Any

from .booking_api import (
    UpstreamAuthenticationError,
    UpstreamQueryError,
    business_messages,
    check_resource_time_slot_capacity,
    create_session,
    fetch_resource_time_id,
    is_business_success,
    list_appointments_for_account,
    make_appointment,
    resolve_user_info,
    solve_and_verify_slide_captcha,
    unwrap_graphql_result,
)
from .booking_store import BookingStore, JobState, booking_store
from .core_utils import (
    deobfuscate_password,
    handle_errors,
    success_response,
)
from .credentials import get_token_cached
from .http_utils import (
    ClockSync,
    get_target_datetime_from_network,
)
from .token_profile import (
    session_exp_epoch,
)

# 配置日志
logger = logging.getLogger(__name__)


# ============= 抢票节奏常量（2026-08-27 对上游实测定案）=============
#
# 实测结论（scripts/test_captcha_reuse.py）：
# 1. 上游对 saveAppointmentInformationAll 按账号限流：约 2 连发内安全，
#    曾观察到第 3 发触发「频繁调用接口，禁用3分钟」；当前上限设为 2。
# 2. 滑块验证码一次性消费：同一凭证第二次提交返回「验证码不能重复使用」，
#    且第一枪即被烧掉 → 每枪必须自带独立凭证，在预取窗口内解好几份备着；
# 3. 验证码求解单次成功率约 70%，失败为 checkCaptcha 4001，重试即可，
#    重试冗余放在预取窗口（时间充裕），不放 T-0；
# 4. 服务端先验验证码后走业务，无效凭证返回「系统异常」。

# 并发提交枪数上限（受上游账号级频控约束）
MAX_UPSTREAM_BURST = 2

# 距目标时刻多少秒唤醒，进入预取窗口（登录/预取/验证码池构建）
PREFETCH_WINDOW_SEC = 75

# 验证码池构建最晚截止点（距目标时刻秒数）；此后不再尝试解新码
CAPTCHA_HARD_STOP_BEFORE_SEC = 35

# 抢票请求超时秒数（快速失败；预约接口正常 <1s 返回）
RUSH_REQUEST_TIMEOUT_SEC = 4

# token exp 距目标时刻不足该秒数时提前重新登录
TOKEN_EXP_BUFFER_SEC = 120

# T-0 兜底解析「资源/时段 ID」的重试次数与间隔。
# 上游放号晚于预取窗口（常见：预约日期 21:00 才放出）时，预取必然拿不到 ID，
# 此时不能在预取阶段就把任务判死，改由主线程在 T-0 统一解析后提交。
T0_RESOLVE_RETRIES = 3
T0_RESOLVE_GAP_SEC = 0.15
T0_RESOLVE_BUDGET_SEC = 4.0


def _classify_upstream_response(resp) -> str:
    """对 saveAppointmentInformationAll 的响应分类（展开 GraphQL 包体）。

    Returns:
        success / banned / captcha_error / other
    """
    inner = unwrap_graphql_result(resp)
    if not inner:
        return "other"
    code = str(inner.get("code", "")).lower()
    if code in ("0", "success"):
        return "success"
    msgs = business_messages(inner)
    if any(k in msgs for k in ("频繁", "禁用", "禁止", "解禁")):
        return "banned"
    if "验证码" in msgs or "captcha" in msgs.lower():
        return "captcha_error"
    return "other"


# ============= 等待辅助（定时任务共用） =============


def _wait_until(target_time, wake_delta_sec, cancel_event=None, now_fn=None) -> bool:
    """按本地/校准时钟等待；Event.wait 使长等待也能立即响应取消。"""
    from datetime import datetime

    event = cancel_event if cancel_event is not None else threading.Event()
    now_fn = now_fn or (lambda: datetime.now(target_time.tzinfo))
    while not event.is_set():
        remaining = (target_time - now_fn()).total_seconds() - wake_delta_sec
        if remaining <= 0:
            return True
        interval = min(remaining, 60.0 if wake_delta_sec else (0.5 if remaining > 1 else 0.05))
        event.wait(interval)
    return False


def _get_slide_captcha(access_token: str) -> tuple[str, str]:
    """获取滑块验证码（如果需要）。

    Returns:
        (captcha_id, captcha_code) 元组
    """
    try:
        result = solve_and_verify_slide_captcha(access_token)
        if result:
            return result
    except Exception as e:
        logger.error("滑块验证码处理异常: %s", e)
    return "", ""


def _token_covers_rush(tokens, start_epoch):
    expiry = session_exp_epoch(tokens)
    return expiry is None or expiry > start_epoch + T0_RESOLVE_BUDGET_SEC + RUSH_REQUEST_TIMEOUT_SEC


def _prepare_rush_tokens(login_url, captcha_url, username, password, target_time, cancel_event, *, force_refresh=False):
    """准备阶段允许重登；已知令牌有效期必须覆盖目标时刻及提交窗口。"""
    tokens = get_token_cached(login_url, captcha_url, username, password, force_refresh=force_refresh)
    if not tokens or not tokens.get("access_token"):
        if cancel_event.wait(1):
            return None
        tokens = get_token_cached(login_url, captcha_url, username, password, force_refresh=force_refresh)
    if not tokens or not tokens.get("access_token"):
        return None
    start_epoch = max(target_time.timestamp(), time.time())
    expiry = session_exp_epoch(tokens)
    if (expiry is None and not force_refresh) or (expiry is not None and expiry < start_epoch + TOKEN_EXP_BUFFER_SEC):
        refreshed = get_token_cached(login_url, captcha_url, username, password, force_refresh=True)
        if refreshed and refreshed.get("access_token"):
            tokens = refreshed
    return tokens if _token_covers_rush(tokens, start_epoch) else None


def _prepare_rush_captchas(token, target, shots, target_time, cancel_event, now_fn, *, renewed=False):
    """通常提前准备；若临近提交重新登录，限次重建绑定新令牌的验证码。"""
    if target and target[2] != "1":
        return []
    deadline = target_time.timestamp() - CAPTCHA_HARD_STOP_BEFORE_SEC
    # 临近开抢才创建的任务、或重新登录后的补准备，最多尝试每枪两次。
    limited = renewed or now_fn().timestamp() >= deadline
    pool = []
    attempts = 0
    while len(pool) < shots and not cancel_event.is_set():
        if limited:
            if attempts >= shots * 2:
                break
        elif now_fn().timestamp() >= deadline:
            break
        attempts += 1
        creds = _get_slide_captcha(token)
        if creds and all(creds) and creds[0] not in {item[0] for item in pool}:
            pool.append(creds)
        elif cancel_event.wait(0.2):
            break
    return pool


def _resolve_rush_target(token, id_token, session, slot, cancel_event):
    """到点仅做有限的资源查询；不嵌套请求重试，也不在此重新登录。"""
    deadline = time.monotonic() + T0_RESOLVE_BUDGET_SEC
    for attempt in range(T0_RESOLVE_RETRIES):
        if cancel_event.is_set() or time.monotonic() >= deadline:
            return None
        try:
            target = fetch_resource_time_id(token, **slot, id_token=id_token, session=session, deadline=deadline)
        except UpstreamAuthenticationError:
            raise
        except UpstreamQueryError:
            target = None
        if time.monotonic() >= deadline:
            return None
        if target:
            return target
        if attempt < T0_RESOLVE_RETRIES - 1 and cancel_event.wait(T0_RESOLVE_GAP_SEC):
            return None
    return None


def _submit_rush(token, id_token, session, slot, target, user_info, captcha_pool, shots, cancel_event):
    """每份凭证最多提交一次；成功结果优先于同时到达的取消信号。"""
    from concurrent.futures import ThreadPoolExecutor

    shots = max(1, min(shots, MAX_UPSTREAM_BURST))
    resource_id, time_id, captcha_flag = target
    # 即使上游或调用方重复返回同一凭证，也只消费一次。
    unique = {creds[0]: creds for creds in captcha_pool if creds and all(creds)}
    credentials = list(unique.values())[:shots] if captcha_flag == "1" else [("", "")] * shots
    if not credentials:
        logger.warning("需要验证码但没有可用凭证，放弃提交")
        return False
    success = threading.Event()

    def submit(creds):
        if cancel_event.is_set() or success.is_set():
            return False
        try:
            response = make_appointment(
                token,
                time_id,
                resource_id,
                slot["bookdate"],
                slot["kssj"],
                slot["jssj"],
                id_token=id_token,
                captcha_id=creds[0],
                captcha_code=creds[1],
                user_info=user_info,
                session=session,
                allow_retry=False,
                timeout_seconds=RUSH_REQUEST_TIMEOUT_SEC,
            )
            ok = is_business_success(response)
            logger.info(
                "抢票提交结果: ok=%s kind=%s messages=%s", ok, _classify_upstream_response(response), business_messages(response)
            )
            if ok:
                success.set()
            return ok
        except Exception:
            logger.exception("抢票提交异常")
            return False

    with ThreadPoolExecutor(max_workers=len(credentials)) as executor:
        # 消费所有结果后才能关闭共用连接、释放占位。
        results = list(executor.map(submit, credentials))
    return any(results)


class BookingJob:
    def __init__(self, thread: threading.Thread, cancel_event: threading.Event, meta: dict[str, Any]):
        self.thread = thread
        self.cancel_event = cancel_event
        self.meta = meta  # {type: 'immediate'|'scheduled', created_at, params}


class BookingManager:
    """单进程任务执行器：管理线程、等待和提交，持久化委托 BookingStore。"""

    def __init__(self, store: BookingStore | None = None) -> None:
        self.store = store if store is not None else booking_store
        self._jobs: dict[str, BookingJob] = {}
        self._lock = threading.Lock()

        logger.info("预约管理器初始化完成")

    def get_job_owner(self, job_id):
        row = self.store.detail(job_id)
        return row["username"] if row else None

    def get_job_detail(self, job_id):
        return self.store.detail(job_id)

    def list_scheduled_jobs(self, username=None):
        return self.store.list_jobs(username)

    def day_booking_conflict(self, username, bookdate):
        return self.store.day_conflict(username, bookdate)

    def add_local_booking(self, username, bookdate, resources_name, kssj, jssj):
        from .core_utils import BookingError

        try:
            self.store.reserve(username=username, bookdate=bookdate, resources_name=resources_name, kssj=kssj, jssj=jssj)
        except BookingError as exc:
            return exc.code
        return None

    def _finish_job(self, job_id, status):
        # SQL 状态与占位在一个事务中收尾；内存条目由线程入口 finally 回收。
        return self.store.transition(job_id, status)

    def _launch(self, job_id, thread, *, persist=None):
        try:
            if persist is not None:
                self.store.create_job(job_id, **persist)
            thread.start()
        except Exception:
            try:
                self._finish_job(job_id, JobState.FAILED)
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)
            raise

    @handle_errors(default_return=[], log_error=True, error_message="获取任务列表失败")
    def list_jobs(self) -> list[dict[str, Any]]:
        """获取所有活跃任务列表"""
        with self._lock:
            out: list[dict[str, Any]] = []
            for job_id, job in self._jobs.items():
                # 只展示正在执行且未请求取消的任务，回收由线程入口完成
                if not job.thread.is_alive() or job.cancel_event.is_set():
                    continue
                out.append(
                    {
                        "job_id": job_id,
                        "alive": job.thread.is_alive(),
                        "type": job.meta.get("type"),
                        "created_at": job.meta.get("created_at"),
                        "username": job.meta.get("username") or job.meta.get("params", {}).get("username"),
                        "params": job.meta.get("params", {}),
                    }
                )
            return out

    def stop_job(self, job_id: str) -> bool:
        """活跃线程只发取消信号，由执行结束时确认终态；已发出的请求允许先返回结果。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job:
            job.cancel_event.set()
            if job.thread.ident is not None and job.thread is not threading.current_thread():
                job.thread.join(timeout=0.5)
            return True
        return self._finish_job(job_id, JobState.CANCELLED)

    def is_active(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.thread.is_alive())

    def load_pending_jobs(self) -> None:
        """恢复定时任务；单个坏任务不阻断其余任务恢复。"""
        for row in self.store.pending_jobs():
            job_id = row.pop("job_id")
            row.pop("status")
            with self._lock:
                if job_id in self._jobs:
                    continue
            if not row["target_time_str"]:
                self._finish_job(job_id, JobState.FAILED)
                continue
            row["password"] = deobfuscate_password(row["password"]) if row["password"] else ""
            try:
                self.start_scheduled_booking(**row, resume_job_id=job_id, rollback_local_on_fail=False)
            except Exception:
                logger.exception("恢复任务失败: %s", job_id)
                self._finish_job(job_id, JobState.FAILED)

    def _execute_immediate(self, job_id, params, cancel_event):
        try:
            result = _attempt_booking(**params, cancel_event=cancel_event)
        except Exception:
            logger.exception("即时预约任务异常: %s", job_id)
            result = {"ok": False, "error": "booking_failed"}
        state = (
            JobState.DONE
            if result.get("ok")
            else JobState.CANCELLED
            if result.get("error") == "cancelled"
            else JobState.SKIPPED
            if result.get("error") == "user_already_booked_today"
            else JobState.FAILED
        )
        self._finish_job(job_id, state)
        return result

    def start_immediate_booking(
        self,
        *,
        login_url,
        captcha_url,
        username,
        password,
        bookdate,
        kssj,
        jssj,
        resources_name,
        rollback_local_on_fail=False,
        local_booking_id=None,
        wait=False,
    ):
        params = dict(
            login_url=login_url,
            captcha_url=captcha_url,
            username=username,
            password=password,
            bookdate=bookdate,
            kssj=kssj,
            jssj=jssj,
            resources_name=resources_name,
        )
        if local_booking_id is None and rollback_local_on_fail:
            local_booking_id = self.store.local_id(**params)
        job_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        persist = dict(**params, target_time_str="", num_threads=1, status="running", local_booking_id=local_booking_id)
        if wait:
            with self._lock:
                self._jobs[job_id] = BookingJob(
                    threading.current_thread(),
                    cancel_event,
                    {
                        "type": "immediate",
                        "created_at": time.time(),
                        "username": username,
                        "params": {k: v for k, v in params.items() if k not in ("password", "login_url", "captcha_url")},
                    },
                )
            try:
                self.store.create_job(job_id, **persist)
                return self._execute_immediate(job_id, params, cancel_event)
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)

        def run():
            try:
                self._execute_immediate(job_id, params, cancel_event)
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)

        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            self._jobs[job_id] = BookingJob(
                thread,
                cancel_event,
                {
                    "type": "immediate",
                    "created_at": time.time(),
                    "username": username,
                    "params": {k: v for k, v in params.items() if k not in ("password", "login_url", "captcha_url")},
                },
            )
        self._launch(job_id, thread, persist=persist)
        return job_id

    def start_scheduled_booking(
        self,
        *,
        login_url: str,
        captcha_url: str,
        username: str,
        password: str,
        bookdate: str,
        kssj: str,
        jssj: str,
        resources_name: str,
        target_time_str: str,
        num_threads: int = 2,
        resume_job_id: str | None = None,
        rollback_local_on_fail: bool = True,
        local_booking_id: int | None = None,
    ) -> str:
        """创建定时抢票任务。

        Args:
            rollback_local_on_fail: 任务未成功时是否回滚本地预约占位记录。
                仅兼容旧脚本。Web 入口统一由 BookingService 创建占位，
                显式传入 local_booking_id，并随任务持久化。
        """
        # 校验并限制线程数（业务层 clamp）
        num_threads = max(1, min(MAX_UPSTREAM_BURST, num_threads))

        if resume_job_id is None:
            for job in self.store.matching_jobs(
                username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name
            ):
                if job["status"] in ("scheduled", "running"):
                    return job["job_id"]

        if local_booking_id is None and rollback_local_on_fail and resume_job_id is None:
            local_booking_id = self.store.local_id(
                username=username, bookdate=bookdate, resources_name=resources_name, kssj=kssj, jssj=jssj
            )
        cancel_event = threading.Event()

        session = None

        def _run_impl(force_refresh=False):
            nonlocal session
            # ========== 计算目标时刻；长等待到预取窗口 ==========
            target_time = get_target_datetime_from_network(target_time_str, bookdate)
            clock = ClockSync()
            logger.info(
                "[任务 %s] 目标时刻=%s（预约日 %s，T-%ds 唤醒）",
                job_id[:8],
                target_time.isoformat(),
                bookdate,
                PREFETCH_WINDOW_SEC,
            )

            if not _wait_until(target_time, PREFETCH_WINDOW_SEC, cancel_event):
                self._finish_job(job_id, JobState.CANCELLED)
                return

            # 进入关键窗口：测「网络-本地」时钟偏移，此后等待不再发任何 HTTP
            clock.sync(samples=3)

            tokens = _prepare_rush_tokens(
                login_url, captcha_url, username, password, target_time, cancel_event, force_refresh=force_refresh
            )
            if not tokens:
                self._finish_job(job_id, JobState.CANCELLED if cancel_event.is_set() else JobState.FAILED)
                return
            access_token, id_token = tokens["access_token"], tokens.get("id_token", "")
            session = create_session()
            if list_appointments_for_account(access_token, bookdate, id_token=id_token, session=session):
                self._finish_job(job_id, JobState.SKIPPED)
                return

            # 用户信息提前解析，提交阶段不得再隐式查询或重新登录。
            user_info = resolve_user_info(access_token, id_token=id_token, session=session)
            if not user_info:
                self._finish_job(job_id, JobState.FAILED)
                return
            slot = dict(bookdate=bookdate, resources_name=resources_name, kssj=kssj, jssj=jssj)
            try:
                target = fetch_resource_time_id(access_token, **slot, id_token=id_token, session=session)
            except UpstreamAuthenticationError:
                raise
            except UpstreamQueryError:
                logger.warning("[任务 %s] 预取查询失败，保留到点解析机会", job_id[:8])
                target = None
            shots = max(1, min(num_threads, MAX_UPSTREAM_BURST))
            captcha_pool = _prepare_rush_captchas(access_token, target, shots, target_time, cancel_event, clock.now)

            # 只有主线程等时间、解析目标；提交线程只负责一次提交。
            if not _wait_until(target_time, 0, cancel_event, now_fn=clock.now):
                self._finish_job(job_id, JobState.CANCELLED)
                return
            self.store.transition(job_id, JobState.RUNNING)
            if not _token_covers_rush(tokens, clock.now().timestamp()):
                logger.info("[任务 %s] 令牌有效期不足，重新登录并重建验证码", job_id[:8])
                tokens = _prepare_rush_tokens(
                    login_url, captcha_url, username, password, target_time, cancel_event, force_refresh=True
                )
                if not tokens:
                    self._finish_job(job_id, JobState.CANCELLED if cancel_event.is_set() else JobState.FAILED)
                    return
                access_token, id_token = tokens["access_token"], tokens.get("id_token", "")
                user_info = resolve_user_info(access_token, id_token=id_token, session=session)
                if not user_info:
                    self._finish_job(job_id, JobState.FAILED)
                    return
                captcha_pool = _prepare_rush_captchas(
                    access_token, target, shots, target_time, cancel_event, clock.now, renewed=True
                )
            if not target:
                target = _resolve_rush_target(access_token, id_token, session, slot, cancel_event)
            if target and not cancel_event.is_set():
                succeeded = _submit_rush(
                    access_token, id_token, session, slot, target, user_info, captcha_pool, shots, cancel_event
                )
            else:
                succeeded = False
            state = JobState.DONE if succeeded else JobState.CANCELLED if cancel_event.is_set() else JobState.FAILED
            self._finish_job(job_id, state)

        def run():
            nonlocal session
            try:
                for attempt in range(2):
                    try:
                        _run_impl(force_refresh=attempt > 0)
                        break
                    except UpstreamAuthenticationError:
                        if attempt:
                            raise
                        logger.info("预约准备时认证失效，重新登录并重做准备")
                        if session is not None:
                            session.close()
                            session = None
                        if cancel_event.is_set():
                            self._finish_job(job_id, JobState.CANCELLED)
                            return
            except Exception:
                logger.exception("抢票任务异常终止: %s", job_id)
                self._finish_job(job_id, JobState.FAILED)
            finally:
                try:
                    if session is not None:
                        session.close()
                finally:
                    with self._lock:
                        self._jobs.pop(job_id, None)

        th = threading.Thread(target=run, daemon=True)
        meta = {
            "type": "scheduled",
            "created_at": time.time(),
            "params": {
                "username": username,
                "bookdate": bookdate,
                "kssj": kssj,
                "jssj": jssj,
                "resources_name": resources_name,
                "target_time_str": target_time_str,
                "num_threads": num_threads,
            },
            "username": username,
        }
        job_id = resume_job_id or uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = BookingJob(th, cancel_event, meta)
        persist = (
            None
            if resume_job_id
            else dict(
                login_url=login_url,
                captcha_url=captcha_url,
                username=username,
                password=password,
                bookdate=bookdate,
                kssj=kssj,
                jssj=jssj,
                resources_name=resources_name,
                target_time_str=target_time_str,
                num_threads=num_threads,
                status="scheduled",
                local_booking_id=local_booking_id,
            )
        )
        self._launch(job_id, th, persist=persist)
        return job_id


# 单例管理器（可在其他模块导入使用）
booking_manager = BookingManager()


def _attempt_booking(**params):
    """认证失效时仅重做提交前准备；提交请求不会因这里的重登而重发。"""
    for attempt in range(2):
        try:
            return _attempt_booking_once(**params, force_refresh=attempt > 0)
        except UpstreamAuthenticationError:
            logger.info("即时预约准备时认证失效，重新登录并重做准备")
    return {"ok": False, "error": "login_failed"}


def _attempt_booking_once(
    *, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, cancel_event=None, force_refresh=False
):
    """即时预约的唯一执行路径；不写任务或占位记录。"""
    if cancel_event is not None and cancel_event.is_set():
        return {"ok": False, "error": "cancelled"}
    tokens = (
        get_token_cached(login_url, captcha_url, username, password, force_refresh=True)
        if force_refresh
        else get_token_cached(login_url, captcha_url, username, password)
    )
    if not tokens or not tokens.get("access_token"):
        return {"ok": False, "error": "login_failed"}

    access_token = tokens["access_token"]
    id_token = tokens.get("id_token", "")

    # 整个预约链路复用同一连接池 Session，省掉每步的 TCP/TLS 握手
    session = create_session()
    try:
        # 限制：同一用户同一天只能预约一次
        my_edges = list_appointments_for_account(access_token, bookdate, id_token=id_token, session=session)
        if my_edges:
            return {"ok": False, "error": "user_already_booked_today"}

        result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=id_token, session=session)
        if not result:
            logger.warning(
                "fetch_resource_time_id 返回 None: bookdate=%s, resources_name=%s, kssj=%s, jssj=%s",
                bookdate,
                resources_name,
                kssj,
                jssj,
            )

            return {"ok": False, "error": "resource_or_time_not_found"}

        resource_id, time_id, open_captcha_verify = result
        logger.info(
            "获取到资源: resource_id=%s, time_id=%s, open_captcha_verify=%s",
            resource_id[:20] if resource_id else "",
            time_id[:20] if time_id else "",
            open_captcha_verify,
        )

        # 即时预约前置校验：时段容量 + 滑块验证码
        captcha_id = ""
        captcha_code = ""
        if open_captcha_verify == "1":
            logger.info("资源需要滑块验证码，自动处理")
            for attempt in range(1, 4):
                if cancel_event is not None and cancel_event.is_set():
                    return {"ok": False, "error": "cancelled"}
                logger.info("即时预约验证码尝试 %d/3", attempt)
                captcha_id, captcha_code = _get_slide_captcha(access_token)
                if captcha_id and captcha_code:
                    break
            else:
                logger.error("滑块验证码连续 3 次处理失败，未提交预约")
                return {"ok": False, "error": "captcha_verify_failed"}

        capacity_result = check_resource_time_slot_capacity(
            access_token, resource_id, [time_id], bookdate, kssj, jssj, id_token=id_token, session=session
        )
        if capacity_result and capacity_result.get("code") != "0":
            logger.warning("时段容量检查失败: %s", capacity_result)

            return {"ok": False, "error": "capacity_check_failed", "detail": capacity_result}

        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "error": "cancelled"}
        resp_json = make_appointment(
            access_token,
            time_id,
            resource_id,
            bookdate,
            kssj,
            jssj,
            id_token=id_token,
            captcha_id=captcha_id,
            captcha_code=captcha_code,
            session=session,
            allow_retry=False,
            timeout_seconds=8,
        )
    finally:
        session.close()

    # 判断预约是否成功（业务字段在 GraphQL 响应的 data.<mutation> 层，见 unwrap_graphql_result）
    logger.info("make_appointment 返回: %s", resp_json)
    ok = is_business_success(resp_json)
    if not ok:
        logger.warning(
            "预约失败: kind=%s messages=%s",
            _classify_upstream_response(resp_json),
            business_messages(resp_json),
        )

    if ok:
        return success_response(resp_json)
    return {"ok": False, "error": "booking_rejected", "data": resp_json}


def book_badminton_slot(**params):
    """兼容原同步调用入口；与后台即时预约使用相同执行路径。"""
    return booking_manager.start_immediate_booking(**params, wait=True)
