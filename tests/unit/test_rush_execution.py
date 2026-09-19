"""定时执行边界：假上游、可控时钟，不依赖学校网络。"""

import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from smu_badminton import booking_api as api
from smu_badminton import cas_manager as cm

SLOT = dict(bookdate="2026-12-18", kssj="10:00", jssj="11:00", resources_name="court")
OK = {"data": {"saveAppointmentInformationAll": {"code": "0"}}}


@pytest.mark.parametrize("refreshed_expiry, valid", [(None, False), (1050, False), (1200, True)])
def test_token_must_cover_target_even_after_refresh(monkeypatch, refreshed_expiry, valid):
    old = {"access_token": "old", "expiry": 1050}
    monkeypatch.setattr(cm.time, "time", lambda: 1000)
    monkeypatch.setattr(cm, "session_exp_epoch", lambda t: t["expiry"])
    refreshed = {"access_token": "new", "expiry": refreshed_expiry} if refreshed_expiry else None
    monkeypatch.setattr(cm, "get_token_cached", lambda *a, **k: refreshed if k.get("force_refresh") else old)
    result = cm._prepare_rush_tokens("", "", "u", "p", datetime.fromtimestamp(1100, UTC), threading.Event())
    assert bool(result) is valid


def test_failed_refresh_can_keep_token_that_covers_submission(monkeypatch):
    tokens = {"access_token": "valid"}
    monkeypatch.setattr(cm.time, "time", lambda: 1000)
    monkeypatch.setattr(cm, "get_token_cached", lambda *a, **k: None if k.get("force_refresh") else tokens)
    monkeypatch.setattr(cm, "session_exp_epoch", lambda t: 1150)
    assert cm._prepare_rush_tokens("", "", "u", "p", datetime.fromtimestamp(1100, UTC), threading.Event()) == tokens


@pytest.mark.parametrize("pool, flag, count", [([], "1", 0), ([("id", "code")], "1", 1), ([], "0", 2)])
def test_only_prepared_credentials_are_submitted(monkeypatch, pool, flag, count):
    calls = []

    def submit(*args, **kwargs):
        calls.append(kwargs)
        return {"code": "failed"}

    monkeypatch.setattr(cm, "make_appointment", submit)
    monkeypatch.setattr(cm, "solve_and_verify_slide_captcha", lambda *a: pytest.fail("Late captcha solving"))
    assert not cm._submit_rush("t", "id", None, SLOT, ("r", "time", flag), {"user": "u"}, pool, 2, threading.Event())
    assert len(calls) == count
    for call in calls:
        assert call["allow_retry"] is False
        assert bool(call["captcha_id"]) == (flag == "1")


def test_success_during_cancellation_is_preserved(monkeypatch):
    cancel = threading.Event()

    def submit(*args, **kwargs):
        cancel.set()
        return OK

    monkeypatch.setattr(cm, "make_appointment", submit)
    assert cm._submit_rush("t", "id", None, SLOT, ("r", "time", "0"), {"user": "u"}, [], 2, cancel)


def test_worker_exception_does_not_hide_other_success(monkeypatch):
    barrier = threading.Barrier(2)

    def submit(*args, **kwargs):
        barrier.wait(timeout=2)
        if kwargs["captcha_id"] == "bad":
            raise RuntimeError("failed request")
        return OK

    monkeypatch.setattr(cm, "make_appointment", submit)
    assert cm._submit_rush(
        "t", "id", None, SLOT, ("r", "time", "1"), {"user": "u"}, [("bad", "code"), ("good", "code")], 2, threading.Event()
    )


def test_resource_budget_discards_late_result(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(cm.time, "monotonic", lambda: clock[0])

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        clock[0] = kwargs["deadline"] + 1
        return ("r", "time", "0")

    monkeypatch.setattr(cm, "fetch_resource_time_id", fetch)
    assert cm._resolve_rush_target("t", "id", None, SLOT, threading.Event()) is None
    assert len(calls) == 1


@pytest.mark.parametrize("response", [None, {"errors": [{"message": "ACCESS_TOKEN_INVALID"}]}])
def test_rush_query_never_retries_or_refreshes(monkeypatch, response):
    calls = []
    monkeypatch.setattr(api.time, "monotonic", lambda: 100)
    monkeypatch.setattr("smu_badminton.token_profile.refresh_token_for_user", lambda *a: pytest.fail("Late login"))

    def post(*a, **kwargs):
        calls.append(kwargs)
        if response is None:
            raise TimeoutError()
        return SimpleNamespace(status_code=200, json=lambda: response)

    if response is None:
        api._make_graphql_request(SimpleNamespace(post=post), "fake", {}, {}, token="t", deadline=102)
    else:
        with pytest.raises(api.UpstreamAuthenticationError):
            api._make_graphql_request(SimpleNamespace(post=post), "fake", {}, {}, token="t", deadline=102)
    assert len(calls) == 1
    assert calls[0]["timeout"] == 2


def test_late_preparation_has_bounded_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "_get_slide_captcha", lambda token: calls.append(token) or ("", ""))
    now = datetime.fromtimestamp(1000, UTC)
    assert cm._prepare_rush_captchas("t", None, 2, now, threading.Event(), lambda: now) == []
    assert calls == ["t"] * 4


