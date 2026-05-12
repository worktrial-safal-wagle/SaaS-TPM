"""`abandon.current` tool — pre-empt a long-running action.

In the tick-driven driver, the agent is polled every tick even while a long
action (large doc edit, meeting attendance) is still "running" in
sim-time (`busy_until > sim_time`). The agent can choose to stop early by
calling `abandon.current` — they pay a 2 sim-min transition cost and their
`busy_until` is truncated to current + 2.

If the agent is not currently busy, the call is a no-op success with a
warning. Cost is still charged (we don't reward the agent for asking).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.scheduler import Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.registry import ToolError


# Transition penalty in sim-min. Switching attention from a long task back
# to inbox-driven work isn't instant — context-switch cost is real.
ABANDON_TRANSITION_COST_MIN = 2


class AbandonCurrentArgs(BaseModel):
    # Empty-args model. Agent invocations carry no parameters; we accept a
    # no-op model so the registry's pydantic validation path is uniform
    # with every other tool.
    pass


def abandon_current(world: World, scheduler: Scheduler, args: AbandonCurrentArgs, caller_id: str) -> dict[str, Any]:
    person = world.get_person(caller_id)
    if person is None:
        raise ToolError(f"unknown caller: {caller_id}")

    current = scheduler.sim_time
    previous_busy_until = person.busy_until

    if previous_busy_until > current:
        # Truncate the in-flight long action. The 2-min transition is the
        # agent's penalty for switching mid-action.
        person.busy_until = current + ABANDON_TRANSITION_COST_MIN
        return {
            "abandoned": True,
            "previous_busy_until": previous_busy_until,
            "busy_until": person.busy_until,
            "sim_time": current,
        }

    # Not busy — nothing to abandon. The call still cost the agent 2
    # sim-min (registry advances it after the handler returns), so we just
    # return a warning to make the wasted move visible.
    return {
        "abandoned": False,
        "warning": "no long action in progress; abandon.current is a no-op.",
        "busy_until": previous_busy_until,
        "sim_time": current,
    }


def abandon_ops() -> list[ToolOp]:
    return [
        ToolOp(
            "abandon.current",
            AbandonCurrentArgs,
            ABANDON_TRANSITION_COST_MIN,
            abandon_current,
            description=(
                "Pre-empt your current long-running action and return to the "
                "inbox. Costs 2 sim-min as a context-switch penalty. Useful "
                "when a higher-priority signal arrives mid-action. No-op if "
                "you aren't currently busy."
            ),
        ),
    ]
