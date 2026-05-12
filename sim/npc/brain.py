"""NPC brain — LLM that turns persona + context + trigger into tool calls.

The brain is the *content* layer. The runtime is the *behavior* layer. The
brain knows nothing about delays, hop caps, or working hours; the runtime
knows nothing about how to phrase a reply.

Brain outputs are cached by `(scenario_id, seed, event_id)`. Re-running a
scenario against the same agent path produces identical NPC behavior — that
gives us replayable execution, not just replayable scoring.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sim.tools.base import ToolCall


class NpcTrigger(BaseModel):
    """A single inbound event observed by an NPC since their last poll.

    The runtime accumulates these between polls. On each poll, the NPC sees
    every trigger that arrived in the interim — they prioritize internally
    and act on at most one. Unaddressed triggers remain in the personal queue
    for next tick (and may be dropped if the queue grows faster than the NPC
    can drain it; that "dropped ball" is intentional, surfaces as eval signal).
    """

    model_config = ConfigDict(extra="forbid")
    # `message_inserted` (DM or @-mention) or `email_inserted`, etc.
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # sim_time the trigger was created. Used for recency ordering.
    sim_time: int = 0


class NpcBrainContext(BaseModel):
    """The shape of context handed to a brain on a poll (or meeting turn).

    Tick-based polling supplies `triggers` — every inbound observed by the
    NPC since their last poll. The brain decides ONE action (or none) based
    on the queue. Meeting turns set `trigger_kind="meeting_turn"` and pass
    the per-turn payload via `trigger_payload`; the `triggers` list is empty.
    """

    model_config = ConfigDict(extra="forbid")
    npc_id: str
    persona_role: str
    persona_notes: str = ""
    knowledge: dict[str, str] = Field(default_factory=dict)
    # All inbound triggers accumulated since the NPC's last poll, ordered
    # most-recent-first. The brain considers them as a queue and acts on
    # (at most) one. Empty list means "no inbound triggered this poll" —
    # the brain may still decide to do something proactive, or stay silent.
    triggers: list[NpcTrigger] = Field(default_factory=list)
    # Legacy single-trigger fields. Still used by meeting-turn invocations
    # (where the runtime supplies a single per-turn payload). Tick-based
    # polls leave these unset and use `triggers` instead.
    trigger_kind: str = ""
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    # Compact view of relevant prior state — channel snippet, task snapshot, etc.
    context_excerpt: str = ""
    # Tools the NPC is authorized to use. Each entry is
    # {"name": "<dot.form>", "description": "...", "input_schema": <jsonschema>}.
    # Passed in so the brain knows exact arg names — without this it
    # hallucinates parameter names (e.g., to_user_id instead of recipient_id)
    # and every NPC tool call gets rejected at dispatch time.
    available_tools: list[dict[str, Any]] = Field(default_factory=list)


class NpcBrainOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # In-meeting speech turn. Set when the brain is invoked with
    # `trigger_kind="meeting_turn"`. None means the NPC stays silent for that turn.
    speech: str | None = None
    # Free-form rationale (for debugging / final-eval state_accuracy axis later).
    rationale: str = ""


class NpcBrain(Protocol):
    def respond(self, context: NpcBrainContext) -> NpcBrainOutput: ...


# ---------------------------------------------------------------------------
# Stub brain — deterministic, for tests and scenario authoring
# ---------------------------------------------------------------------------


class StubBrain:
    """A deterministic brain driven by a user-supplied function.

    Tests can pass `lambda ctx: NpcBrainOutput(tool_calls=[...])` and get
    fully predictable NPC behavior.
    """

    def __init__(self, respond_fn: Callable[[NpcBrainContext], NpcBrainOutput]) -> None:
        self._respond = respond_fn

    def respond(self, context: NpcBrainContext) -> NpcBrainOutput:
        return self._respond(context)


# ---------------------------------------------------------------------------
# Brain cache — content-addressed; ensures replayable execution
# ---------------------------------------------------------------------------


class BrainCache:
    """Caches NPC brain outputs by `(scenario_id, seed, event_id)`.

    Cache miss is the only path that invokes the underlying brain. Identical
    `(scenario_id, seed, event_id)` triples always produce identical outputs
    across runs, even with a stochastic underlying brain.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, str], NpcBrainOutput] = {}
        self.hits: int = 0
        self.misses: int = 0

    def get_or_compute(
        self,
        scenario_id: str,
        seed: int,
        event_id: str,
        compute: Callable[[], NpcBrainOutput],
    ) -> NpcBrainOutput:
        key = (scenario_id, seed, event_id)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]
        self.misses += 1
        value = compute()
        self._cache[key] = value
        return value
