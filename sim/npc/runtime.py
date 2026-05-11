"""NPC runtime — turns world events into delayed, policy-filtered NPC reactions.

Subscribes to `message_inserted` and `email_inserted` (more later). When a
trigger matches an NPC, the runtime:

  1. Samples a base delay from the persona's `delay_spec` (seeded RNG).
  2. Multiplies by the persona's `responsiveness` (junior=2.5, VP=0.3).
  3. Pushes the fire time forward to the next business minute + 0..60 min
     jitter if it would otherwise land outside the NPC's working hours.
  4. Enforces a peer-to-peer hop cap on `(sender, recipient)` pairs to stop
     NPC↔NPC chatter from cascading. Agent-triggered reactions are exempt.
  5. Schedules a `npc_reaction` event with deterministic `event_id`.

When the event fires, the runtime:

  6. Builds an `NpcBrainContext` from the trigger payload + world state.
  7. Looks up the brain output by `(scenario_id, seed, event_id)` (cache).
  8. Filters tool calls through the NPC's policy allow-list.
  9. Dispatches each remaining tool call through the NPC's own ToolRegistry.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Iterable

from sim.scheduler import Event, Scheduler
from sim.store import World
from sim.store.entities import Person
from sim.store.worktime import is_in_hours, next_business_minute
from sim.npc.brain import BrainCache, NpcBrain, NpcBrainContext, NpcBrainOutput
from sim.npc.policy import DelaySpec, NpcPolicy
from sim.tools import ToolCall, ToolRegistry


HOP_CAP_DEFAULT = 4
HOP_WINDOW_MIN_DEFAULT = 60
JITTER_MAX_MIN_DEFAULT = 60
# Every NPC has a minimum "perception lag" — they don't read the message the
# moment it lands. This also keeps NPC reactions from firing inside the
# triggering agent's tool-cost advance window.
PERCEPTION_LAG_MIN_DEFAULT = 1


class NpcRuntime:
    """Owns delay sampling, business-hours gating, hop caps, and brain dispatch."""

    def __init__(
        self,
        world: World,
        scheduler: Scheduler,
        brain: NpcBrain,
        policies: dict[str, NpcPolicy],
        npc_registries: dict[str, ToolRegistry],
        *,
        rng_seed: int = 0,
        brain_cache: BrainCache | None = None,
        hop_cap: int = HOP_CAP_DEFAULT,
        hop_window_min: int = HOP_WINDOW_MIN_DEFAULT,
        jitter_max_min: int = JITTER_MAX_MIN_DEFAULT,
        perception_lag_min: int = PERCEPTION_LAG_MIN_DEFAULT,
    ) -> None:
        self.world = world
        self.scheduler = scheduler
        self.brain = brain
        self.policies = dict(policies)
        self.npc_registries = dict(npc_registries)
        self.brain_cache = brain_cache or BrainCache()

        self._rng = random.Random(rng_seed)
        self._hop_cap = hop_cap
        self._hop_window_min = hop_window_min
        self._jitter_max_min = jitter_max_min
        self._perception_lag_min = perception_lag_min

        # rolling window of (sender, recipient) -> [sim_times of hops]
        self._hops: dict[tuple[str, str], list[int]] = defaultdict(list)

        # Counter for deterministic event_id construction.
        self._reaction_counter: int = 0

        # Drop log — actions silently filtered out, useful for debugging tests.
        self.dropped: list[dict] = []

        # Wire subscriptions
        world.subscribe("message_inserted", self._on_message_inserted)

        # Register the firing handler with the scheduler so other code can
        # also schedule `npc_reaction` events (proactive periodic triggers).
        scheduler.register_handler("npc_reaction", self._fire_npc_reaction)

    # ------------------------------------------------------------------
    # Trigger: a message was inserted somewhere in the world
    # ------------------------------------------------------------------

    def _on_message_inserted(self, event_name: str, payload: dict) -> None:
        sender_id = payload["sender_id"]
        channel_id = payload["channel_id"]
        message_id = payload["message_id"]
        mentions = list(payload.get("mentions", []))
        channel = self.world.get_channel(channel_id)
        if channel is None:
            return

        # Decide which NPCs react. We do NOT trigger reactions for the message
        # sender themselves, nor for the agent (the agent gets its own driver
        # loop in Step 7).
        candidates: set[str] = set()
        if channel.is_dm:
            # DM: the other member reacts.
            candidates.update(m for m in channel.members if m != sender_id)
        candidates.update(mentions)
        candidates.discard(sender_id)

        for npc_id in sorted(candidates):
            if npc_id == self.world.agent_id:
                continue
            if npc_id not in self.policies:
                continue  # not an NPC we manage
            if not self._reserve_hop(sender_id, npc_id):
                self.dropped.append({
                    "reason": "hop_cap",
                    "sender": sender_id, "recipient": npc_id, "message_id": message_id,
                })
                continue
            self._schedule_reaction(npc_id, "message_inserted", payload)

    # ------------------------------------------------------------------
    # Scheduling: sample delay, gate by hours, schedule event
    # ------------------------------------------------------------------

    def _schedule_reaction(self, npc_id: str, trigger_kind: str, trigger_payload: dict) -> None:
        npc = self.world.get_person(npc_id)
        if npc is None:
            return
        policy = self.policies[npc_id]

        delay_seconds = self._sample_delay(policy.delay_for(trigger_kind))
        delay_seconds *= max(0.05, float(npc.responsiveness))
        delay_minutes = max(1, math.ceil(delay_seconds / 60))

        # Perception lag keeps NPC reactions from firing inside the triggering
        # agent's tool-cost advance window — they always land on a *later* tick.
        fire_at = self.scheduler.sim_time + self._perception_lag_min + delay_minutes
        gated = self._gate_to_business_hours(npc, fire_at)
        if gated != fire_at:
            jitter = self._rng.randint(0, self._jitter_max_min)
            fire_at = gated + jitter
        else:
            fire_at = gated

        self._reaction_counter += 1
        event_id_str = f"npc_reaction.{trigger_kind}.{trigger_payload.get('message_id', self._reaction_counter)}.{npc_id}"
        self.scheduler.schedule(
            fire_at=fire_at,
            kind="npc_reaction",
            payload={
                "npc_id": npc_id,
                "trigger_kind": trigger_kind,
                "trigger_payload": trigger_payload,
                "reaction_event_id": event_id_str,
            },
        )

    def _sample_delay(self, spec: DelaySpec) -> float:
        if spec.kind == "fixed":
            return spec.value_seconds
        if spec.kind == "uniform":
            return self._rng.uniform(spec.low_seconds, spec.high_seconds)
        # lognormal
        return float(self._rng.lognormvariate(spec.mu, spec.sigma))

    def _gate_to_business_hours(self, person: Person, sim_time: int) -> int:
        if is_in_hours(self.world.scenario_origin_minute_of_week, sim_time, person):
            return sim_time
        return next_business_minute(self.world.scenario_origin_minute_of_week, sim_time, person)

    # ------------------------------------------------------------------
    # Hop cap (peer-to-peer)
    # ------------------------------------------------------------------

    def _reserve_hop(self, sender_id: str, recipient_id: str) -> bool:
        """Returns True iff the hop is permitted; records it if so."""
        # Agent → NPC hops are unrestricted: the agent is supposed to talk to people.
        if sender_id == self.world.agent_id:
            return True
        pair = (sender_id, recipient_id)
        now = self.scheduler.sim_time
        window = [t for t in self._hops[pair] if now - t <= self._hop_window_min]
        if len(window) >= self._hop_cap:
            self._hops[pair] = window
            return False
        window.append(now)
        self._hops[pair] = window
        return True

    # ------------------------------------------------------------------
    # Firing: build context, call brain, filter, dispatch
    # ------------------------------------------------------------------

    def _fire_npc_reaction(self, event: Event, scheduler: Scheduler) -> None:
        payload = event.payload
        npc_id: str = payload["npc_id"]
        trigger_kind: str = payload["trigger_kind"]
        trigger_payload: dict = payload["trigger_payload"]
        reaction_event_id: str = payload["reaction_event_id"]

        npc = self.world.get_person(npc_id)
        if npc is None:
            return
        policy = self.policies.get(npc_id)
        if policy is None:
            return
        registry = self.npc_registries.get(npc_id)
        if registry is None:
            return

        context = self._build_context(npc, trigger_kind, trigger_payload)
        output = self.brain_cache.get_or_compute(
            self.world.scenario_id, self.world.seed, reaction_event_id,
            lambda: self.brain.respond(context),
        )

        for call in output.tool_calls:
            if not policy.permits(call.tool):
                self.dropped.append({
                    "reason": "policy_denied",
                    "npc_id": npc_id, "tool": call.tool,
                    "reaction_event_id": reaction_event_id,
                })
                continue
            registry.dispatch(call)

    def _build_context(
        self, npc: Person, trigger_kind: str, trigger_payload: dict
    ) -> NpcBrainContext:
        excerpt = self._excerpt_for_trigger(trigger_kind, trigger_payload)
        return NpcBrainContext(
            npc_id=npc.id,
            persona_role=npc.role,
            persona_notes=npc.persona_notes,
            knowledge=dict(npc.knowledge),
            trigger_kind=trigger_kind,
            trigger_payload=trigger_payload,
            context_excerpt=excerpt,
        )

    def _excerpt_for_trigger(self, kind: str, payload: dict) -> str:
        """Compact world view tied to the trigger — passed to the brain."""
        if kind == "message_inserted":
            channel_id = payload.get("channel_id", "")
            messages = self.world.messages_in_channel(channel_id)[-5:]
            lines = [
                f"{m.sender_id}@{m.sim_time}: {m.body}" for m in messages
            ]
            return "\n".join(lines)
        return ""

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def hop_count(self, sender_id: str, recipient_id: str) -> int:
        now = self.scheduler.sim_time
        return sum(
            1
            for t in self._hops.get((sender_id, recipient_id), [])
            if now - t <= self._hop_window_min
        )
