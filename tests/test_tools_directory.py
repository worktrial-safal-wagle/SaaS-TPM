"""Tests for `directory.presence` (Phase 5 presence-visibility surface).

`directory.presence` is the agent's "is this person available right now?"
read. Same projection used by `chat.list`'s per-DM presence block — so a
single canonical schema is exercised across both surfaces.
"""

from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import CalendarEvent, Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.directory import directory_ops


def _world() -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm",
                            team="exec", is_agent=True))
    world.add_person(Person(id="person.kai", display_name="Kai", role="eng",
                            team="eng"))
    world.add_person(Person(id="person.maya", display_name="Maya", role="eng",
                            team="eng"))
    registry = ToolRegistry(world, scheduler, caller_id="person.tpm")
    registry.register_all(directory_ops())
    return world, scheduler, registry


def test_presence_reports_available_when_not_busy():
    world, scheduler, registry = _world()
    # Kai's busy_until is 0 (default); now is 0; not busy.
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kai"},
    ))
    assert result.ok, result.error
    payload = result.result
    assert payload["person_id"] == "person.kai"
    assert payload["in_meeting"] is False
    assert payload["available_at"] is None
    assert payload["current_event_title"] is None


def test_presence_reflects_busy_until_when_set_without_event():
    """busy_until set by a long action (no covering calendar event) reports
    in_meeting=True but current_event_title=None."""
    world, scheduler, registry = _world()
    world.people["person.kai"].busy_until = 90
    # scheduler.sim_time is 0; busy_until=90 means Kai is busy for 90 min.
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kai"},
    ))
    assert result.ok
    payload = result.result
    assert payload["in_meeting"] is True
    assert payload["available_at"] == 90
    assert payload["current_event_title"] is None


def test_presence_reflects_active_calendar_event_title():
    """When a calendar event covers `now` for the person AND busy_until is
    set, presence reports the event's title."""
    world, scheduler, registry = _world()
    world.add_calendar_event(CalendarEvent(
        id="cal.launch_review", title="Launch review",
        start_sim_time=0, end_sim_time=60,
        organizer_id="person.tpm", attendees=["person.kai"],
    ))
    world.people["person.kai"].busy_until = 60
    # scheduler.sim_time is 0; event spans [0, 60), Kai is in it.
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kai"},
    ))
    assert result.ok
    payload = result.result
    assert payload["in_meeting"] is True
    assert payload["available_at"] == 60
    assert payload["current_event_title"] == "Launch review"


def test_presence_for_unknown_person_returns_suggestion():
    """Bad person_id should produce a structured 'did you mean' message so the
    agent can self-correct in one turn instead of looping."""
    world, scheduler, registry = _world()
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kia"},  # typo for kai
    ))
    assert result.ok is False
    err = result.error.lower()
    assert "person" in err
    # The suggester picks 'person.kai' as the close match for 'person.kia'.
    assert "person.kai" in result.error


def test_presence_costs_one_sim_minute():
    """Same cost as a directory lookup — cheap, but not free."""
    world, scheduler, registry = _world()
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kai"},
    ))
    assert result.ok
    assert result.cost_minutes == 1


def test_presence_after_busy_until_elapsed():
    """When sim_time advances past busy_until, the person is no longer busy."""
    world, scheduler, registry = _world()
    world.people["person.kai"].busy_until = 30
    scheduler.advance(45)  # now=45; busy_until=30; no longer busy
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kai"},
    ))
    assert result.ok
    payload = result.result
    assert payload["in_meeting"] is False
    assert payload["available_at"] is None


def test_presence_event_only_counts_when_person_is_attendee():
    """If a calendar event covers `now` but the person isn't an attendee, no
    event title is reported — they're not in *that* meeting."""
    world, scheduler, registry = _world()
    world.add_calendar_event(CalendarEvent(
        id="cal.exec_only", title="Exec sync",
        start_sim_time=0, end_sim_time=60,
        organizer_id="person.tpm", attendees=["person.maya"],  # Maya only
    ))
    world.people["person.kai"].busy_until = 60  # Kai busy for a different reason
    result = registry.dispatch(ToolCall(
        tool="directory.presence", args={"person_id": "person.kai"},
    ))
    assert result.ok
    payload = result.result
    assert payload["in_meeting"] is True
    assert payload["current_event_title"] is None
