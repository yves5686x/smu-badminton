from smu_badminton.http_utils import get_target_datetime_from_network


def test_normal_schedule_still_uses_seven_days_before_booking():
    target = get_target_datetime_from_network("21:00:00", "2026-09-26")
    assert target.isoformat() == "2026-09-19T21:00:00+08:00"
