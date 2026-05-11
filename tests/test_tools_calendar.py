from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import CalendarEvent, Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.calendar import calendar_ops


def _setup() -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.alice", display_name="Alice", role="eng"))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(calendar_ops())
    return world, scheduler, reg


def test_calendar_create_and_list():
    world, scheduler, reg = _setup()
    result = reg.dispatch(ToolCall(
        tool="calendar.create",
        args={"event_id": "cal.standup", "title": "Standup",
              "start_sim_time": 60, "end_sim_time": 75,
              "attendees": ["person.alice"]},
    ))
    assert result.ok
    assert result.result["event"]["organizer_id"] == "person.tpm"
    # Listing for the agent shows it
    listed = reg.dispatch(ToolCall(tool="calendar.list", args={}))
    ids = [e["id"] for e in listed.result["events"]]
    assert "cal.standup" in ids


def test_calendar_create_invalid_duration_rejected():
    world, scheduler, reg = _setup()
    result = reg.dispatch(ToolCall(
        tool="calendar.create",
        args={"event_id": "cal.bad", "title": "x",
              "start_sim_time": 100, "end_sim_time": 100},
    ))
    assert result.ok is False


def test_calendar_create_returns_conflicts_as_warning_not_error():
    world, scheduler, reg = _setup()
    world.add_calendar_event(CalendarEvent(
        id="cal.existing", title="Pre-existing", start_sim_time=60, end_sim_time=90,
        organizer_id="person.alice", attendees=["person.alice"],
    ))
    result = reg.dispatch(ToolCall(
        tool="calendar.create",
        args={"event_id": "cal.new", "title": "Overlapping",
              "start_sim_time": 75, "end_sim_time": 120,
              "attendees": ["person.alice"]},
    ))
    # Conflicts surface as warnings — creation still succeeds.
    assert result.ok
    assert len(result.result["conflicts"]) == 1
    assert result.result["conflicts"][0]["event_id"] == "cal.existing"


def test_calendar_rsvp_yes_adds_attendee():
    world, scheduler, reg = _setup()
    world.add_calendar_event(CalendarEvent(
        id="cal.x", title="x", start_sim_time=60, end_sim_time=90,
        organizer_id="person.alice", attendees=["person.alice"],
    ))
    result = reg.dispatch(ToolCall(
        tool="calendar.rsvp", args={"event_id": "cal.x", "response": "yes"},
    ))
    assert result.ok
    assert "person.tpm" in world.calendar["cal.x"].attendees


def test_calendar_rsvp_no_removes_attendee():
    world, scheduler, reg = _setup()
    world.add_calendar_event(CalendarEvent(
        id="cal.x", title="x", start_sim_time=60, end_sim_time=90,
        organizer_id="person.alice",
        attendees=["person.alice", "person.tpm"],
    ))
    result = reg.dispatch(ToolCall(
        tool="calendar.rsvp", args={"event_id": "cal.x", "response": "no"},
    ))
    assert result.ok
    assert "person.tpm" not in world.calendar["cal.x"].attendees
