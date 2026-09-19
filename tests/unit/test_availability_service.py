import asyncio
import threading

import pytest

from smu_badminton.availability import AvailabilityService
from smu_badminton.booking_api import UpstreamAuthenticationError, UpstreamQueryError, fetch_all_time_slots


def test_slot_query_preserves_login_expiry(monkeypatch):
    def expired(*args, **kwargs):
        raise UpstreamAuthenticationError("登录失效")

    monkeypatch.setattr("smu_badminton.booking_api.find_time_slots_by_resource", expired)
    with pytest.raises(UpstreamAuthenticationError):
        fetch_all_time_slots("expired", "2026-12-18", [{"id": "court", "resources_name": "场地"}])


@pytest.mark.asyncio
async def test_shared_slots_keep_personal_bookings_separate(monkeypatch):
    service = AvailabilityService()
    calls = []
    slots = {
        "court": (
            "场地",
            {
                "data": {
                    "findResourcesTimeSlotByResourcesIdAndDate": [
                        {"kssj": "10:00", "jssj": "11:00", "canAppointmentNumber": 1},
                    ]
                }
            },
        )
    }

    def load(*args):
        calls.append(args)
        return slots

    monkeypatch.setattr(service, "_load_slots", load)
    monkeypatch.setattr(
        service,
        "_my_bookings",
        lambda token, *a: (
            [{"node": {"resources_id": "court", "start_time": "10:00", "end_time": "11:00"}}] if token == "owner" else []
        ),
    )
    mine, _ = await service.query("owner", "2026-12-18")
    other, status = await service.query("other", "2026-12-18")
    assert len(calls) == 1
    assert status == "HIT-PUBLIC"
    assert mine[0]["slots"][0]["bookedByMe"] is True
    assert other[0]["slots"][0]["bookedByMe"] is False


@pytest.mark.asyncio
async def test_cancelling_leader_does_not_duplicate_shared_query(monkeypatch):
    service = AvailabilityService()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load(*args):
        calls.append(args)
        entered.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(service, "_load_slots", load)
    monkeypatch.setattr(service, "_my_bookings", lambda *a: [])
    first = asyncio.create_task(service.query("one", "2026-12-18"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(service.query("two", "2026-12-18"))
        await asyncio.sleep(0)
    finally:
        release.set()
    await second
    assert len(calls) == 1
    assert not service._inflight


@pytest.mark.asyncio
async def test_failed_query_is_not_cached(monkeypatch):
    service = AvailabilityService()

    def fail(*args):
        raise UpstreamQueryError("unavailable")

    monkeypatch.setattr(service, "_load_slots", fail)
    with pytest.raises(UpstreamQueryError):
        await service.query("one", "2026-12-18")
    assert not service._cache
    assert not service._inflight


@pytest.mark.asyncio
async def test_manual_refresh_bypasses_public_cache(monkeypatch):
    service = AvailabilityService()
    calls = []
    monkeypatch.setattr(service, "_load_slots", lambda *args: calls.append(args) or {})
    monkeypatch.setattr(service, "_my_bookings", lambda *args: [])
    await service.query("one", "2026-12-18")
    await service.query("two", "2026-12-18")
    assert len(calls) == 1
    _, status = await service.query("two", "2026-12-18", force_refresh=True)
    assert status == "MISS"
    assert len(calls) == 2
