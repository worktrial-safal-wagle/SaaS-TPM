"""Presence projection shared by `chat.list` and `directory.presence`.

A single canonical schema for "is this person available right now?" so the
agent never has to reconcile two slightly-different presence dicts. The
shape is intentionally minimal — three fields — so downstream evaluators
can score "did the agent respect presence?" without parsing prose:

  - `in_meeting: bool` — True iff `busy_until > now`. The name says "meeting"
    because that's the dominant case, but the field is also True when the
    person is mid-long-action (e.g., a 90-minute design session triggered
    outside the calendar). `current_event_title` distinguishes the two.
  - `available_at: int | None` — the sim_time at which the person becomes
    free. None when not busy.
  - `current_event_title: str | None` — title of the calendar event covering
    `now` for this person, if one exists. None when busy_until is set but
    no event covers now (e.g., long action) — the caller can still tell
    "they're not available" but won't get a meeting label.
"""

from __future__ import annotations

from typing import Any

from sim.store import World
from sim.store.entities import Person


def presence_for_person(world: World, person: Person, now: int) -> dict[str, Any]:
    """Build a presence projection for `person` at sim_time `now`.

    Pure read; no mutation. Safe to call from any tool path or from the
    briefing assembler.
    """
    in_meeting = person.busy_until > now
    available_at: int | None = person.busy_until if in_meeting else None
    current_event_title: str | None = None
    if in_meeting:
        for evt in world.calendar.values():
            if person.id not in evt.attendees:
                continue
            if evt.start_sim_time <= now < evt.end_sim_time:
                current_event_title = evt.title
                break
    return {
        "in_meeting": in_meeting,
        "available_at": available_at,
        "current_event_title": current_event_title,
    }
