from __future__ import annotations

from sim.store import Person, WorkingHours
from sim.store.worktime import (
    MINUTES_PER_DAY,
    MINUTES_PER_WEEK,
    is_in_hours,
    minute_of_day,
    next_business_minute,
    weekday,
)

# Scenario origin = Monday 09:00 = weekday 0, minute 540
MON_9AM = 0 * MINUTES_PER_DAY + 9 * 60


def p(pid: str = "person.x", *, tz_offset=0, hours=None) -> Person:
    return Person(
        id=pid, display_name=pid, role="engineer",
        timezone_offset_minutes=tz_offset,
        working_hours=hours or WorkingHours(),
    )


def test_weekday_progression():
    assert weekday(MON_9AM, 0) == 0  # Mon
    assert weekday(MON_9AM, MINUTES_PER_DAY) == 1  # Tue
    assert weekday(MON_9AM, 6 * MINUTES_PER_DAY) == 6  # Sun
    assert weekday(MON_9AM, 7 * MINUTES_PER_DAY) == 0  # back to Mon


def test_minute_of_day():
    assert minute_of_day(MON_9AM, 0) == 9 * 60
    assert minute_of_day(MON_9AM, 60) == 10 * 60
    # Wrap across midnight
    assert minute_of_day(MON_9AM, 15 * 60) == 0  # 09:00 + 15h = 00:00 Tue


def test_in_hours_default_9_to_5_weekdays():
    person = p()
    # Monday 09:00 sharp
    assert is_in_hours(MON_9AM, 0, person) is True
    # Monday 08:59
    assert is_in_hours(MON_9AM, -1, person) is False  # would underflow but works mod
    # Monday 16:59
    assert is_in_hours(MON_9AM, 7 * 60 + 59, person) is True
    # Monday 17:00 (end exclusive)
    assert is_in_hours(MON_9AM, 8 * 60, person) is False


def test_in_hours_weekend_excluded():
    person = p()
    # Saturday 09:00
    sat_morning = 5 * MINUTES_PER_DAY
    assert is_in_hours(MON_9AM, sat_morning, person) is False


def test_in_hours_with_timezone_offset_east():
    """Person 3h east — their 9am is 3h before scenario clock 9am."""
    east = p(tz_offset=3 * 60)
    # At scenario sim_time 0 (Mon 09:00 scenario), east person is at 12:00 local
    assert is_in_hours(MON_9AM, 0, east) is True
    # At scenario Mon 14:00 (sim_time 5h), east is at 17:00 — end exclusive → False
    assert is_in_hours(MON_9AM, 5 * 60, east) is False
    # At scenario Mon 13:59 east is at 16:59 — in hours
    assert is_in_hours(MON_9AM, 4 * 60 + 59, east) is True


def test_in_hours_with_timezone_offset_west():
    west = p(tz_offset=-3 * 60)
    # At scenario Mon 09:00 west is at 06:00 — too early
    assert is_in_hours(MON_9AM, 0, west) is False
    # At scenario Mon 12:00 west is at 09:00 — in hours
    assert is_in_hours(MON_9AM, 3 * 60, west) is True


def test_next_business_minute_already_in_hours_returns_same():
    person = p()
    assert next_business_minute(MON_9AM, 0, person) == 0
    assert next_business_minute(MON_9AM, 60, person) == 60


def test_next_business_minute_before_start_pushes_to_start():
    """Person comes online 3h late on Monday morning."""
    person = p()
    # Scenario starts at Mon 09:00; rewind start to Mon 06:00 by setting origin earlier
    origin = 6 * 60  # Mon 06:00
    # At sim_time 0 (Mon 06:00), 3h before start
    assert next_business_minute(origin, 0, person) == 3 * 60


def test_next_business_minute_after_end_pushes_to_next_day():
    person = p()
    # sim_time 9h = Mon 18:00 (past end). Next business is Tue 09:00 = sim_time 9h + 15h = 24h
    assert next_business_minute(MON_9AM, 9 * 60, person) == 24 * 60


def test_next_business_minute_friday_evening_pushes_to_monday():
    person = p()
    # Mon 09:00 + 4 days + 9h = Fri 18:00
    fri_evening = 4 * MINUTES_PER_DAY + 9 * 60
    # Next working minute is Mon 09:00 = +7 days from start = sim_time 7*1440
    assert next_business_minute(MON_9AM, fri_evening, person) == 7 * MINUTES_PER_DAY


def test_next_business_minute_weekend_pushes_to_monday():
    person = p()
    # Sat 12:00
    sat_noon = 5 * MINUTES_PER_DAY + 3 * 60
    expected = 7 * MINUTES_PER_DAY  # Mon 09:00
    assert next_business_minute(MON_9AM, sat_noon, person) == expected


def test_custom_working_hours_respected():
    early_bird = p(hours=WorkingHours(start_minute=6 * 60, end_minute=14 * 60))
    # Mon 06:00 = sim_time -3h. Use origin starting earlier.
    origin = 6 * 60
    assert is_in_hours(origin, 0, early_bird) is True
    assert is_in_hours(origin, 8 * 60, early_bird) is False  # 14:00


def test_minute_of_week_wraps_modulo():
    """sim_time longer than a week wraps cleanly."""
    person = p()
    # sim_time = 8 days = 11520 minutes; equivalent to Tue 09:00 (since origin is Mon 09:00)
    assert weekday(MON_9AM, 8 * MINUTES_PER_DAY) == 1
