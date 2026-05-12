"""Tool registry — the single dispatch point for agent-initiated actions.

Each call:
  1. Looks up the op by name (`chat.send` etc.).
  2. Validates args through the op's pydantic model.
  3. Computes the sim-time cost.
  4. Invokes the handler, which mutates World.
  5. Advances the scheduler by the cost (drains any pending events).
  6. Collects notifications addressed to the caller that arrived since the
     last dispatch and returns them in the result.

ACL is enforced inside individual handlers. Validation errors and handler
exceptions surface as `ToolResult(ok=False, error=...)` — never as raw
Python exceptions to the agent.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import ValidationError

from sim.scheduler import Scheduler
from sim.store import World
from sim.store.entities import Notification
from sim.tools.base import ToolCall, ToolOp, ToolResult, from_sdk_name


# Failed tool calls still cost real time — a TPM that walks into a room for
# a meeting that already ended burns a minute discovering it. Without this,
# the scheduler doesn't advance on failure and the agent loop can get stuck
# calling the same broken tool forever (the in-flight run wasted 136 of 250
# turns this way before the budget ran out). 1 sim-minute is enough to
# guarantee tight loops eventually terminate against end_sim_time.
FAILURE_COST_MINUTES = 1


class ToolRegistry:
    def __init__(self, world: World, scheduler: Scheduler, caller_id: str) -> None:
        self.world = world
        self.scheduler = scheduler
        self.caller_id = caller_id
        self._ops: dict[str, ToolOp] = {}
        # Cursor: returned all notifications for caller with `created_at < this`.
        # Bumped past `sim_time` after each dispatch.
        self._notif_cursor: int = scheduler.sim_time
        # Notification ids already delivered to caller (avoids re-delivery if
        # multiple notifications share the same created_at).
        self._delivered_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, op: ToolOp) -> None:
        if op.name in self._ops:
            raise ValueError(f"tool op already registered: {op.name}")
        self._ops[op.name] = op

    def register_all(self, ops: Iterable[ToolOp]) -> None:
        for op in ops:
            self.register(op)

    def list_tools(self) -> list[str]:
        return sorted(self._ops)

    def get_op(self, name: str) -> ToolOp:
        return self._ops[name]

    def tool_specs(self) -> list[dict]:
        """Anthropic-SDK-compatible tool specs for every registered op.

        The SDK requires tool names to match `[a-zA-Z0-9_-]`, so names get
        translated `chat.send` → `chat__send` here. `dispatch()` accepts both
        the dot form and the underscore form.
        """
        return [op.to_spec() for _, op in sorted(self._ops.items())]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _failure(self, call: ToolCall, error: str) -> ToolResult:
        """Build a failed ToolResult and advance sim_time by FAILURE_COST_MINUTES.

        Charging real time on failure is what makes the scheduler-based design
        safe against agents that loop on a broken call. With cost=0 on failure
        the agent could call the same failing tool indefinitely; with cost=1
        the scheduler eventually crosses end_sim_time and the run terminates.
        """
        self.scheduler.advance(FAILURE_COST_MINUTES)
        return ToolResult(
            ok=False, tool=call.tool, sim_time=self.scheduler.sim_time,
            cost_minutes=FAILURE_COST_MINUTES, error=error,
        )

    def dispatch(self, call: ToolCall) -> ToolResult:
        op = self._ops.get(call.tool) or self._ops.get(from_sdk_name(call.tool))
        if op is None:
            return self._failure(call, f"unknown tool: {call.tool}")

        try:
            args = op.args_model.model_validate(call.args)
        except ValidationError as e:
            return self._failure(call, f"invalid args: {e.errors()[0]['msg']}")

        try:
            declared_cost = op.cost_for(args)
        except Exception as e:
            return self._failure(call, f"cost computation failed: {e}")

        start_sim_time = self.scheduler.sim_time
        try:
            result = op.handler(self.world, self.scheduler, args, self.caller_id)
        except ToolError as e:
            return self._failure(call, str(e))
        except Exception as e:
            return self._failure(call, f"handler error: {e}")

        # Most tools have a declared cost > 0 and don't touch the scheduler
        # themselves; we advance now. `wait.*` handlers advance the scheduler
        # internally with declared_cost == 0, so this is a no-op for them.
        if declared_cost > 0:
            self.scheduler.advance(declared_cost)

        actual_cost = self.scheduler.sim_time - start_sim_time
        notifications = self._collect_notifications()
        return ToolResult(
            ok=True, tool=call.tool, sim_time=self.scheduler.sim_time,
            cost_minutes=actual_cost, result=result, notifications=notifications,
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _collect_notifications(self) -> list[Notification]:
        out: list[Notification] = []
        now = self.scheduler.sim_time
        for notif in self.world.notifications.values():
            if notif.recipient_id != self.caller_id:
                continue
            if notif.id in self._delivered_ids:
                continue
            if notif.created_at > now:
                continue
            out.append(notif)
            self._delivered_ids.add(notif.id)
        out.sort(key=lambda n: (n.created_at, n.id))
        self._notif_cursor = now
        return out


class ToolError(Exception):
    """Raised by tool handlers to signal a structured failure that should be
    returned to the agent as `ToolResult(ok=False, error=...)`."""
