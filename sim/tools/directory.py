"""`directory.*` tool operations — the company org chart, exposed to the agent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.scheduler import Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.costs import COST_DIR_GET, COST_DIR_LIST
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


def directory_ops() -> list[ToolOp]:
    return [
        ToolOp("directory.list", DirectoryListArgs, COST_DIR_LIST, directory_list,
               description="List everyone in the company. Optionally filter by `team` or `role`."),
        ToolOp("directory.get", DirectoryGetArgs, COST_DIR_GET, directory_get,
               description="Get a single person's directory entry (role, team, manager, persona notes)."),
    ]
