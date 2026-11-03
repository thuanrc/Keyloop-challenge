from datetime import datetime, time, timedelta

import pytest

from keyloop_test.booking import (
    OutsideBusinessHours,
    StartInPast,
    check_business_hours,
    check_not_past,
    compute_end,
)

LON = "Europe/London"
OPEN, CLOSE = time(8), time(18)


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def hours(start: str, minutes: int, tz=LON, opens=OPEN, closes=CLOSE):
    s = dt(start)
    check_business_hours(s, compute_end(s, minutes), tz, opens, closes)


def test_compute_end_adds_duration():
    assert compute_end(dt("2030-01-07T09:00+00:00"), 120) == dt("2030-01-07T11:00+00:00")


def test_past_start_rejected():
    now = dt("2030-01-07T09:00+00:00")
    with pytest.raises(StartInPast):
        check_not_past(now, now)
    check_not_past(now + timedelta(minutes=1), now)


def test_within_hours_ok():
    hours("2030-01-07T08:00+00:00", 600)  # 08:00–18:00 London winter (UTC+0)


def test_ends_after_close_rejected():
    with pytest.raises(OutsideBusinessHours):
        hours("2030-01-07T17:30+00:00", 60)


def test_starts_before_open_rejected():
    with pytest.raises(OutsideBusinessHours):
        hours("2030-01-07T07:30+00:00", 60)


def test_crosses_midnight_rejected():
    with pytest.raises(OutsideBusinessHours):
        hours("2030-01-07T23:00+00:00", 120, opens=time(0), closes=time(23, 59))


def test_offset_input_converted():
    hours("2030-01-07T15:00+07:00", 60)  # == 08:00 London


def test_dst_summer_time():
    hours("2030-07-08T07:00+00:00", 60)  # == 08:00 BST
    with pytest.raises(OutsideBusinessHours):
        hours("2030-07-08T17:30+00:00", 60)  # ends 19:30 BST
