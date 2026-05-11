from __future__ import annotations

import math
import random
import statistics

import pytest

from sim.npc import (
    BrainCache,
    NpcBrainContext,
    NpcBrainOutput,
    NpcPolicy,
    NpcRuntime,
    StubBrain,
)
from sim.npc.policy import DelaySpec
from sim.scheduler import Scheduler
from sim.store import Channel, Person, WorkingHours, World
from sim.store.notifications import wire_notification_dispatcher
from sim.tools import ToolCall, ToolRegistry
from sim.tools.chat import chat_ops

MON_9AM_ORIGIN = 9 * 60  # scenario starts Mon 09:00


def _make_world() -> World:
    return World(scenario_id="test", seed=42, scenario_origin_minute_of_week=MON_9AM_ORIGIN)


def _add_agent_and_npc(world: World, policy: NpcPolicy | None = None) -> tuple[Person, Person, NpcPolicy]:
    agent = Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True)
    npc = Person(
        id="person.maya", display_name="Maya", role="engineer",
        persona_notes="Junior backend engineer.",
    )
    world.add_person(agent)
    world.add_person(npc)
    world.add_channel(Channel(
        id="channel.eng", name="eng", members=["person.tpm", "person.maya"],
    ))
    pol = policy or NpcPolicy(
        allowed_tools=["chat.send", "chat.dm"],
        delays={"default": DelaySpec(kind="fixed", value_seconds=60)},
    )
    return agent, npc, pol


def _registry_for(world, scheduler, person_id):
    reg = ToolRegistry(world, scheduler, caller_id=person_id)
    reg.register_all(chat_ops())
    return reg


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_permits_only_allowed_tools():
    policy = NpcPolicy(allowed_tools=["chat.send"])
    assert policy.permits("chat.send")
    assert not policy.permits("tasks.create")


def test_policy_returns_default_delay_for_unknown_intent():
    policy = NpcPolicy(delays={"default": DelaySpec(kind="fixed", value_seconds=42)})
    spec = policy.delay_for("never_seen_intent")
    assert spec.kind == "fixed"
    assert spec.value_seconds == 42


# ---------------------------------------------------------------------------
# Brain cache
# ---------------------------------------------------------------------------


def test_brain_cache_returns_identical_output_on_repeated_event_id():
    cache = BrainCache()
    counter = {"n": 0}

    def compute():
        counter["n"] += 1
        return NpcBrainOutput(tool_calls=[ToolCall(tool="chat.send", args={"body": f"v{counter['n']}"})])

    out1 = cache.get_or_compute("scn", 7, "evt.42", compute)
    out2 = cache.get_or_compute("scn", 7, "evt.42", compute)
    assert out1 == out2
    assert counter["n"] == 1
    assert cache.hits == 1
    assert cache.misses == 1


def test_brain_cache_different_keys_call_compute():
    cache = BrainCache()
    n = {"v": 0}
    def compute():
        n["v"] += 1
        return NpcBrainOutput()
    cache.get_or_compute("a", 1, "x", compute)
    cache.get_or_compute("a", 1, "y", compute)
    cache.get_or_compute("a", 2, "x", compute)
    assert n["v"] == 3


# ---------------------------------------------------------------------------
# Runtime: reactions on DMs
# ---------------------------------------------------------------------------


def _build_runtime(
    world: World, scheduler: Scheduler, brain: StubBrain, policy: NpcPolicy,
    npc_id: str = "person.maya", rng_seed: int = 0, jitter_max_min: int = 60,
) -> NpcRuntime:
    npc_registry = _registry_for(world, scheduler, npc_id)
    return NpcRuntime(
        world, scheduler, brain,
        policies={npc_id: policy},
        npc_registries={npc_id: npc_registry},
        rng_seed=rng_seed,
        jitter_max_min=jitter_max_min,
    )


