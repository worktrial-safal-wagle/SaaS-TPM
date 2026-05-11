"""NPC policy — the intent allow-list.

Each NPC persona carries a `NpcPolicy` (attached to the `Person` via `npc_policy`).
The policy declares the tool ops this NPC may invoke, plus the parameters of
their delay distribution. The runtime gates the brain's proposed tool calls
through `permits()` — anything else is silently dropped (logged).

This is the safety rail that keeps an LLM brain from emitting a `tasks.create`
on behalf of a junior engineer who has no authority to do so.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


DelayKind = Literal["fixed", "lognormal", "uniform"]


class DelaySpec(BaseModel):
    """Per-intent delay distribution.

    Sampled value is in seconds. The runtime multiplies the sample by the
    NPC's `responsiveness` and pushes the result to the next business minute
    (plus jitter) if it would land outside working hours.
    """

    model_config = ConfigDict(extra="forbid")
    kind: DelayKind = "lognormal"
    # lognormal: mean of underlying normal in log-seconds (mu=4.0 ≈ 55s median)
    mu: float = 4.0
    sigma: float = 1.0
    # fixed: value in seconds
    value_seconds: float = 60.0
    # uniform: low/high in seconds
    low_seconds: float = 30.0
    high_seconds: float = 300.0


class NpcPolicy(BaseModel):
    """An NPC's behavior policy. Attach to a `Person` via `npc_policy`."""

    model_config = ConfigDict(extra="forbid")
    # Tool ops this NPC is permitted to invoke. Outputs from the brain
    # naming any other op are dropped by the runtime.
    allowed_tools: list[str] = Field(default_factory=list)
    # Per-intent delay specs; "default" is used when the intent isn't listed.
    delays: dict[str, DelaySpec] = Field(default_factory=lambda: {"default": DelaySpec()})

    def permits(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    def delay_for(self, intent: str) -> DelaySpec:
        return self.delays.get(intent) or self.delays.get("default") or DelaySpec()
