"""`idle.until` tool — opt-in deferral of the agent's next poll.

In the tick-driven driver, the agent is polled every `tick_size_minutes` by
default. `idle.until` lets the agent skip ahead to a specific sim_time
(capped at 120 sim-min) when it has nothing useful to do this tick. The
agent's `next_poll_at` is updated and a fresh `actor_poll` event is
scheduled at the new fire time — the driver's end-of-tick logic respects
the agent's explicit override and won't reschedule on top of it.

Cost: 1 sim-min (the decision to defer takes thought, and we need a
non-zero cost so a tight idle-loop still advances the simulated clock).

The handler advances the scheduler itself (declared cost 0 in the registry)
so we can schedule the new poll at a fire_at that's strictly greater than
the post-advance sim_time. Otherwise a request to "idle for 1 minute"
would have the new poll fire silently inside the registry's cost advance.

Replaces the deprecated wait-for-next-event tool that existed in the
action-driven loop.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.scheduler import EVENT_KIND_ACTOR_POLL, Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.registry import ToolError


# Maximum forward-skip per idle.until call. The agent should not be able to
# blank out 8 hours of simulated wall-clock in one tool call; we want it to
# stay reactive to incoming events. 120 sim-min ≈ 2 hours.
IDLE_UNTIL_CAP_MIN = 120

# Charged cost in sim-min. Small but non-zero so an idle-loop still
# advances sim_time past end_sim_time. Also models the few real-world
# seconds it takes a professional to decide "I'll come back to this after
# lunch." Applied by the handler directly (declared cost in the registry
# is 0; see module docstring for why).
IDLE_UNTIL_COST_MIN = 1


class IdleUntilArgs(BaseModel):
    target_sim_time: int
    # Optional knob for a future "wake on event kind X" variant. Accepted
    # and ignored today — the field is here so agents that emit it don't
    # fail validation when the feature lands.
    or_on_event: str | None = None


def idle_until(world: World, scheduler: Scheduler, args: IdleUntilArgs, caller_id: str) -> dict[str, Any]:
    person = world.get_person(caller_id)
    if person is None:
        raise ToolError(f"unknown caller: {caller_id}")

    current = scheduler.sim_time
    # Requested duration relative to NOW. Past targets get clamped to at
    # least current + 1 (must be strictly in the future).
    requested_delta = args.target_sim_time - current
    clamped = False
    if requested_delta > IDLE_UNTIL_CAP_MIN:
        requested_delta = IDLE_UNTIL_CAP_MIN
        clamped = True
    if requested_delta < 1:
        requested_delta = 1

    new_next_poll = current + requested_delta
    person.next_poll_at = new_next_poll

    # Pay the cost up front (advance sim_time by IDLE_UNTIL_COST_MIN). The
    # registry's declared cost for this op is 0 so we don't double-advance.
    # We always advance at least IDLE_UNTIL_COST_MIN even if it would push
    # us past `new_next_poll`; in that case we clamp the new_next_poll to
    # be strictly after the post-advance sim_time so the new poll lands in
    # a future `_wait_for_next_agent_poll` rather than during this advance.
    cost_target = current + IDLE_UNTIL_COST_MIN
    if cost_target > current:
        scheduler.advance(IDLE_UNTIL_COST_MIN)
    post_advance = scheduler.sim_time
    if new_next_poll <= post_advance:
        new_next_poll = post_advance + 1
        person.next_poll_at = new_next_poll

    # Schedule a fresh actor_poll at the new fire time. The driver dedupes
    # stale earlier polls by comparing `event.fire_at` to
    # `person.next_poll_at` when each poll fires.
    scheduler.schedule(
        fire_at=new_next_poll,
        kind=EVENT_KIND_ACTOR_POLL,
        payload={
            "actor_id": caller_id,
            "event_id": f"actor_poll.{caller_id}.{new_next_poll}",
        },
    )

    result: dict[str, Any] = {
        "next_poll_at": new_next_poll,
        "sim_time": scheduler.sim_time,
    }
    if clamped:
        result["warning"] = (
            f"idle.until capped at {IDLE_UNTIL_CAP_MIN} sim-min; "
            f"requested {args.target_sim_time - current} min."
        )
    return result


def idle_ops() -> list[ToolOp]:
    # Declared cost is 0 because the handler advances the scheduler
    # directly (mirrors `wait.until`). The ToolResult's `cost_minutes`
    # reflects the actual sim-time spent (IDLE_UNTIL_COST_MIN).
    return [
        ToolOp(
            "idle.until",
            IdleUntilArgs,
            0,
            idle_until,
            description=(
                "Defer your next tick poll until a future sim_time. "
                "Useful when nothing actionable is pending and you'd rather "
                f"wait. Capped at {IDLE_UNTIL_CAP_MIN} sim-min per call so "
                "you stay responsive to incoming events. Past targets are "
                "clamped to at least current + 2 sim-min so the new poll "
                "lands after the tool's own cost advance."
            ),
        ),
    ]
