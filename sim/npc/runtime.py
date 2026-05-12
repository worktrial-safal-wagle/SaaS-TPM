"""NPC runtime — tick-based polling that drives policy-filtered NPC behavior.

Each NPC ticks on a *personal cadence*. Inbound events (DMs, mentions, emails)
are accumulated into a per-NPC queue between polls. On each `actor_poll`
event, the runtime:

  1. Drains the NPC's queue of triggers that arrived since their last poll.
  2. Builds an `NpcBrainContext` carrying ALL accumulated triggers,
     most-recent-first (the default "priority" — see below).
  3. Asks the brain for ONE action (or none).
  4. Filters the chosen tool calls through the NPC's policy allow-list.
  5. Dispatches the action via the NPC's own ToolRegistry.
  6. Re-schedules the NPC's next poll at
     `max(sim_time + tick_size, npc.busy_until)` with an explicit
     per-event handler (mirrors `schedule_actor_poll`'s formula but
     attaches the handler so the event routes correctly even when the
     agent driver swaps the scheduler's fallback handler for the same
     kind).

Priority rule for the accumulated triggers: most-recent-first by sim_time.
The brain sees a queue; it decides which one to act on (typically the
freshest, but the brain has full context to override). When the queue
grows faster than the NPC can drain it (long meetings, deep backlog,
single-decision-per-tick semantics), unaddressed triggers naturally fall
off — that's the "dropped balls" property and it's intentional.

The old reaction-scheduled model (`_schedule_reaction`, `_push_past_meetings`,
`npc_reaction` events, sampled delays) is gone. Calendar-aware busyness is
managed via `busy_until` on `Person`, updated by the runtime's
`calendar_event_start` handler.

Hop-cap is preserved as a defense against runaway NPC↔NPC chatter: a
trigger that originates from another NPC and would put a pair past the
per-window cap is recorded as a drop instead of enqueued.
"""

from __future__ import annotations

from collections import defaultdict

from sim.scheduler import (
    EVENT_KIND_ACTOR_POLL,
    Event,
    Scheduler,
)
from sim.store import World
from sim.store.entities import Person
from sim.npc.brain import (
    BrainCache,
    NpcBrain,
    NpcBrainContext,
    NpcBrainOutput,
    NpcTrigger,
)
from sim.npc.policy import NpcPolicy
from sim.tools import ToolRegistry


HOP_CAP_DEFAULT = 4
HOP_WINDOW_MIN_DEFAULT = 60
DEFAULT_TICK_SIZE_MINUTES = 15


