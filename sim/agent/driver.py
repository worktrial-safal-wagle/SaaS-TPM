"""Agent driver — the perceive-decide-act loop.

The driver doesn't decide *what* the agent does — that's the Agent's job.
The driver decides *when* the agent runs and *what context it sees*.

Loop semantics:
  - while turn_count < max_turns and sim_time < end_sim_time:
      build briefing
      call agent.decide(briefing)
      dispatch the tool call
      record the turn
  - The agent uses `wait.until` / `wait.for_next_event` to skip dead time;
    the driver itself never sleeps the clock implicitly.

One tool call per turn. The driver does not accept multi-call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import json as _json

from sim.agent.briefing import Briefing, BriefingAssembler, BriefingRecentAction
from sim.scheduler import Scheduler
from sim.store import World
from sim.tools import ToolCall, ToolRegistry, ToolResult


class Agent(Protocol):
    """An agent implementation. The driver calls `decide` once per turn."""

    def decide(self, briefing: Briefing) -> ToolCall: ...


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class TurnRecord:
    turn: int
    sim_time_before: int
    sim_time_after: int
    briefing: Briefing
    call: ToolCall
    result: ToolResult


@dataclass
class DriverConfig:
    max_turns: int = 500
    end_sim_time: int = 7200
    halt_on_error: bool = False


class AgentDriver:
    def __init__(
        self,
        world: World,
        scheduler: Scheduler,
        registry: ToolRegistry,
        agent: Agent,
        briefing_assembler: BriefingAssembler,
        config: DriverConfig | None = None,
        turn_observer: Callable[[TurnRecord], None] | None = None,
        feedback_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.world = world
        self.scheduler = scheduler
        self.registry = registry
        self.agent = agent
        self.briefing_assembler = briefing_assembler
        self.config = config or DriverConfig()
        self.turn_observer = turn_observer
        self.feedback_provider = feedback_provider
        self.turns: list[TurnRecord] = []
        self.last_turn_sim_time: int = -1

    def run(self) -> list[TurnRecord]:
        while True:
            if len(self.turns) >= self.config.max_turns:
                break
            if self.scheduler.sim_time >= self.config.end_sim_time:
                break
            verdict = self.feedback_provider() if self.feedback_provider else None
            recent = [
                BriefingRecentAction(
                    turn=r.turn, sim_time=r.sim_time_after,
                    tool=r.call.tool,
                    args_summary=_summarise_args(r.call.args),
                    ok=r.result.ok, error=r.result.error,
                    result_summary=_summarise_result(r.result.result),
                )
                for r in self.turns[-10:]
            ]
            stalled = _stalled_for_turns(self.turns, self.scheduler.sim_time)
            briefing = self.briefing_assembler.build(
                now_sim_time=self.scheduler.sim_time,
                last_turn_sim_time=self.last_turn_sim_time,
                last_verdict=verdict,
                tool_names=self.registry.list_tools(),
                recent_actions=recent,
                sim_time_stalled_for_turns=stalled,
            )
            sim_before = self.scheduler.sim_time
            try:
                call = self.agent.decide(briefing)
            except StopIteration:
                break
            result = self.registry.dispatch(call)
            sim_after = self.scheduler.sim_time
            record = TurnRecord(
                turn=len(self.turns), sim_time_before=sim_before,
                sim_time_after=sim_after, briefing=briefing,
                call=call, result=result,
            )
            self.turns.append(record)
            self.last_turn_sim_time = sim_after
            if self.turn_observer:
                self.turn_observer(record)
            if not result.ok and self.config.halt_on_error:
                break
        return self.turns


# ---------------------------------------------------------------------------
# Reference scripted agent — deterministic, used in tests
# ---------------------------------------------------------------------------


def _stalled_for_turns(turns: list["TurnRecord"], now_sim_time: int) -> int:
    """How many consecutive most-recent turns advanced sim_time by zero.

    If the agent has been spinning on free reads or erroring tool calls,
    sim_time delta stays zero. The driver surfaces this in the next briefing
    so the agent can self-correct. Resets to zero immediately after any
    turn that costs sim-time.
    """
    count = 0
    for r in reversed(turns):
        if r.sim_time_after == r.sim_time_before:
            count += 1
        else:
            break
    return count


def _summarise_args(args: dict) -> str:
    s = _json.dumps(args, default=str)
    return s if len(s) <= 120 else s[:117] + "..."


def _summarise_result(result: Any) -> str | None:
    """Short, readable summary of a tool result for the recent_actions
    section of the next briefing. Keep this terse — the agent has the
    full result in conversational memory if needed.

    Truncates at ~300 chars. Highlights key list-count fields when present.
    """
    if result is None:
        return None
    try:
        if isinstance(result, dict):
            # Common shapes: {channels: [...]}, {messages: [...]}, {tasks: [...]}
            for key in ("channels", "messages", "emails", "tasks", "people", "events", "docs", "notifications"):
                if key in result and isinstance(result[key], list):
                    n = len(result[key])
                    sample = result[key][:3]
                    sample_repr = _json.dumps(sample, default=str)
                    if len(sample_repr) > 260:
                        sample_repr = sample_repr[:257] + "..."
                    return f"{n} {key}; sample={sample_repr}"
        s = _json.dumps(result, default=str)
        return s if len(s) <= 300 else s[:297] + "..."
    except Exception:
        return None


class ScriptedAgent:
    """An agent that emits a pre-determined sequence of tool calls.

    When the script is exhausted, defaults to `wait.until(end_sim_time)`.
    """

    def __init__(self, calls: Iterable[ToolCall], default: ToolCall | None = None) -> None:
        self._queue = list(calls)
        self._default = default

    def decide(self, briefing: Briefing) -> ToolCall:
        if self._queue:
            return self._queue.pop(0)
        if self._default is not None:
            return self._default
        return ToolCall(
            tool="wait.until",
            args={"target_sim_time": briefing.end_sim_time},
        )
