"""`directory.*` tool operations — the company org chart, exposed to the agent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.scheduler import Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.costs import COST_DIR_GET, COST_DIR_LIST, COST_DIR_PRESENCE
from sim.tools.presence import presence_for_person
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


class DirectoryListArgs(BaseModel):
    team: str | None = None
    role: str | None = None


def directory_list(world: World, scheduler: Scheduler, args: DirectoryListArgs, caller_id: str) -> dict[str, Any]:
    out = []
    for person in sorted(world.people.values(), key=lambda p: p.id):
        if args.team and person.team != args.team:
            continue
        if args.role and person.role != args.role:
            continue
        out.append({
            "id": person.id, "display_name": person.display_name, "role": person.role,
            "team": person.team, "manager_id": person.manager_id,
        })
    return {"people": out}


class DirectoryGetArgs(BaseModel):
    person_id: str


def directory_get(world: World, scheduler: Scheduler, args: DirectoryGetArgs, caller_id: str) -> dict[str, Any]:
    person = world.get_person(args.person_id)
    if person is None:
        raise ToolError(suggest_id("person", args.person_id, world.people.keys()))
    return {
        "id": person.id, "display_name": person.display_name,
        "role": person.role, "team": person.team, "manager_id": person.manager_id,
        "persona_notes": person.persona_notes,
    }


class DirectoryPresenceArgs(BaseModel):
    person_id: str


def directory_presence(
    world: World, scheduler: Scheduler, args: DirectoryPresenceArgs, caller_id: str,
) -> dict[str, Any]:
    """Return the presence projection for `person_id` at the current sim_time.

    Same shape as the `presence` block in `chat.list` for DM peers. The
    schema is the canonical "is this person available right now?" reply —
    `{in_meeting, available_at, current_event_title}`. See
    `sim.tools.presence` for field semantics.
    """
    person = world.get_person(args.person_id)
    if person is None:
        raise ToolError(suggest_id("person", args.person_id, world.people.keys()))
    presence = presence_for_person(world, person, scheduler.sim_time)
    return {
        "person_id": person.id,
        "display_name": person.display_name,
        **presence,
    }


def directory_ops() -> list[ToolOp]:
    return [
        ToolOp("directory.list", DirectoryListArgs, COST_DIR_LIST, directory_list,
               description="List everyone in the company. Optionally filter by `team` or `role`."),
        ToolOp("directory.get", DirectoryGetArgs, COST_DIR_GET, directory_get,
               description="Get a single person's directory entry (role, team, manager, persona notes)."),
        ToolOp("directory.presence", DirectoryPresenceArgs, COST_DIR_PRESENCE,
               directory_presence,
               description="Check whether a person is in a meeting right now. "
                           "Returns `{in_meeting, available_at, current_event_title}`. "
                           "Useful before pinging someone in DMs."),
    ]
