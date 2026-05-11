"""Business-hours helpers.

sim_time is integer minutes since scenario start. Wall-clock interpretation
requires a `scenario_origin_minute_of_week` (0..10080, 0 = Monday 00:00 in
the scenario timezone). All per-person working hours are local-time minute-
of-day; conversion uses the person's `timezone_offset_minutes`.

Helpers here are pure functions on these scalars — no World coupling — so
they're trivially testable.
"""

from __future__ import annotations

from sim.store.entities import Person, WorkingHours

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY


def absolute_minute_of_week(origin: int, sim_time: int) -> int:
    return (origin + sim_time) % MINUTES_PER_WEEK


def weekday(origin: int, sim_time: int) -> int:
    """0=Mon..6=Sun, in scenario timezone."""
    return (absolute_minute_of_week(origin, sim_time) // MINUTES_PER_DAY) % 7


def minute_of_day(origin: int, sim_time: int) -> int:
    """0..1439, in scenario timezone."""
    return absolute_minute_of_week(origin, sim_time) % MINUTES_PER_DAY


def person_local_minute_of_week(origin: int, sim_time: int, person: Person) -> int:
    return (origin + sim_time + person.timezone_offset_minutes) % MINUTES_PER_WEEK


def is_in_hours(origin: int, sim_time: int, person: Person) -> bool:
    """True iff `sim_time` falls inside this person's local working hours."""
    return _is_in_hours_at_local_minute(
        person_local_minute_of_week(origin, sim_time, person), person.working_hours
    )


def _is_in_hours_at_local_minute(local_minute_of_week: int, hours: WorkingHours) -> bool:
    day = (local_minute_of_week // MINUTES_PER_DAY) % 7
    if day not in hours.weekdays:
        return False
    mod = local_minute_of_week % MINUTES_PER_DAY
    return hours.start_minute <= mod < hours.end_minute


def next_business_minute(origin: int, sim_time: int, person: Person) -> int:
    """Smallest sim_time >= input at which `person` is in working hours.

    If already in-hours, returns `sim_time` unchanged.
    """
    if is_in_hours(origin, sim_time, person):
        return sim_time
    # Walk forward day-by-day until we find a working day, then snap to start
    # of the working window.
    cursor = sim_time
    hours = person.working_hours
    # Bound the loop at one full week of search; if no working day exists
    # we'd otherwise loop forever.
    for _ in range(8):
        local_mow = person_local_minute_of_week(origin, cursor, person)
        day = (local_mow // MINUTES_PER_DAY) % 7
        mod = local_mow % MINUTES_PER_DAY
        if day in hours.weekdays and mod < hours.end_minute:
            # Same working day; either currently before start, or in-hours already
            # (handled by early-return above), or after end — caller handles after-end
            # by falling through to next iteration.
            if mod < hours.start_minute:
                return cursor + (hours.start_minute - mod)
            if mod < hours.end_minute:
                return cursor  # in-hours but already handled above
        # Advance to start of next day in the person's local frame.
        cursor += MINUTES_PER_DAY - mod
    raise RuntimeError(f"no working day found within a week for person={person.id}")
