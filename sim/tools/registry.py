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


# Fallback failure cost (minutes) — used only when we can't determine the
# action's declared cost. Failed tool calls normally cost the action's
# *declared* sim-time cost (`op.cost_for(args)`): a failed 10-min docs.create
# should waste 10 sim-min, not 1, so a 10-call docs.create failure loop burns
# 100 sim-min and the budget-pressure surfaces the bug. Cases where we fall
# back to this constant:
#   - unknown tool (no `op` to call `cost_for` on);
#   - args failed pydantic validation (no typed `args` to pass to `cost_for`);
#   - `cost_for(args)` itself raised (extremely rare; defensive only).
# In all three the agent hasn't actually "attempted" a known costed action,
# so charging the minimum is fair. We keep this > 0 so tight loops on broken
# calls still cross `end_sim_time` and terminate.
FAILURE_COST_MINUTES = 1


def _format_validation_error(e: ValidationError) -> str:
    """Format a Pydantic ValidationError into a self-recoverable message.

    Pydantic's default `errors()[0]['msg']` drops the field name and error
    type, leaving the agent with messages like `"Field required"` that don't
    say *which* field. Real Sonnet runs got stuck looping the same incomplete
    `docs.create` call 10+ times because the error didn't say "body".

    Format: `invalid args: <field>: <msg> (type=<type>); <field2>: <msg2> ...`
    Up to 5 errors are reported (more than that is almost certainly a wholly
    malformed call where the first 5 are enough to diagnose).
    """
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc") or []) or "<args>"
        msg = err.get("msg", "")
        etype = err.get("type", "")
        parts.append(f"{loc}: {msg}" + (f" (type={etype})" if etype else ""))
    suffix = f" [+{len(e.errors()) - 5} more]" if len(e.errors()) > 5 else ""
    return "invalid args: " + "; ".join(parts) + suffix


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

    def _failure(
        self, call: ToolCall, error: str, *,
        op: ToolOp | None = None, args: object | None = None,
    ) -> ToolResult:
        """Build a failed ToolResult and advance sim_time by the action's cost.

        Policy: a failed call that *got far enough to know what it was trying
        to do* costs its declared sim-time. Only fail-fast paths (unknown tool,
        args failed validation, cost computation itself raised) fall back to
        `FAILURE_COST_MINUTES`. This makes failure-loop bugs surface in
        sim-time budgets — a 10-call docs.create failure loop burns 100
        sim-min, not 10, so the budget pressure is visible in eval.

        Charging real time on failure also keeps the scheduler-based design
        safe against agents looping on a broken call: with cost > 0 the loop
        eventually crosses end_sim_time and terminates.
        """
        if op is not None and args is not None:
            try:
                cost = op.cost_for(args)
            except Exception:
                # `cost_for` shouldn't raise when args is a parsed model,
                # but if it does we fall back rather than mask the real
                # failure with a second exception.
                cost = FAILURE_COST_MINUTES
        else:
            cost = FAILURE_COST_MINUTES
        self.scheduler.advance(cost)
        return ToolResult(
            ok=False, tool=call.tool, sim_time=self.scheduler.sim_time,
            cost_minutes=cost, error=error,
        )

    def dispatch(self, call: ToolCall) -> ToolResult:
        op = self._ops.get(call.tool) or self._ops.get(from_sdk_name(call.tool))
        if op is None:
            # Unknown tool — no `cost_for` available, fall back.
            return self._failure(call, f"unknown tool: {call.tool}")

        try:
            args = op.args_model.model_validate(call.args)
        except ValidationError as e:
            # Args didn't parse — we don't have a typed model to pass to
            # `cost_for`, and the agent didn't make it past the parser, so
            # charging the declared cost would be unfair. Fall back to
            # FAILURE_COST_MINUTES.
            return self._failure(call, _format_validation_error(e))

        try:
            declared_cost = op.cost_for(args)
        except Exception as e:
            # `cost_for` itself raised before we ever ran the handler;
            # we don't have a usable declared cost. Fall back.
            return self._failure(call, f"cost computation failed: {e}")

        start_sim_time = self.scheduler.sim_time
        try:
            result = op.handler(self.world, self.scheduler, args, self.caller_id)
        except ToolError as e:
            # Args parsed cleanly, handler ran, business-logic rejection
            # (ACL violation, duplicate id, etc.) — charge the declared cost.
            return self._failure(call, str(e), op=op, args=args)
        except Exception as e:
            # Unexpected handler crash with valid args — still charge the
            # declared cost; the agent did commit to the action.
            return self._failure(call, f"handler error: {e}", op=op, args=args)

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
