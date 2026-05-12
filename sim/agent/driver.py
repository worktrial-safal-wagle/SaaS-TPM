"""Agent driver — the tick-driven perceive-decide-act loop.

Replaces the older action-driven loop. The agent is polled when an
`actor_poll` event fires for them; on each poll the agent gets up to 4
chained decisions (default 1, agent must opt-in to chain via
`continue_in_tick=True`). When the tick ends, the next `actor_poll` is
scheduled at `max(sim_time + tick_size, busy_until)` so long-running
actions naturally push the next poll past the tick boundary.

Key invariants:
  - sim_time only advances through tool dispatches and event drain.
  - `actor_poll` events are the gate for agent decisions; calendar events,
    NPC reactions, and heartbeats fire as part of the scheduler's event
    pipeline but do not themselves invoke the agent.
  - `idle.until` and `abandon.current` reschedule actor_poll directly via
    side effects on the person record + a fresh event push. The driver
    dedupes stale polls by comparing `event.fire_at` to
    `person.next_poll_at` when the poll fires.
  - One TurnRecord per tool dispatch (preserves observers and the run log).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

import json as _json

from sim.agent.briefing import Briefing, BriefingAssembler, BriefingRecentAction
from sim.scheduler import EVENT_KIND_ACTOR_POLL, Event, Scheduler, schedule_actor_poll
from sim.store import World
from sim.tools import ToolCall, ToolRegistry, ToolResult


class Agent(Protocol):
    """An agent implementation. The driver calls `decide` once per agent
    decision; the agent may return `ToolCall(..., continue_in_tick=True)`
    to chain another decision within the same tick (subject to the cap)."""

    def decide(self, briefing: Briefing) -> ToolCall: ...


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


# Hard cap on decisions per tick. Without this, an agent could opt into
# unbounded chained decisions and starve the rest of the simulation. 4
# matches the design doc.
MAX_DECISIONS_PER_TICK = 4


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
        tick_size_minutes: int | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.world = world
        self.scheduler = scheduler
        self.registry = registry
        self.agent = agent
        self.briefing_assembler = briefing_assembler
        self.config = config or DriverConfig()
        self.turn_observer = turn_observer
        self.feedback_provider = feedback_provider
        # `tick_size_minutes` defaults to 15 — the design-locked default.
        # The scenario-level value is plumbed in via `build_runtime`'s
        # scheduling of the initial actor_poll, but the driver itself also
        # needs the value to schedule subsequent polls. Callers (CLI,
        # tests) can pass it explicitly; if absent, fall back to 15 so
        # legacy test fixtures still work.
        self.tick_size_minutes = tick_size_minutes if tick_size_minutes is not None else 15
        # `agent_id` is the actor whose polls drive this loop. Falls back
        # to `world.agent_id` (set during scenario load when a persona has
        # `is_agent: true`).
        self.agent_id = agent_id or world.agent_id
        assert self.agent_id is not None, (
            "AgentDriver requires an agent_id, either passed explicitly "
            "or set on the world via a persona with is_agent=true."
        )
        self.turns: list[TurnRecord] = []
        self.last_turn_sim_time: int = -1
        # Tracks the sim_time of the last actor_poll we processed. Used to
        # dedupe out-of-date polls left in the heap by `idle.until` after
        # an early reschedule.
        self.last_processed_poll_at: int = -1
        # Tracks whether we've scheduled the initial actor_poll. Tests
        # that bypass `build_runtime` and construct an `AgentDriver`
        # directly can rely on the driver to schedule the first poll
        # itself on `run()`.
        self._initial_poll_scheduled: bool = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _ensure_initial_poll(self) -> None:
        """Make sure the heap contains at least one actor_poll for our agent.

        `build_runtime` already does this for production paths. Tests that
        construct AgentDriver directly might not have called the helper —
        we cover that here so the loop has something to fire on.
        """
        person = self.world.get_person(self.agent_id)
        if person is None:
            raise RuntimeError(
                f"agent_id {self.agent_id!r} not found in world.people"
            )
        # If any actor_poll for our agent is already pending, do nothing.
        for evt in self.scheduler.pending():
            if evt.kind == EVENT_KIND_ACTOR_POLL and evt.payload.get("actor_id") == self.agent_id:
                return
        schedule_actor_poll(self.scheduler, self.world, self.agent_id, self.tick_size_minutes)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> list[TurnRecord]:
        if not self._initial_poll_scheduled:
            self._ensure_initial_poll()
            self._initial_poll_scheduled = True

        while True:
            if len(self.turns) >= self.config.max_turns:
                break
            if self.scheduler.sim_time >= self.config.end_sim_time:
                break

            # Drain the heap until we either find our agent's poll, run
            # out of events, or cross end_sim_time. Non-poll events
            # (NPC reactions, calendar starts, heartbeats) fire as part of
            # `advance_to` — we never need to handle them in the driver.
            poll_event = self._wait_for_next_agent_poll()
            if poll_event is None:
                # No more polls within end_sim_time. Advance the clock to
                # end_sim_time so the requires_full_run tier gate fires
                # correctly. Without this, an agent that idled to the end
                # of the week leaves sim_time short of end_sim_time and the
                # eval drops to slice_safe tier — losing deadline_hit_rate
                # and stakeholder_contact_rate from the scorecard.
                if self.scheduler.sim_time < self.config.end_sim_time:
                    self.scheduler.advance_to(self.config.end_sim_time)
                break

            # Stale poll: a tool (idle.until, abandon) rescheduled this
            # actor's next poll to a later sim_time and we're seeing a
            # leftover heap entry. Skip it.
            person = self.world.get_person(self.agent_id)
            assert person is not None
            if poll_event.fire_at < person.next_poll_at:
                # An earlier idle.until requested a later poll; drop this
                # stale event and keep waiting for the right one.
                continue
            # Also skip duplicate polls at the same fire_at (idle.until
            # schedules a fresh event without removing the original; if
            # both happen to share the same fire_at we still want to fire
            # only once).
            if poll_event.fire_at == self.last_processed_poll_at:
                continue
            self.last_processed_poll_at = poll_event.fire_at

            # Snapshot the actor's planned next-poll *before* the tick.
            # Tools like `idle.until` mutate `person.next_poll_at` during
            # the tick to defer the next poll; we use the pre-tick value
            # as our baseline so we can tell whether the agent explicitly
            # rescheduled itself.
            pre_tick_next_poll_at = person.next_poll_at

            # Run the tick — up to MAX_DECISIONS_PER_TICK chained decisions.
            self._run_tick()

            # End-of-tick bookkeeping: schedule the next poll.
            #
            # Two cases:
            #   1) Agent invoked `idle.until` during the tick: the tool
            #      already updated `person.next_poll_at` to the requested
            #      target and scheduled the event. Respect that — do
            #      nothing more so we don't overwrite the agent's choice.
            #   2) Default cadence: next poll lands at
            #      max(sim_time + tick_size, busy_until). This is exactly
            #      `schedule_actor_poll`'s formula. Long actions that
            #      pushed `busy_until` past the tick boundary get their
            #      next poll naturally deferred (action straddling).
            current = self.scheduler.sim_time
            if person.next_poll_at != pre_tick_next_poll_at:
                # The agent (via idle.until) explicitly set the next poll.
                # The tool itself scheduled the event; nothing to do.
                continue
            actual_next = max(current + self.tick_size_minutes, person.busy_until)
            person.next_poll_at = actual_next
            schedule_actor_poll(
                self.scheduler, self.world, self.agent_id, self.tick_size_minutes,
            )

        return self.turns

    # ------------------------------------------------------------------
    # Per-tick logic
    # ------------------------------------------------------------------

    def _run_tick(self) -> None:
        """Drive one tick: up to MAX_DECISIONS_PER_TICK chained decisions."""
        for _ in range(MAX_DECISIONS_PER_TICK):
            if len(self.turns) >= self.config.max_turns:
                return
            if self.scheduler.sim_time >= self.config.end_sim_time:
                return
            briefing = self._build_briefing()
            sim_before = self.scheduler.sim_time
            try:
                call = self.agent.decide(briefing)
            except StopIteration:
                return
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
                return
            # Honor the agent's opt-in continuation flag. Default False —
            # tick ends after this dispatch.
            if not call.continue_in_tick:
                return

    # ------------------------------------------------------------------
    # Scheduler interaction
    # ------------------------------------------------------------------

    def _wait_for_next_agent_poll(self) -> Event | None:
        """Drain the heap until our actor's `actor_poll` fires, or no
        agent-poll remains in the heap, or we cross `end_sim_time`.

        Returns the agent's actor_poll Event when it fires, or None if no
        such event will fire in the remaining sim window.

        Non-poll events (NPC reactions, calendar starts, heartbeats) drain
        naturally via `advance_to` — they have their own handlers
        registered with the scheduler.
        """
        end = self.config.end_sim_time
        captured: list[Event] = []

        def _capture(event: "Event", _sch: Scheduler) -> None:
            captured.append(event)

        # Register an inline handler so we observe every actor_poll for
        # this agent as it fires. Other actor_poll events (future: NPCs
        # with their own tick) are not intercepted unless they share the
        # same kind handler — we filter on `actor_id` below.
        prior = self.scheduler._fallback_handlers.get(EVENT_KIND_ACTOR_POLL)
        self.scheduler.register_handler(EVENT_KIND_ACTOR_POLL, _capture)
        try:
            while True:
                next_at = self.scheduler.peek_next_fire_at()
                if next_at is None:
                    # No events at all — there will never be a poll. Time
                    # to terminate the loop.
                    return None
                target = min(next_at, end)
                self.scheduler.advance_to(target)
                # Drain captured polls in order; return the first that
                # belongs to this agent.
                for evt in captured:
                    if evt.payload.get("actor_id") == self.agent_id:
                        captured.clear()
                        return evt
                captured.clear()
                if self.scheduler.sim_time >= end:
                    return None
        finally:
            # Restore any prior fallback handler for actor_poll. Production
            # code doesn't register one (Phase 4 may; we leave room).
            if prior is None:
                self.scheduler._fallback_handlers.pop(EVENT_KIND_ACTOR_POLL, None)
            else:
                self.scheduler.register_handler(EVENT_KIND_ACTOR_POLL, prior)

    # ------------------------------------------------------------------
    # Briefing
    # ------------------------------------------------------------------

    def _build_briefing(self) -> Briefing:
        """Build the next briefing for the agent.

        Phase 5 will replace this with a delta-first variant; for now we
        delegate to the existing `BriefingAssembler` so the tick rewrite
        is decoupled from the briefing rewrite.
        """
        verdict = self.feedback_provider() if self.feedback_provider else None
        recent = [
            BriefingRecentAction(
                turn=r.turn, sim_time=r.sim_time_after,
                tool=r.call.tool,
                args_summary=_summarise_args(r.call.args),
                ok=r.result.ok, error=r.result.error,
                result_summary=_summarise_result(r.result.result),
                # Preserve full content for read tools so the agent doesn't
                # re-fetch what it just fetched. Briefing renderer picks the
                # last N reads to show full; older reads use the summary.
                result_full=_full_result_content(r.call.tool, r.result.result),
            )
            for r in self.turns[-10:]
        ]
        stalled = _stalled_for_turns(self.turns, self.scheduler.sim_time)
        return self.briefing_assembler.build(
            now_sim_time=self.scheduler.sim_time,
            last_turn_sim_time=self.last_turn_sim_time,
            last_verdict=verdict,
            tool_names=self.registry.list_tools(),
            recent_actions=recent,
            sim_time_stalled_for_turns=stalled,
        )


# ---------------------------------------------------------------------------
# Helpers (shared with the briefing renderer)
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


_READ_TOOLS_FOR_FULL_PRESERVATION = {
    "chat.read", "email.read", "docs.read", "tasks.get",
    "calendar.get", "directory.get", "meetings.get_transcript",
}


def _full_result_content(tool: str, result: Any) -> str | None:
    """Full result body, preserved for read tools so the briefing can present
    them without re-fetching. Capped at 2000 chars to keep briefing size
    bounded. Returns None for non-read tools — there's nothing to recall."""
    if tool not in _READ_TOOLS_FOR_FULL_PRESERVATION or result is None:
        return None
    try:
        s = _json.dumps(result, default=str, indent=2)
    except Exception:
        return None
    return s if len(s) <= 2000 else s[:1997] + "..."


def _summarise_result(result: Any) -> str | None:
    """Short, readable summary of a tool result for the recent_actions
    section of the next briefing. Kept terse for tools whose result the
    agent doesn't need to recall — for read tools we ALSO preserve the
    full content via `_full_result_content` so the briefing renderer
    can show whichever is appropriate based on recency.

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


# ---------------------------------------------------------------------------
# Reference scripted agent — deterministic, used in tests
# ---------------------------------------------------------------------------


class ScriptedAgent:
    """An agent that emits a pre-determined sequence of tool calls.

    When the script is exhausted, defaults to `wait.until(end_sim_time)`
    (the simplest "end the run" action). Tests that want the agent to keep
    polling indefinitely can pass `default=None` and rely on max_turns or
    end_sim_time to terminate.
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