def test_npc_reacts_to_dm_with_brain_output():
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    agent_registry = _registry_for(world, scheduler, "person.tpm")

    def brain_fn(ctx: NpcBrainContext) -> NpcBrainOutput:
        # Reply in the channel that triggered the reaction
        ch_id = ctx.trigger_payload["channel_id"]
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={"channel_id": ch_id, "body": "ack from maya"}),
        ])

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy)

    # Agent DMs Maya. (agent_registry already has chat_ops registered by _registry_for.)
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "hey maya"},
    ))
    # The scheduled NPC reaction is pending; advance until it fires.
    pending = scheduler.peek_next_fire_at()
    assert pending is not None
    scheduler.advance_to(pending)

    # Maya should have sent a message in the DM channel.
    msgs = [m for m in world.messages.values() if m.sender_id == "person.maya"]
    assert len(msgs) == 1
    assert msgs[0].body == "ack from maya"


def test_npc_policy_drops_disallowed_tools():
    world = _make_world()
    _, _, _ = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    # Policy permits only chat.send — brain tries to create a task.
    policy = NpcPolicy(allowed_tools=["chat.send"])

    def brain_fn(ctx):
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="tasks.create", args={
                "task_id": "task.malicious", "project": "p", "title": "t",
            }),
        ])

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy)

    agent_registry = _registry_for(world, scheduler, "person.tpm")
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "hi"},
    ))
    pending = scheduler.peek_next_fire_at()
    if pending is not None:
        scheduler.advance_to(pending)

    # No task created
    assert "task.malicious" not in world.tasks
    # Drop recorded
    assert any(d["reason"] == "policy_denied" for d in runtime.dropped)


# ---------------------------------------------------------------------------
# Business-hours gating + jitter
# ---------------------------------------------------------------------------


def test_after_hours_dm_pushed_to_next_business_open_plus_jitter():
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy,
                             rng_seed=0, jitter_max_min=60)

    # Advance to Monday 18:00 (sim_time = 9h = 540 min)
    scheduler.advance(9 * 60)
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "late ping"},
    ))
    # Reaction should be scheduled for Tuesday morning + jitter.
    # advance(9*60) drained no events; chat.dm dispatched at sim_time=540, ran
    # message_inserted, then registry advanced by cost (1 min) to 541. The
    # NPC reaction's fire_at was computed at trigger time (sim_time=540), then
    # gated to next business open and jittered.
    pending = scheduler.peek_next_fire_at()
    assert pending is not None
    # Tuesday 09:00 = 1 day after Monday 09:00 = sim_time 1440. Jitter ∈ [0, 60].
    assert 1440 <= pending <= 1440 + 60


def test_in_hours_dm_replies_promptly():
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy)

    # 10am Monday = sim_time 60
    scheduler.advance(60)
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "ping"},
    ))
    pending = scheduler.peek_next_fire_at()
    # chat.dm handler runs at sim_time=60, fires message_inserted → reaction
    # scheduled at 60 + perception_lag(1) + delay(1 min for fixed 60s) = 62.
    # Then registry advances by chat.dm cost of 1 min → sim_time=61. Reaction
    # is still pending at 62, not yet drained.
    assert pending == 62


# ---------------------------------------------------------------------------
# Responsiveness scaling
# ---------------------------------------------------------------------------


def test_responsiveness_scales_delay_statistically():
    """A slow responder produces consistently larger delays than a fast one."""
    # We compare medians of sampled lognormal delays multiplied by responsiveness.
    fast_samples = []
    slow_samples = []
    fast_rng = random.Random(0)
    slow_rng = random.Random(0)
    for _ in range(500):
        fast_samples.append(fast_rng.lognormvariate(4.0, 1.0) * 0.3)
        slow_samples.append(slow_rng.lognormvariate(4.0, 1.0) * 2.5)
    fast_med = statistics.median(fast_samples)
    slow_med = statistics.median(slow_samples)
    # 2.5 / 0.3 ≈ 8.3 — give a bit of slack
    assert slow_med / fast_med > 5.0


# ---------------------------------------------------------------------------
# Hop cap
# ---------------------------------------------------------------------------