class NpcRuntime:
    """Owns NPC trigger queues, polling cadence, and brain dispatch."""

    def __init__(
        self,
        world: World,
        scheduler: Scheduler,
        brain: NpcBrain,
        policies: dict[str, NpcPolicy],
        npc_registries: dict[str, ToolRegistry],
        *,
        tick_size_minutes: int = DEFAULT_TICK_SIZE_MINUTES,
        rng_seed: int = 0,
        brain_cache: BrainCache | None = None,
        hop_cap: int = HOP_CAP_DEFAULT,
        hop_window_min: int = HOP_WINDOW_MIN_DEFAULT,
        schedule_initial_polls: bool = True,
    ) -> None:
        self.world = world
        self.scheduler = scheduler
        self.brain = brain
        self.policies = dict(policies)
        self.npc_registries = dict(npc_registries)
        self.brain_cache = brain_cache or BrainCache()

        self._tick_size_minutes = max(1, tick_size_minutes)
        # rng_seed retained for forward-compat (LLM brains may use it via
        # the scenario seed in the cache key). No internal RNG is needed
        # in the tick-poll model — delays come from the global tick cadence,
        # not per-action sampling.
        self._rng_seed = rng_seed

        self._hop_cap = hop_cap
        self._hop_window_min = hop_window_min

        # Per-NPC trigger queue. Append on inbound; drain on poll.
        # Ordered by arrival sim_time (oldest first); the runtime presents
        # the queue most-recent-first to the brain at poll time.
        self._queues: dict[str, list[NpcTrigger]] = defaultdict(list)

        # rolling window of (sender, recipient) -> [sim_times of hops]
        self._hops: dict[tuple[str, str], list[int]] = defaultdict(list)

        # Counter for poll event ids (deterministic across re-runs because
        # the underlying `schedule_actor_poll` event_id is sim_time-based).
        self._poll_counter: int = 0

        # Drop log — actions silently filtered out, useful for debugging tests.
        self.dropped: list[dict] = []

        # Visibility into NPC brain health. Any brain output whose rationale
        # is prefixed with `brain_` and has no real content (no tool calls,
        # no speech) is a failure — API error after retries, missing tool_use
        # block, etc. Operators need to see this at the end of a run rather
        # than silently get back the "NPCs were inert" failure mode.
        self.brain_failures: int = 0
        self.brain_successes: int = 0
        # Counter for NPC ticks that were skipped because the trigger queue
        # was empty (perf optimization — see _fire_actor_poll).
        self.empty_queue_skips: int = 0

        # Wire subscriptions: messages and emails feed per-NPC queues.
        world.subscribe("message_inserted", self._on_message_inserted)
        world.subscribe("email_inserted", self._on_email_inserted)

        # NOTE: actor_poll events scheduled by this runtime carry an
        # explicit per-event handler (`self._fire_actor_poll`) so they
        # route correctly even when other code (e.g., the agent driver)
        # swaps the scheduler's fallback handler for the same kind.

        if schedule_initial_polls:
            self._schedule_initial_polls()

    # ------------------------------------------------------------------
    # Subscriptions: accumulate triggers
    # ------------------------------------------------------------------

    def _on_message_inserted(self, event_name: str, payload: dict) -> None:
        """Route message_inserted into the queues of NPCs who'd care.

        Cares = a DM recipient (the other member) or a @-mention target.
        The hop-cap still applies: NPC↔NPC echoes that exceed the cap in
        the rolling window are recorded as drops instead of enqueued.
        Agent-origin messages are exempt from the cap.
        """
        sender_id = payload["sender_id"]
        channel_id = payload["channel_id"]
        message_id = payload["message_id"]
        sim_time = payload.get("sim_time", self.scheduler.sim_time)
        mentions = list(payload.get("mentions", []))
        channel = self.world.get_channel(channel_id)
        if channel is None:
            return

        candidates: set[str] = set()
        if channel.is_dm:
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
                    "sender": sender_id, "recipient": npc_id,
                    "message_id": message_id,
                })
                continue
            self._queues[npc_id].append(NpcTrigger(
                kind="message_inserted",
                payload=dict(payload),
                sim_time=sim_time,
            ))

    def _on_email_inserted(self, event_name: str, payload: dict) -> None:
        """Route email_inserted to to/cc recipients who are NPCs we manage."""
        sender_id = payload["sender_id"]
        email_id = payload["email_id"]
        sim_time = payload.get("sim_time", self.scheduler.sim_time)
        recipients = set(payload.get("to", [])) | set(payload.get("cc", []))
        recipients.discard(sender_id)

        for npc_id in sorted(recipients):
            if npc_id == self.world.agent_id:
                continue
            if npc_id not in self.policies:
                continue
            if not self._reserve_hop(sender_id, npc_id):
                self.dropped.append({
                    "reason": "hop_cap",
                    "sender": sender_id, "recipient": npc_id,
                    "email_id": email_id,
                })
                continue
            self._queues[npc_id].append(NpcTrigger(
                kind="email_inserted",
                payload=dict(payload),
                sim_time=sim_time,
            ))

    # ------------------------------------------------------------------
    # Hop cap (peer-to-peer)
    # ------------------------------------------------------------------

    def _reserve_hop(self, sender_id: str, recipient_id: str) -> bool:
        """Returns True iff the hop is permitted; records it if so.

        Agent → NPC hops are unrestricted — the agent is supposed to talk
        to people. NPC↔NPC hops are capped per rolling window to prevent
        runaway echo cascades.
        """
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
    # Poll scheduling
    # ------------------------------------------------------------------

    def _schedule_initial_polls(self) -> None:
        """Schedule the first poll for every managed NPC.

        Default is sim_time 0 + tick_size, *or* the NPC's current
        `busy_until` if it's later (someone with a calendar event at t=0
        won't poll until it ends).
        """
        for npc_id in sorted(self.policies):
            if npc_id == self.world.agent_id:
                continue
            if self.world.get_person(npc_id) is None:
                continue
            self._schedule_next_poll(npc_id)

    def _schedule_next_poll(self, npc_id: str) -> None:
        """Schedule this NPC's next poll with an explicit per-event handler.

        Mirrors `schedule_actor_poll` (fire_at = max(sim_time + tick, busy))
        but attaches `self._fire_actor_poll` as the handler so the event
        routes to the NPC runtime *regardless* of which fallback handler
        is currently registered for `actor_poll`. The agent driver
        temporarily swaps the fallback handler while waiting for its own
        polls; per-event handlers always win, so NPC polls still fire.
        """
        npc = self.world.get_person(npc_id)
        if npc is None:
            return
        now = self.scheduler.sim_time
        fire_at = max(now + self._tick_size_minutes, npc.busy_until)
        event_id_str = f"actor_poll.{npc_id}.{fire_at}"
        self.scheduler.schedule(
            fire_at=fire_at,
            kind=EVENT_KIND_ACTOR_POLL,
            payload={"actor_id": npc_id, "event_id": event_id_str},
            handler=self._fire_actor_poll,
        )
        npc.next_poll_at = fire_at
        self._poll_counter += 1

    # ------------------------------------------------------------------
    # Calendar busyness: NPC attendees are unavailable for the meeting
    # ------------------------------------------------------------------

    def on_calendar_event_start(self, event: Event, scheduler: Scheduler) -> None:
        """When a calendar event starts, mark NPC attendees busy_until end.

        This replaces the old `_push_past_meetings` deferred-react logic:
        with busy_until set, `schedule_actor_poll` will naturally push the
        NPC's next poll past meeting end. The personal-cadence helper does
        the math; we just set the busy_until floor.
        """
        event_id = event.payload.get("event_id")
        if event_id is None:
            return
        cal_event = self.world.calendar.get(event_id)
        if cal_event is None:
            return
        for attendee_id in cal_event.attendees:
            if attendee_id == self.world.agent_id:
                continue
            if attendee_id not in self.policies:
                continue
            person = self.world.get_person(attendee_id)
            if person is None:
                continue
            person.busy_until = max(person.busy_until, cal_event.end_sim_time)

    # ------------------------------------------------------------------
    # Polling: drain queue, run brain, dispatch one action, re-schedule
    # ------------------------------------------------------------------

    def _fire_actor_poll(self, event: Event, scheduler: Scheduler) -> None:
        payload = event.payload
        npc_id: str = payload.get("actor_id", "")
        event_id_str: str = payload.get("event_id", f"actor_poll.{npc_id}.{event.fire_at}")

        if npc_id == self.world.agent_id:
            return  # agent polls handled by Phase 3, not us
        if npc_id not in self.policies:
            return
        npc = self.world.get_person(npc_id)
        if npc is None:
            return
        # If the NPC is still inside a meeting (busy_until > now), skip the
        # decision and reschedule. This handles the case where a poll was
        # already scheduled before a calendar event extended busy_until.
        if npc.busy_until > scheduler.sim_time:
            self._schedule_next_poll(npc_id)
            return

        registry = self.npc_registries.get(npc_id)
        if registry is None:
            self._schedule_next_poll(npc_id)
            return
        policy = self.policies[npc_id]

        # Peek at the NPC's accumulated triggers (don't pop yet — we may
        # need to put unaddressed ones back).
        queued = list(self._queues.get(npc_id, []))
        # Present to the brain most-recent-first — the default priority.
        triggers = sorted(queued, key=lambda t: t.sim_time, reverse=True)

        # Empty-queue short-circuit: if the NPC has no accumulated triggers,
        # they have nothing to react to. Skip the (expensive) brain call and
        # just reschedule. This is an architectural perf optimization: in a
        # quiet stretch of a long scenario, 9 NPCs polling every 15 sim-min
        # was generating ~4300 unnecessary LLM calls per run, dominating
        # wall-clock. NPCs in our scenarios are reactive — they don't need
        # to be polled when there's nothing to react to.
        if not triggers:
            self.empty_queue_skips += 1
            self._schedule_next_poll(npc_id)
            return

        context = self._build_context(npc, triggers)
        output: NpcBrainOutput = self.brain_cache.get_or_compute(
            self.world.scenario_id, self.world.seed, event_id_str,
            lambda: self.brain.respond(context),
        )

        # Brain health: a `brain_`-prefixed rationale with no action is a
        # failed call; surface counts for end-of-run diagnostics.
        is_failure = (
            output.rationale.startswith("brain_")
            and not output.tool_calls
            and output.speech is None
        )
        if is_failure:
            self.brain_failures += 1
        else:
            self.brain_successes += 1

        # Single decision per tick: dispatch up to ONE tool call. If the
        # brain returns multiple, only the first allowed-by-policy call
        # runs; subsequent calls are dropped (recorded for visibility).
        # The "one action per NPC per tick" rule is part of the locked
        # design: NPCs don't get opt-in continuation.
        dispatched = False
        for call in output.tool_calls:
            if not policy.permits(call.tool):
                self.dropped.append({
                    "reason": "policy_denied",
                    "npc_id": npc_id, "tool": call.tool,
                    "poll_event_id": event_id_str,
                })
                continue
            if dispatched:
                self.dropped.append({
                    "reason": "single_decision_per_tick",
                    "npc_id": npc_id, "tool": call.tool,
                    "poll_event_id": event_id_str,
                })
                continue
            registry.dispatch(call)
            dispatched = True

        # Trigger consumption: if the brain took an action, consume the
        # highest-priority (most-recent) trigger — that's the one it
        # "responded to." Remaining triggers stay queued for next tick.
        # If the brain stayed silent (no dispatch), no triggers are
        # consumed and the whole backlog re-presents next tick.
        if dispatched and triggers:
            # Find and remove the head trigger from the underlying queue
            # by identity. `triggers[0]` is the most-recent (post-sort);
            # match it by (kind, payload identity / message_id, sim_time).
            head = triggers[0]
            self._consume_trigger(npc_id, head)

        # Re-schedule the next poll. `_schedule_next_poll` reads
        # `busy_until` from the person — if a long action just bumped it,
        # the next poll lands appropriately.
        self._schedule_next_poll(npc_id)

    def _consume_trigger(self, npc_id: str, head: NpcTrigger) -> None:
        """Remove `head` from the NPC's queue. Idempotent if not present."""
        queue = self._queues.get(npc_id)
        if not queue:
            return
        for idx, t in enumerate(queue):
            if (t.kind == head.kind and t.sim_time == head.sim_time
                    and t.payload == head.payload):
                queue.pop(idx)
                return

    # ------------------------------------------------------------------
    # Brain context assembly
    # ------------------------------------------------------------------

    def _build_context(self, npc: Person, triggers: list[NpcTrigger]) -> NpcBrainContext:
        excerpt = self._excerpt_for_triggers(triggers)
        registry = self.npc_registries.get(npc.id)
        available_tools: list[dict] = []
        if registry is not None:
            for spec in registry.tool_specs():
                dotted = spec.get("name", "").replace("__", ".")
                available_tools.append({
                    "name": dotted,
                    "description": spec.get("description", ""),
                    "input_schema": spec.get("input_schema", {}),
                })
        # Legacy single-trigger fields stay empty for tick polls; downstream
        # consumers (like the Anthropic brain prompt) prefer `triggers` when
        # populated.
        return NpcBrainContext(
            npc_id=npc.id,
            persona_role=npc.role,
            persona_notes=npc.persona_notes,
            knowledge=dict(npc.knowledge),
            triggers=triggers,
            trigger_kind="",
            trigger_payload={},
            context_excerpt=excerpt,
            available_tools=available_tools,
        )

    def _excerpt_for_triggers(self, triggers: list[NpcTrigger]) -> str:
        """Compact world view tied to the (newest) chat trigger.

        Picks the most recent message-style trigger and renders the last 5
        messages in that channel. Older triggers do not contribute excerpt
        content (cap keeps prompt size bounded). If no message-style trigger
        is in the queue, returns an empty string.
        """
        for trig in triggers:  # already newest-first
            if trig.kind == "message_inserted":
                channel_id = trig.payload.get("channel_id", "")
                msgs = self.world.messages_in_channel(channel_id)[-5:]
                return "\n".join(
                    f"{m.sender_id}@{m.sim_time}: {m.body}" for m in msgs
                )
        return ""

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def pending_triggers(self, npc_id: str) -> list[NpcTrigger]:
        """Triggers the NPC has not yet polled — sorted oldest-first.

        Tests use this to assert that "dropped balls" really did stay in
        the queue (and weren't silently consumed). The runtime drains the
        queue when the next poll fires; in the meantime callers can read
        what's still pending.
        """
        return list(self._queues.get(npc_id, []))

    def hop_count(self, sender_id: str, recipient_id: str) -> int:
        now = self.scheduler.sim_time
        return sum(
            1
            for t in self._hops.get((sender_id, recipient_id), [])
            if now - t <= self._hop_window_min
        )