def test_renewed_token_gets_new_captcha_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "_get_slide_captcha", lambda token: calls.append(token) or (token, "code"))
    now = datetime.fromtimestamp(1000, UTC)
    assert cm._prepare_rush_captchas("new", ("r", "time", "1"), 1, now, threading.Event(), lambda: now, renewed=True) == [
        ("new", "code")
    ]
    assert calls == ["new"]


def test_force_login_bypasses_cached_credentials(monkeypatch):
    from smu_badminton import credentials

    monkeypatch.setattr(credentials, "get_cached_token", lambda *a: pytest.fail("Used old token"))
    monkeypatch.setattr(credentials, "resolve_login_credentials", lambda *a: ("login", "captcha", "password"))
    monkeypatch.setattr(credentials, "login_with_retry", lambda *a, **k: {"access_token": "fresh"})
    saved = []
    monkeypatch.setattr(credentials, "cache_token_for_user", lambda *a: saved.append(a))
    assert credentials.get_token_cached("", "", "u", "p", force_refresh=True) == {"access_token": "fresh"}
    assert saved == [("u", {"access_token": "fresh"})]


@pytest.mark.parametrize("requested", [3, 5])
def test_rush_submits_at_most_two_distinct_credentials(monkeypatch, requested):
    calls = []

    def submit(*args, **kwargs):
        calls.append(kwargs["captcha_id"])
        return {"code": "failed"}

    monkeypatch.setattr(cm, "make_appointment", submit)
    pool = [(str(i), "code") for i in range(5)]
    assert not cm._submit_rush("t", "id", None, SLOT, ("r", "time", "1"), {"user": "u"}, pool, requested, threading.Event())
    assert sorted(calls) == ["0", "1"]


def test_duplicate_captcha_is_never_submitted_twice(monkeypatch):
    calls = []
    monkeypatch.setattr(cm, "make_appointment", lambda *a, **k: calls.append(k["captcha_id"]) or {"code": "failed"})
    assert not cm._submit_rush(
        "t", "id", None, SLOT, ("r", "time", "1"), {"user": "u"}, [("same", "code"), ("same", "code")], 2, threading.Event()
    )
    assert calls == ["same"]


def test_unknown_cached_expiry_triggers_fresh_login(monkeypatch):
    calls = []

    def login(*args, **kwargs):
        calls.append(kwargs.get("force_refresh", False))
        return {"access_token": "fresh" if calls[-1] else "cached"}

    monkeypatch.setattr(cm, "get_token_cached", login)
    monkeypatch.setattr(cm, "session_exp_epoch", lambda token: None)
    result = cm._prepare_rush_tokens("", "", "u", "p", datetime.now(UTC), threading.Event())
    assert result["access_token"] == "fresh"
    assert calls == [False, True]


@pytest.mark.parametrize("status", [200, 401, 403])
@pytest.mark.parametrize("query", ["profile", "capacity"])
def test_queries_propagate_auth_failure_without_hidden_login(monkeypatch, status, query):
    calls = []
    monkeypatch.setattr("smu_badminton.token_profile.refresh_token_for_user", lambda *a: pytest.fail("Hidden login"))
    response = SimpleNamespace(status_code=status, json=lambda: {"errors": [{"message": "ACCESS_TOKEN_INVALID"}]})

    def post(url, **kwargs):
        calls.append((url, kwargs["headers"]["Authorization"]))
        return response

    session = SimpleNamespace(post=post)
    with pytest.raises(api.UpstreamAuthenticationError):
        if query == "profile":
            api.get_user_info_from_appointment("old", id_token="old-id", session=session)
        else:
            api.check_resource_time_slot_capacity(
                "old", "r", ["t"], "2026-09-25", "10:00", "11:00", id_token="old-id", session=session
            )
    assert len(calls) == 1
    assert calls[0][0].endswith("id_token_hint=old-id")
    assert calls[0][1] == "Bearer old"
