"""`calendar.*` tool operations.

Calendar conflicts are surfaced as *warnings* in the result, not errors —
real PMs sometimes double-book and the agent should be free to decide.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sim.scheduler import Scheduler
from sim.store import CalendarEvent, World
from sim.tools.base import ToolOp
from sim.tools.costs import COST_CAL_CREATE, COST_CAL_GET, COST_CAL_LIST, COST_CAL_RSVP
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


# ---------------------------------------------------------------------------
# calendar.list
# ---------------------------------------------------------------------------


class CalendarListArgs(BaseModel):
    start_sim_time: int | None = None
    end_sim_time: int | None = None
    attendee_id: str | None = None


def calendar_list(world: World, scheduler: Scheduler, args: CalendarListArgs, caller_id: str) -> dict[str, Any]:
    attendee = args.attendee_id or caller_id
    out = []
    for evt in sorted(world.calendar.values(), key=lambda e: (e.start_sim_time, e.id)):
        if attendee not in evt.attendees:
            continue
        if args.start_sim_time is not None and evt.end_sim_time <= args.start_sim_time:
            continue
        if args.end_sim_time is not None and evt.start_sim_time >= args.end_sim_time:
            continue
        out.append(evt.model_dump())
    return {"events": out}


# ---------------------------------------------------------------------------
# calendar.get
# ---------------------------------------------------------------------------


class CalendarGetArgs(BaseModel):
    event_id: str


def calendar_get(world: World, scheduler: Scheduler, args: CalendarGetArgs, caller_id: str) -> dict[str, Any]:
    evt = world.calendar.get(args.event_id)
    if evt is None:
        raise ToolError(suggest_id("event", args.event_id, world.calendar.keys()))
    return evt.model_dump()


# ---------------------------------------------------------------------------
# calendar.rsvp
# ---------------------------------------------------------------------------


class CalendarRsvpArgs(BaseModel):
    event_id: str
    response: Literal["yes", "no", "tentative"]


def calendar_rsvp(world: World, scheduler: Scheduler, args: CalendarRsvpArgs, caller_id: str) -> dict[str, Any]:
    evt = world.calendar.get(args.event_id)
    if evt is None:
        raise ToolError(suggest_id("event", args.event_id, world.calendar.keys()))
    if args.response in ("yes", "tentative"):
        if caller_id not in evt.attendees:
            evt.attendees.append(caller_id)
    elif args.response == "no":
        if caller_id in evt.attendees:
            evt.attendees.remove(caller_id)
    world._emit("calendar_rsvp", {"event_id": args.event_id, "person_id": caller_id, "response": args.response})
    return evt.model_dump()


# ---------------------------------------------------------------------------
# calendar.create
# ---------------------------------------------------------------------------


class CalendarCreateArgs(BaseModel):
    event_id: str
    title: str
    start_sim_time: int
    end_sim_time: int
    attendees: list[str] = Field(default_factory=list)
    location: str = ""
    agenda: str = ""


def _conflicts_for(world: World, attendees: list[str], start: int, end: int) -> list[dict]:
    conflicts = []
    for evt in world.calendar.values():
        if evt.end_sim_time <= start or evt.start_sim_time >= end:
            continue
        for att in attendees:
            if att in evt.attendees:
                conflicts.append({"event_id": evt.id, "attendee_id": att})
    return conflicts


def calendar_create(world: World, scheduler: Scheduler, args: CalendarCreateArgs, caller_id: str) -> dict[str, Any]:
    if args.end_sim_time <= args.start_sim_time:
        raise ToolError("end_sim_time must be after start_sim_time")
    if args.event_id in world.calendar:
        raise ToolError(f"event already exists: {args.event_id}")
    for att in args.attendees:
        if att not in world.people:
            raise ToolError(suggest_id("person (attendee)", att, world.people.keys()))
    attendees = sorted(set([caller_id, *args.attendees]))
    conflicts = _conflicts_for(world, attendees, args.start_sim_time, args.end_sim_time)
    evt = CalendarEvent(
        id=args.event_id, title=args.title,
        start_sim_time=args.start_sim_time, end_sim_time=args.end_sim_time,
        organizer_id=caller_id, attendees=attendees,
        location=args.location, agenda=args.agenda,
    )
    world.add_calendar_event(evt)
    return {"event": evt.model_dump(), "conflicts": conflicts}


def calendar_ops() -> list[ToolOp]:
    return [
        ToolOp("calendar.list", CalendarListArgs, COST_CAL_LIST, calendar_list,
               description="List calendar events you're invited to, optionally filtered by a sim_time window."),
        ToolOp("calendar.get", CalendarGetArgs, COST_CAL_GET, calendar_get,
               description="Get one calendar event's details (title, attendees, agenda)."),
        ToolOp("calendar.rsvp", CalendarRsvpArgs, COST_CAL_RSVP, calendar_rsvp,
               description="RSVP yes/no/tentative to an event. 'no' removes you from attendees."),
        ToolOp("calendar.create", CalendarCreateArgs, COST_CAL_CREATE, calendar_create,
               description="Create a calendar event. Returns conflicts as a warning but does not refuse — real PMs sometimes double-book."),
    ]