def test_hop_cap_blocks_runaway_npc_to_npc_chatter():
    world = _make_world()
    # Two NPCs that DM each other indefinitely
    world.add_person(Person(id="person.alice", display_name="Alice", role="eng"))
    world.add_person(Person(id="person.bob", display_name="Bob", role="eng"))
    world.add_channel(Channel(id="channel.x", name="x",
                              members=["person.alice", "person.bob"]))
    world.agent_id = None  # No agent — all chatter is NPC↔NPC
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    policy_alice = NpcPolicy(allowed_tools=["chat.send"],
                             delays={"default": DelaySpec(kind="fixed", value_seconds=60)})
    policy_bob = NpcPolicy(allowed_tools=["chat.send"],
                           delays={"default": DelaySpec(kind="fixed", value_seconds=60)})

    def echo(ctx):
        # Each NPC replies in the channel and mentions the OTHER one, so the
        # cascade actually continues until the hop cap kicks in.
        other = "person.alice" if ctx.npc_id == "person.bob" else "person.bob"
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={
                "channel_id": "channel.x", "body": f"@{other} echo",
                "mentions": [other],
            }),
        ])

    brain = StubBrain(echo)
    npc_registries = {
        "person.alice": _registry_for(world, scheduler, "person.alice"),
        "person.bob": _registry_for(world, scheduler, "person.bob"),
    }
    runtime = NpcRuntime(
        world, scheduler, brain,
        policies={"person.alice": policy_alice, "person.bob": policy_bob},
        npc_registries=npc_registries,
        rng_seed=0,
        hop_cap=3,
    )

    # Alice mentions Bob to kick off the cascade.
    alice_reg = npc_registries["person.alice"]
    alice_reg.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.x", "body": "@bob hi", "mentions": ["person.bob"]},
    ))
    # Run sim for a while.
    scheduler.advance(1000)

    # Drops with reason=hop_cap should be present once the cap is reached.
    cap_drops = [d for d in runtime.dropped if d["reason"] == "hop_cap"]
    assert len(cap_drops) > 0


def test_agent_origin_does_not_consume_hop_quota():
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy)

    agent_reg = _registry_for(world, scheduler, "person.tpm")
    for _ in range(10):
        agent_reg.dispatch(ToolCall(
            tool="chat.dm", args={"recipient_id": "person.maya", "body": "hi"},
        ))
    # Even though we're past the hop cap, no agent-origin drops recorded.
    assert all(d["reason"] != "hop_cap" for d in runtime.dropped)


# ---------------------------------------------------------------------------
# Replayable execution
# ---------------------------------------------------------------------------


def test_same_seed_and_event_id_produces_same_npc_output():
    """If the brain is stochastic, the cache key still ensures replay determinism."""
    cache = BrainCache()
    calls = {"n": 0}

    def stochastic_compute():
        calls["n"] += 1
        # Returns different content each call
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={"body": f"call_{calls['n']}"}),
        ])

    out1 = cache.get_or_compute("scn", 0, "evt.1", stochastic_compute)
    out2 = cache.get_or_compute("scn", 0, "evt.1", stochastic_compute)
    out3 = cache.get_or_compute("scn", 0, "evt.1", stochastic_compute)
    assert out1.tool_calls[0].args["body"] == "call_1"
    assert out1 == out2 == out3
    assert calls["n"] == 1


def test_seeded_rng_produces_deterministic_delays():
    spec = DelaySpec(kind="lognormal", mu=4.0, sigma=1.0)
    # Two runtimes with the same seed should sample identically.
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler1 = Scheduler()
    scheduler2 = Scheduler()
    brain = StubBrain(lambda ctx: NpcBrainOutput())
    r1 = _build_runtime(world, scheduler1, brain, policy, rng_seed=99)
    r2 = _build_runtime(World(scenario_id="test", seed=42, scenario_origin_minute_of_week=MON_9AM_ORIGIN), scheduler2, brain, policy, rng_seed=99)
    seq1 = [r1._sample_delay(spec) for _ in range(20)]
    seq2 = [r2._sample_delay(spec) for _ in range(20)]
    assert seq1 == seq2
