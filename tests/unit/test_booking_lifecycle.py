"""用临时数据库和假上游验证预约不变量，不访问学校或真实账号。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from smu_badminton import booking_api as api
from smu_badminton import booking_service as service_module
from smu_badminton import cas_manager as cm
from smu_badminton import core_utils
from smu_badminton.booking_service import BookingService
from smu_badminton.booking_store import BookingStore, JobState
from smu_badminton.core_utils import BookingError, DatabasePool

PARAMS = dict(
    login_url="",
    captcha_url="",
    username="test_owner",
    password="p",
    bookdate="2026-12-18",
    kssj="10:00",
    jssj="11:00",
    resources_name="court",
)
SLOT = {key: PARAMS[key] for key in ("username", "bookdate", "kssj", "jssj", "resources_name")}


@pytest.fixture
def system(tmp_path, monkeypatch):
    pool = DatabasePool(str(tmp_path / "booking.db"))
    monkeypatch.setattr(core_utils, "get_db_pool", lambda: pool)
    core_utils.init_db_tables()
    store = BookingStore(pool)
    manager = cm.BookingManager(store)
    service = BookingService(manager)
    monkeypatch.setattr("requests.sessions.Session.request", lambda *a, **k: pytest.fail("Unexpected network request"))
    threads = []
    launch = manager._launch

    def tracked_launch(job_id, thread, **kwargs):
        threads.append(thread)
        return launch(job_id, thread, **kwargs)

    monkeypatch.setattr(manager, "_launch", tracked_launch)
    yield SimpleNamespace(store=store, manager=manager, service=service, threads=threads)
    for thread in threads:
        if thread.ident is not None:
            thread.join(timeout=2)
            assert not thread.is_alive()
    pool.close_all()


def join(system):
    for thread in system.threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


def create_job(store, job_id="job", status="running", **extra):
    store.create_job(job_id, **PARAMS, status=status, target_time_str="", num_threads=1, **extra)


def test_final_transition_is_atomic(system):
    create_job(system.store)
    barrier = threading.Barrier(2)

    def finish(state):
        barrier.wait()
        return system.store.transition("job", state)

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(finish, [JobState.DONE, JobState.CANCELLED]))
    assert sum(results) == 1
    assert not system.store.transition("job", JobState.FAILED)


def test_same_day_reservation_is_atomic_across_courts(system):
    barrier = threading.Barrier(2)

    def reserve(court):
        barrier.wait()
        try:
            return system.store.reserve(**{**SLOT, "resources_name": court})
        except BookingError:
            return None

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(reserve, ["court1", "court2"]))
    assert sum(r is not None for r in results) == 1


def test_old_job_cannot_delete_replacement_reservation(system):
    old_id = system.store.reserve(**SLOT)
    create_job(system.store, local_booking_id=old_id)
    system.store.release(old_id)
    new_id = system.store.reserve(**SLOT, check_day=False)
    assert system.store.transition("job", JobState.FAILED)
    assert system.store.local_id(**SLOT) == new_id


def test_immediate_exception_finishes_and_releases(system, monkeypatch):
    def fail(**params):
        raise ValueError("unexpected upstream shape")

    monkeypatch.setattr(cm, "_attempt_booking", fail)
    response = system.service.submit(**PARAMS)
    join(system)
    assert system.store.status(response["data"]["job_id"]) == "failed"
    assert system.store.local_id(**SLOT) is None
    assert system.manager.list_jobs() == []


@pytest.mark.parametrize("wait", [False, True])
def test_sync_and_background_use_same_attempt(system, monkeypatch, wait):
    calls = []

    def attempt(**params):
        calls.append(params)
        return {"ok": True, "data": {"appointmentId": "a"}}

    monkeypatch.setattr(cm, "_attempt_booking", attempt)
    response = system.service.submit(**PARAMS, wait=wait)
    join(system)
    assert response["ok"]
    assert len(calls) == 1
    assert system.store.list_jobs()[0]["status"] == "done"
    assert system.store.local_id(**SLOT) is not None


def test_persist_failure_starts_no_thread_and_releases(system, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(system.store, "create_job", fail)
    response = system.service.submit(**PARAMS)
    assert not response["ok"]
    assert all(t.ident is None for t in system.threads)
    assert system.manager.list_jobs() == []
    assert system.store.local_id(**SLOT) is None


def test_scheduled_early_failure_does_not_delete_unowned_reservation(system, monkeypatch):
    local_id = system.store.reserve(**SLOT)
    monkeypatch.setattr(cm, "_wait_until", lambda *a, **k: True)
    monkeypatch.setattr(cm.ClockSync, "sync", lambda *a, **k: None)
    monkeypatch.setattr(cm, "get_token_cached", lambda *a, **k: None)
    jid = system.manager.start_scheduled_booking(**PARAMS, target_time_str="21:00:00", rollback_local_on_fail=False)
    join(system)
    assert system.store.status(jid) == "failed"
    assert system.store.local_id(**SLOT) == local_id


def test_recovery_preserves_reservation_id(system, monkeypatch):
    local_id = system.store.reserve(**SLOT)
    system.store.create_job(
        "restore", **PARAMS, status="scheduled", target_time_str="21:00:00", num_threads=2, local_booking_id=local_id
    )
    recovered = []
    monkeypatch.setattr(system.manager, "start_scheduled_booking", lambda **kw: recovered.append(kw))
    system.manager.load_pending_jobs()
    assert recovered[0]["resume_job_id"] == "restore"
    assert recovered[0]["local_booking_id"] == local_id
    assert recovered[0]["password"] == "p"


def test_cancel_inflight_success_keeps_success_state(system, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def attempt(**params):
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    monkeypatch.setattr(cm, "_attempt_booking", attempt)
    response = system.service.submit(**PARAMS)
    jid = response["data"]["job_id"]
    try:
        assert entered.wait(1)
        assert system.manager.stop_job(jid)
        assert system.store.status(jid) == "running"
    finally:
        release.set()
    join(system)
    assert system.store.status(jid) == "done"
    assert system.store.local_id(**SLOT) is not None


def test_long_wait_is_interruptible():
    entered, cancel = threading.Event(), threading.Event()
    now = datetime.now(timezone(timedelta(hours=8)))
    outcomes = []

    def now_fn():
        entered.set()
        return now

    thread = threading.Thread(target=lambda: outcomes.append(cm._wait_until(now + timedelta(days=5), 75, cancel, now_fn)))
    thread.start()
    assert entered.wait(1)
    cancel.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert outcomes == [False]


def test_cancel_query_failure_keeps_confirmed_local_booking(system, monkeypatch):
    local_id = system.store.reserve(**SLOT)
    create_job(system.store, status="done", local_booking_id=local_id)

    def fail(*a, **k):
        raise api.UpstreamQueryError("unavailable")

    monkeypatch.setattr(service_module, "find_my_appointment_id", fail)
    response = system.service.cancel(**SLOT, access_token="test-token")
    assert response["upstream_status"] == "failed"
    assert system.store.local_id(**SLOT) == local_id


def test_cancel_success_releases_only_after_upstream_success(system, monkeypatch):
    local_id = system.store.reserve(**SLOT)
    create_job(system.store, status="done", local_booking_id=local_id)
    monkeypatch.setattr(service_module, "find_my_appointment_id", lambda *a, **k: "upstream-id")
    monkeypatch.setattr(service_module, "check_appointment_cancel_time", lambda *a, **k: (True, ""))

    def cancel(*a, **k):
        assert system.store.local_id(**SLOT) == local_id
        return True, ""

    monkeypatch.setattr(service_module, "update_appointment_state", cancel)
    response = system.service.cancel(**SLOT, access_token="test-token")
    assert response["upstream_status"] == "cancelled"
    assert system.store.local_id(**SLOT) is None


def test_cancel_query_requests_and_uses_appointment_id(monkeypatch):
    def request(session, url, headers, payload, *args, **kwargs):
        assert "node {\n        id\n" in payload["query"]
        node = dict(
            id="appointment",
            resources_name="court",
            start_time="10:00",
            end_time="11:00",
            state=0,
            appointment_date=api._bookdate_to_ms(PARAMS["bookdate"]),
        )
        return SimpleNamespace(
            status_code=200, json=lambda: {"data": {"findAppointmentInformationAllForAccount": {"edges": [{"node": node}]}}}
        )

    monkeypatch.setattr(api, "_make_graphql_request", request)
    assert api.find_my_appointment_id("token", PARAMS["bookdate"], "10:00", "11:00", "court") == "appointment"


@pytest.mark.parametrize("body", [None, {"errors": [{"message": "expired"}]}, {"data": None}])
def test_query_failure_is_not_empty_booking_list(monkeypatch, body):
    response = None if body is None else SimpleNamespace(status_code=200, json=lambda: body)
    monkeypatch.setattr(api, "_make_graphql_request", lambda *a, **k: response)
    with pytest.raises(api.UpstreamQueryError):
        api.list_appointments_for_account("token", PARAMS["bookdate"])


def test_upgrade_old_database_is_idempotent_and_preserves_rows(system):
    with system.store.pool.get_connection() as conn:
        conn.execute("ALTER TABLE scheduled_jobs DROP COLUMN local_booking_id")
        conn.execute("INSERT INTO scheduled_jobs (job_id,username,status,created_at) VALUES ('legacy','owner','scheduled',1)")
    core_utils.init_db_tables()
    core_utils.init_db_tables()
    row = system.store.detail("legacy")
    assert row["username"] == "owner"
    assert row["status"] == "scheduled"
    assert row["local_booking_id"] is None


def test_stopping_registered_but_not_started_job_sets_signal(system):
    create_job(system.store, status="scheduled")
    event = threading.Event()
    thread = threading.Thread(target=lambda: None)
    system.manager._jobs["job"] = cm.BookingJob(thread, event, {})
    assert system.manager.stop_job("job")
    assert event.is_set()
    assert system.store.status("job") == "scheduled"
    system.manager._jobs.clear()


def test_synchronous_booking_can_be_stopped_without_overwriting_success(system, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def attempt(**params):
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    monkeypatch.setattr(cm, "_attempt_booking", attempt)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(system.service.submit, **PARAMS, wait=True)
        try:
            assert entered.wait(1)
            jid = system.store.list_jobs()[0]["job_id"]
            assert system.manager.stop_job(jid)
            assert system.store.status(jid) == "running"
        finally:
            release.set()
        assert future.result()["ok"]
    assert system.store.status(jid) == "done"
    assert not system.manager.is_active(jid)


def test_scheduled_expiry_relogs_and_rebuilds_captcha_before_submit(system, monkeypatch):
    now = datetime.now(UTC)
    target = now + timedelta(seconds=60)
    clock_now = [now]
    tokens = {"access_token": "old", "expiry": now.timestamp() + 300}
    fresh = {"access_token": "new", "expiry": now.timestamp() + 1000}
    login_calls, submitted = [], []

    def login(*args, **kwargs):
        login_calls.append(kwargs.get("force_refresh", False))
        return fresh if kwargs.get("force_refresh") else tokens

    def wait(target_time, delta, *args, **kwargs):
        if delta == 0:
            clock_now[0] = now + timedelta(seconds=400)
        return True

    monkeypatch.setattr(cm, "get_target_datetime_from_network", lambda *a: target)
    monkeypatch.setattr(cm, "ClockSync", lambda: SimpleNamespace(sync=lambda **k: None, now=lambda: clock_now[0]))
    monkeypatch.setattr(cm, "_wait_until", wait)
    monkeypatch.setattr(cm, "get_token_cached", login)
    monkeypatch.setattr(cm, "session_exp_epoch", lambda t: t["expiry"])
    monkeypatch.setattr(cm, "create_session", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cm, "list_appointments_for_account", lambda *a, **k: [])
    monkeypatch.setattr(cm, "resolve_user_info", lambda *a, **k: {"user": "u"})
    monkeypatch.setattr(cm, "fetch_resource_time_id", lambda *a, **k: ("r", "time", "1"))
    captcha_ids = []

    def solve(token):
        captcha_id = f"{token}-{len(captcha_ids)}"
        captcha_ids.append(captcha_id)
        return captcha_id, "code"

    monkeypatch.setattr(cm, "solve_and_verify_slide_captcha", solve)

    def submit(token, *a, **k):
        submitted.append((token, k["captcha_id"]))
        return {"code": "0"}

    monkeypatch.setattr(cm, "make_appointment", submit)
    job = system.manager.start_scheduled_booking(**PARAMS, target_time_str="21:00:00")
    join(system)
    assert True in login_calls
    assert submitted and all(token == "new" and captcha_id.startswith("new-") for token, captcha_id in submitted)
    assert system.store.status(job) == "done"


@pytest.mark.parametrize("path", ["/api/jobs/immediate", "/api/book/schedule"])
@pytest.mark.parametrize("success", [True, False])
def test_http_to_worker_to_store_and_cancel(system, client, monkeypatch, path, success):
    """保留真实路由、服务、执行器和数据库，只替换学校侧调用。"""
    from smu_badminton import routes_booking, routes_jobs

    monkeypatch.setattr(routes_booking, "booking_service", system.service)
    monkeypatch.setattr(routes_jobs, "booking_service", system.service)
    monkeypatch.setattr(routes_jobs, "booking_manager", system.manager)
    monkeypatch.setattr(cm, "get_token_cached", lambda *a, **k: {"access_token": "test"})
    monkeypatch.setattr(cm, "session_exp_epoch", lambda *a: None)
    monkeypatch.setattr(cm, "ClockSync", lambda: SimpleNamespace(sync=lambda **k: None, now=lambda: datetime.now(UTC)))
    monkeypatch.setattr(cm, "_wait_until", lambda *a, **k: True)
    monkeypatch.setattr(cm, "create_session", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cm, "list_appointments_for_account", lambda *a, **k: [])
    monkeypatch.setattr(cm, "resolve_user_info", lambda *a, **k: {"user": "test"})
    monkeypatch.setattr(cm, "fetch_resource_time_id", lambda *a, **k: ("resource", "time", "0"))
    monkeypatch.setattr(cm, "check_resource_time_slot_capacity", lambda *a, **k: {"code": "0"})
    submissions = []

    def submit(*args, **kwargs):
        submissions.append(kwargs)
        return {"data": {"saveAppointmentInformationAll": {"code": "0" if success else "rejected"}}}

    monkeypatch.setattr(cm, "make_appointment", submit)
    response = client.post(path, json={**PARAMS, "target_time_str": "21:00:00", "num_threads": 5}).json()
    assert response["ok"]
    job_id = response["data"]["job_id"]
    join(system)
    detail = client.get(f"/api/schedule/{job_id}").json()["data"]
    assert detail["status"] == ("done" if success else "failed")
    local = client.get("/api/local_bookings", params={"bookdate": SLOT["bookdate"]}).json()["data"]["list"]
    assert bool(local) is success
    assert 1 <= len(submissions) <= 2
    assert all(x["allow_retry"] is False for x in submissions)
    if success:
        monkeypatch.setattr(service_module, "find_user_by_access_token", lambda *a: (PARAMS["username"], ""))
        monkeypatch.setattr(service_module, "find_my_appointment_id", lambda *a, **k: "appointment")
        monkeypatch.setattr(service_module, "check_appointment_cancel_time", lambda *a, **k: (True, ""))
        monkeypatch.setattr(service_module, "update_appointment_state", lambda *a, **k: (True, ""))
        cancelled = client.post(
            "/api/jobs/stop_by_params", json={**SLOT, "current_username": PARAMS["username"], "access_token": "test"}
        ).json()
        assert cancelled["data"]["upstream_status"] == "cancelled"
        assert system.store.local_id(**SLOT) is None


@pytest.mark.parametrize("succeeds_on", [1, 2, 3, None])
def test_immediate_captcha_retries_without_extra_booking_submissions(monkeypatch, succeeds_on):
    attempts, submitted = [], []
    monkeypatch.setattr(cm, "get_token_cached", lambda *a: {"access_token": "token"})
    monkeypatch.setattr(cm, "create_session", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cm, "list_appointments_for_account", lambda *a, **k: [])
    monkeypatch.setattr(cm, "fetch_resource_time_id", lambda *a, **k: ("r", "time", "1"))
    monkeypatch.setattr(cm, "check_resource_time_slot_capacity", lambda *a, **k: {"code": "0"})

    def solve(token):
        attempts.append(token)
        if len(attempts) == succeeds_on:
            return "fresh-id", "code"
        if len(attempts) == 1:
            raise RuntimeError("captcha request failed")
        return None

    monkeypatch.setattr(cm, "solve_and_verify_slide_captcha", solve)
    monkeypatch.setattr(cm, "make_appointment", lambda *a, **k: submitted.append(k) or {"code": "0"})
    result = cm._attempt_booking(**PARAMS)
    assert len(attempts) == (succeeds_on or 3)
    assert result["ok"] == bool(succeeds_on)
    assert len(submitted) == (1 if succeeds_on else 0)
    if submitted:
        assert submitted[0]["captcha_id"] == "fresh-id"
        assert submitted[0]["allow_retry"] is False
    else:
        assert result["error"] == "captcha_verify_failed"


def test_cancel_between_captcha_attempts_prevents_retry(monkeypatch):
    event = threading.Event()
    attempts = []
    monkeypatch.setattr(cm, "get_token_cached", lambda *a: {"access_token": "token"})
    monkeypatch.setattr(cm, "create_session", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cm, "list_appointments_for_account", lambda *a, **k: [])
    monkeypatch.setattr(cm, "fetch_resource_time_id", lambda *a, **k: ("r", "time", "1"))

    def solve(token):
        attempts.append(token)
        event.set()
        return None

    monkeypatch.setattr(cm, "solve_and_verify_slide_captcha", solve)
    monkeypatch.setattr(cm, "make_appointment", lambda *a, **k: pytest.fail("Unexpected booking"))
    assert cm._attempt_booking(**PARAMS, cancel_event=event)["error"] == "cancelled"
    assert len(attempts) == 1


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("expiry_stage", ["appointments", "resources", "profile"])
def test_auth_refresh_rebuilds_entire_booking_preparation(system, monkeypatch, scheduled, expiry_stage):
    current = ["old"]
    seen, submissions, logins = [], [], []

    def login(*a, **k):
        logins.append(k.get("force_refresh", False))
        if k.get("force_refresh"):
            current[0] = "new"
        return {"access_token": current[0], "id_token": current[0] + "-id"}

    monkeypatch.setattr(cm, "get_token_cached", login)
    monkeypatch.setattr(cm, "session_exp_epoch", lambda t: datetime.now(UTC).timestamp() + 86400 * 365)
    monkeypatch.setattr(cm, "get_target_datetime_from_network", lambda *a: datetime.now(UTC))
    monkeypatch.setattr(cm, "ClockSync", lambda: SimpleNamespace(sync=lambda **k: None, now=lambda: datetime.now(UTC)))
    monkeypatch.setattr(cm, "_wait_until", lambda *a, **k: True)
    monkeypatch.setattr(cm, "create_session", lambda: SimpleNamespace(close=lambda: None))

    def query(stage, token, result, **kwargs):
        seen.append((stage, token, kwargs["id_token"]))
        if stage == expiry_stage and token == "old":
            raise api.UpstreamAuthenticationError("expired")
        return result

    monkeypatch.setattr(cm, "list_appointments_for_account", lambda token, *a, **k: query("appointments", token, [], **k))
    monkeypatch.setattr(cm, "fetch_resource_time_id", lambda token, *a, **k: query("resources", token, ("r", "t", "1"), **k))
    monkeypatch.setattr(cm, "resolve_user_info", lambda token, **k: query("profile", token, {"user": token}, **k))
    monkeypatch.setattr(
        cm, "check_resource_time_slot_capacity", lambda token, *a, **k: query("capacity", token, {"code": "0"}, **k)
    )
    captcha_calls = []

    def captcha(token):
        captcha_calls.append(token)
        return token + str(len(captcha_calls)), "code"

    monkeypatch.setattr(cm, "solve_and_verify_slide_captcha", captcha)

    def submit(token, *a, **k):
        # 即时预约原先在 make_appointment 内查询用户信息；模拟该查询传播认证错误。
        if not scheduled:
            query("profile", token, {}, id_token=k["id_token"])
        submissions.append((token, k["id_token"], k["captcha_id"]))
        return {"code": "0"}

    monkeypatch.setattr(cm, "make_appointment", submit)
    if scheduled:
        job = system.manager.start_scheduled_booking(**PARAMS, target_time_str="21:00:00")
        join(system)
        assert system.store.status(job) == "done"
    else:
        assert cm._attempt_booking(**PARAMS)["ok"]
    assert logins == [False, True]
    assert submissions and all(t == "new" and i == "new-id" and c.startswith("new") for t, i, c in submissions)
    assert ("appointments", "new", "new-id") in seen


def test_relogin_failure_never_submits_booking(monkeypatch):
    monkeypatch.setattr(cm, "get_token_cached", lambda *a, **k: None if k.get("force_refresh") else {"access_token": "old"})
    monkeypatch.setattr(cm, "create_session", lambda: SimpleNamespace(close=lambda: None))

    def expired(*a, **k):
        raise api.UpstreamAuthenticationError("expired")

    monkeypatch.setattr(cm, "list_appointments_for_account", expired)
    monkeypatch.setattr(cm, "make_appointment", lambda *a, **k: pytest.fail("Unexpected submission"))
    assert cm._attempt_booking(**PARAMS)["error"] == "login_failed"


def test_cancellation_uses_refreshed_token_for_all_remaining_steps(system, monkeypatch):
    local = system.store.reserve(**SLOT)
    calls = []
    monkeypatch.setattr(service_module, "find_user_by_access_token", lambda *a: (PARAMS["username"], "old-id"))
    monkeypatch.setattr(service_module, "refresh_token_for_user", lambda *a: {"access_token": "new", "id_token": "new-id"})

    def find(token, *a, **k):
        if token == "old":
            raise api.UpstreamAuthenticationError("expired")
        calls.append((token, k["id_token"]))
        return "appointment"

    def cancel(token, *a, **k):
        calls.append((token, k["id_token"]))
        return True, ""

    monkeypatch.setattr(service_module, "find_my_appointment_id", find)
    monkeypatch.setattr(service_module, "check_appointment_cancel_time", cancel)
    monkeypatch.setattr(service_module, "update_appointment_state", cancel)
    assert system.service.cancel(**SLOT, access_token="old")["upstream_status"] == "cancelled"
    assert calls == [("new", "new-id")] * 3
    assert system.store.local_id(**SLOT) is None
    assert local is not None
