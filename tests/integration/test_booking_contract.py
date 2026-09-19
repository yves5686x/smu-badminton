"""检查多个 HTTP 入口使用相同用例，以及错误在接口边界可见。"""

import pytest

from smu_badminton import routes_booking, routes_jobs
from smu_badminton.booking_api import UpstreamAuthenticationError, UpstreamQueryError

SLOT = dict(username="no_credential_contract_user", bookdate="2026-12-18", kssj="10:00", jssj="11:00", resources_name="court")


@pytest.mark.parametrize("path", ["/api/book", "/api/jobs/immediate", "/api/book/schedule", "/api/jobs/scheduled"])
def test_all_booking_entries_use_service(client, monkeypatch, path):
    calls = []

    def submit(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "error": "test_conflict"}

    monkeypatch.setattr(routes_booking.booking_service, "submit", submit)
    assert routes_jobs.booking_service is routes_booking.booking_service
    response = client.post(path, json={**SLOT, "target_time_str": "21:00:00"})
    assert response.json()["error"] == "test_conflict"
    assert len(calls) == 1
    assert calls[0].get("scheduled", False) == (path.endswith("/schedule") or path.endswith("/scheduled"))


@pytest.mark.parametrize(
    "changes",
    [dict(bookdate="2026-99-99"), dict(kssj="99:99"), dict(kssj="12:00", jssj="11:00"), dict(target_time_str="garbage")],
)
def test_bad_slot_rejected_before_service(client, monkeypatch, changes):
    def unexpected(**params):
        pytest.fail("Invalid request reached booking service")

    monkeypatch.setattr(routes_booking.booking_service, "submit", unexpected)
    response = client.post("/api/book/schedule", json={**SLOT, "target_time_str": "21:00:00", **changes})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "error, expected",
    [
        (UpstreamQueryError("timeout"), "upstream_query_failed"),
        (UpstreamAuthenticationError("expired"), "login_failed"),
    ],
)
def test_availability_errors_keep_auth_distinct(client, monkeypatch, error, expected):
    async def fail(*a, **kwargs):
        raise error

    monkeypatch.setattr(routes_booking.availability_service, "query", fail)
    response = client.post("/api/availability", json={"token": "fake", "bookdate": "2026-12-18"})
    assert response.json()["error"] == expected
