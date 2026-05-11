"""`wait.*` operations — explicit sim-time advancement.

Two flavors:
  - `wait.until(target)` — advance to a specific sim_time.
  - `wait.for_next_event()` — advance to the next *agent-visible* scheduled event.

The cost of a wait is the elapsed sim time. Wait is the only tool whose cost
is literally the cost.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.scheduler import Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.costs import AGENT_VISIBLE_EVENT_KINDS
from sim.tools.registry import ToolError


# ---------------------------------------------------------------------------
# wait.until
# ---------------------------------------------------------------------------


class WaitUntilArgs(BaseModel):
    target_sim_time: int


def _wait_until_cost(args: WaitUntilArgs) -> int:
    # The actual advance is performed inside the handler so the scheduler
    # is the source of truth for sim_time. We must declare cost *before*
    # the handler runs (registry-imposed), so we capture it here using the
    # current sim_time pulled from the args at validation time. This is
    # a slight asymmetry from other tools — we resolve it by setting cost
    # in the handler-and-result path: cost is reported but the scheduler
    # state advance happens entirely inside the handler.
    return 0  # placeholder; actual advance is in the handler.


def wait_until(world: World, scheduler: Scheduler, args: WaitUntilArgs, caller_id: str) -> dict[str, Any]:
    if args.target_sim_time < scheduler.sim_time:
        raise ToolError(
            f"target_sim_time ({args.target_sim_time}) is in the past (now={scheduler.sim_time})"
        )
    if args.target_sim_time == scheduler.sim_time:
        return {"waited_minutes": 0, "sim_time": scheduler.sim_time}
    delta = args.target_sim_time - scheduler.sim_time
    scheduler.advance(delta)
    return {"waited_minutes": delta, "sim_time": scheduler.sim_time}


# ---------------------------------------------------------------------------
# wait.for_next_event
# ---------------------------------------------------------------------------


class WaitForNextEventArgs(BaseModel):
    pass


def wait_for_next_event(world: World, scheduler: Scheduler, args: WaitForNextEventArgs, caller_id: str) -> dict[str, Any]:
    target = scheduler.next_matching_fire_at(AGENT_VISIBLE_EVENT_KINDS)
    if target is None:
        raise ToolError("no agent-visible event is scheduled; cannot wait")
    if target <= scheduler.sim_time:
        # An event is already due — drain it without advancing.
        scheduler.advance(0)
        return {"waited_minutes": 0, "sim_time": scheduler.sim_time}
    delta = target - scheduler.sim_time
    scheduler.advance(delta)
    return {"waited_minutes": delta, "sim_time": scheduler.sim_time}


# ---------------------------------------------------------------------------
# Op bundle
# ---------------------------------------------------------------------------


def wait_ops() -> list[ToolOp]:
    return [
        # `wait.until` cost is reported as 0 because the handler performs the
        # advance itself (the registry would double-advance otherwise). The
        # ToolResult still reflects the new sim_time correctly.
        ToolOp("wait.until", WaitUntilArgs, 0, wait_until,
               description="Advance sim_time to a specific target. Use to skip dead time."),
        ToolOp("wait.for_next_event", WaitForNextEventArgs, 0, wait_for_next_event,
               description="Advance sim_time to the next agent-visible event (incoming DM, email, calendar start, heartbeat). Sharper than wait.until — no need to guess the time."),
    ]
