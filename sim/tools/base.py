"""Tool protocol and registry primitives.

A tool *op* is one named operation under a tool namespace, e.g. `chat.send`.
Every op declares:
  - its pydantic args model (validation + schema)
  - its sim-time cost (int minutes, or a function of parsed args)
  - its handler (takes World + Scheduler + parsed-args + caller_id, returns a
    JSON-serializable result)

The registry handles validation, cost accounting, scheduler advance, and
notification delivery. Handlers focus on the mutation alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from sim.scheduler import Scheduler
from sim.store import Notification, World


class ToolCall(BaseModel):
    """A single agent decision: which tool, with which args."""

    model_config = ConfigDict(extra="forbid")
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """A single tool dispatch outcome.

    `notifications` contains *only* notifications the caller hasn't seen on a
    previous tool dispatch. The registry maintains a per-caller cursor.
    """

    model_config = ConfigDict(extra="forbid")
    ok: bool
    tool: str
    sim_time: int
    cost_minutes: int
    result: Any = None
    notifications: list[Notification] = Field(default_factory=list)
    error: str | None = None


# Handler signature: (world, scheduler, parsed_args, caller_id) -> serializable result
Handler = Callable[[World, Scheduler, Any, str], Any]
# Cost may be a plain int or a function of parsed args.
CostSpec = int | Callable[[Any], int]


@dataclass
class ToolOp:
    name: str  # e.g. "chat.send"
    args_model: type[BaseModel]
    cost: CostSpec
    handler: Handler
    description: str = ""

    def cost_for(self, args: BaseModel) -> int:
        if callable(self.cost):
            return int(self.cost(args))
        return int(self.cost)

    def to_spec(self) -> dict[str, Any]:
        """Anthropic SDK tool spec. Tool name uses `__` instead of `.` because
        the Anthropic API restricts tool names to `[a-zA-Z0-9_-]`. The registry
        translates back on dispatch."""
        return {
            "name": sdk_name(self.name),
            "description": self.description or f"Tool: {self.name}",
            "input_schema": self.args_model.model_json_schema(),
        }


def sdk_name(tool_name: str) -> str:
    return tool_name.replace(".", "__")


def from_sdk_name(name: str) -> str:
    return name.replace("__", ".")
