from __future__ import annotations

from sim.npc import (
    BrainCache,
    NpcBrainContext,
    NpcBrainOutput,
    NpcPolicy,
    NpcRuntime,
    NpcTrigger,
    StubBrain,
)
from sim.npc.policy import DelaySpec
from sim.scheduler import EVENT_KIND_ACTOR_POLL, Scheduler
from sim.store import CalendarEvent, Channel, Person, World
from sim.store.notifications import wire_notification_dispatcher
from sim.tools import ToolCall, ToolRegistry
from sim.tools.chat import chat_ops

MON_9AM_ORIGIN = 9 * 60  # scenario starts Mon 09:00
DEFAULT_TICK = 15


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


def _add_meeting(world: World, *, event_id: str, start: int, end: int,
                 attendees: list[str], organizer: str = "person.tpm") -> CalendarEvent:
    evt = CalendarEvent(
        id=event_id, title="standup", start_sim_time=start, end_sim_time=end,
        organizer_id=organizer, attendees=attendees,
    )
    world.add_calendar_event(evt)
    return evt


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
# Runtime helpers
# ---------------------------------------------------------------------------


def _build_runtime(
    world: World, scheduler: Scheduler, brain: StubBrain, policy: NpcPolicy,
    npc_id: str = "person.maya",
    tick_size_minutes: int = DEFAULT_TICK,
    hop_cap: int = 4,
    schedule_initial_polls: bool = True,
) -> NpcRuntime:
    npc_registry = _registry_for(world, scheduler, npc_id)
    return NpcRuntime(
        world, scheduler, brain,
        policies={npc_id: policy},
        npc_registries={npc_id: npc_registry},
        tick_size_minutes=tick_size_minutes,
        hop_cap=hop_cap,
        schedule_initial_polls=schedule_initial_polls,
    )


# ---------------------------------------------------------------------------
# Tick polling: NPC sees triggers, decides one action, re-polls.
# ---------------------------------------------------------------------------


def test_initial_poll_scheduled_at_tick_size():
    """Runtime construction schedules each NPC's first poll at sim_time + tick."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)
    # First poll should be at sim_time(0) + 15 = 15.
    assert scheduler.peek_next_fire_at() == 15
    # And the NPC's next_poll_at mirrors that.
    assert world.get_person("person.maya").next_poll_at == 15


def test_npc_replies_to_dm_on_next_tick():
    """Agent DMs Maya; on Maya's next poll the brain sees the DM and replies."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    seen_triggers: list[list[NpcTrigger]] = []

    def brain_fn(ctx: NpcBrainContext) -> NpcBrainOutput:
        # Snapshot what the brain sees so the test can assert on it.
        seen_triggers.append(list(ctx.triggers))
        if not ctx.triggers:
            return NpcBrainOutput()
        # Reply in the channel that brought the most-recent trigger.
        trig = ctx.triggers[0]
        ch_id = trig.payload.get("channel_id", "")
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={"channel_id": ch_id, "body": "ack from maya"}),
        ])

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)

    # Agent DMs Maya at sim_time=0; chat.dm advances sim_time by 1 minute.
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "hey maya"},
    ))
    # DM is now queued for Maya. Next poll at 15.
    assert len(runtime.pending_triggers("person.maya")) == 1

    scheduler.advance_to(15)

    # Maya should have replied once, after observing the DM in her queue.
    msgs = [m for m in world.messages.values() if m.sender_id == "person.maya"]
    assert len(msgs) == 1
    assert msgs[0].body == "ack from maya"
    # Brain saw exactly the DM as a trigger.
    assert len(seen_triggers) == 1
    assert len(seen_triggers[0]) == 1
    assert seen_triggers[0][0].kind == "message_inserted"
    # Queue drained; next poll re-scheduled. The reply (chat.send) cost
    # 1 minute, so the next poll lands at 16 + 15 = 31.
    assert runtime.pending_triggers("person.maya") == []
    assert scheduler.peek_next_fire_at() == 31


def test_quiet_poll_no_triggers_no_action():
    """A poll with an empty queue results in no action, but re-schedules."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)

    # No incoming. Advance to first poll.
    scheduler.advance_to(15)
    # No messages sent, but the next poll is queued.
    msgs = [m for m in world.messages.values() if m.sender_id == "person.maya"]
    assert msgs == []
    assert scheduler.peek_next_fire_at() == 30


def test_npc_policy_drops_disallowed_tools():
    """A brain that proposes a disallowed tool gets it dropped; nothing dispatched."""
    world = _make_world()
    _, _, _ = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
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
    scheduler.advance_to(15)

    assert "task.malicious" not in world.tasks
    assert any(d["reason"] == "policy_denied" for d in runtime.dropped)


def test_npc_single_decision_per_tick():
    """Brain returns 2 tool calls; only the first runs, second is recorded as a drop."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    def brain_fn(ctx):
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={
                "channel_id": "channel.eng", "body": "first"}),
            ToolCall(tool="chat.send", args={
                "channel_id": "channel.eng", "body": "second"}),
        ])

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy)

    # Trigger a poll with at least one inbound so the brain runs.
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "go"},
    ))
    scheduler.advance_to(15)

    bodies = [m.body for m in world.messages.values() if m.sender_id == "person.maya"]
    assert bodies == ["first"], f"only first call should run, got {bodies}"
    assert any(d["reason"] == "single_decision_per_tick" and d["tool"] == "chat.send"
               for d in runtime.dropped)


# ---------------------------------------------------------------------------
# Calendar-driven busyness: poll fires after meeting end, not during.
# ---------------------------------------------------------------------------


def test_npc_in_meeting_polls_after_meeting_end():
    """Maya in a meeting 10:00-11:00 doesn't poll mid-meeting.

    The calendar_event_start handler sets busy_until=event.end, which makes
    `schedule_actor_poll` push the next poll past meeting end. Without a
    top-level Runtime we wire calendar handling manually here.
    """
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    _add_meeting(world, event_id="cal.standup", start=60, end=120,
                 attendees=["person.maya", "person.tpm"])

    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)
    scheduler.register_handler("calendar_event_start", runtime.on_calendar_event_start)
    scheduler.schedule(
        fire_at=60, kind="calendar_event_start",
        payload={"event_id": "cal.standup"},
    )

    # Run through the meeting: polls at 15, 30, 45 fire (before meeting),
    # then meeting starts at 60 → busy_until=120 → next poll pushed past 120.
    scheduler.advance_to(120)
    # busy_until on Maya has been set by the handler.
    assert world.get_person("person.maya").busy_until == 120
    # The next pending poll for Maya should be at >= 120.
    polls = [e for e in scheduler.pending() if e.kind == EVENT_KIND_ACTOR_POLL
             and e.payload.get("actor_id") == "person.maya"]
    assert polls, "expected at least one pending poll"
    # The next poll after meeting-end is the one scheduled when the in-meeting
    # poll fired (which itself rescheduled because busy_until > sim_time).
    assert min(p.fire_at for p in polls) >= 120


def test_npc_polls_during_meeting_skip_action():
    """A poll that fires while busy_until > sim_time does nothing — the brain
    is not invoked and the trigger queue stays intact for after-meeting."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    # Meeting from t=10 to t=200.
    _add_meeting(world, event_id="cal.m", start=10, end=200,
                 attendees=["person.maya"])

    brain_invocations: list[NpcBrainContext] = []
    def brain_fn(ctx):
        brain_invocations.append(ctx)
        if ctx.triggers:
            return NpcBrainOutput(tool_calls=[
                ToolCall(tool="chat.send", args={
                    "channel_id": "channel.eng", "body": "reply"}),
            ])
        return NpcBrainOutput()

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)
    scheduler.register_handler("calendar_event_start", runtime.on_calendar_event_start)
    scheduler.schedule(
        fire_at=10, kind="calendar_event_start",
        payload={"event_id": "cal.m"},
    )

    # Queue a DM at sim_time=0 so it's in Maya's inbox before the meeting.
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    agent_registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "before meeting"},
    ))
    # Trigger queued.
    assert len(runtime.pending_triggers("person.maya")) == 1

    # Advance halfway into the meeting. The poll at t=15 fires WHILE Maya
    # is busy; it should skip without invoking the brain and re-schedule.
    scheduler.advance_to(100)
    # Brain wasn't invoked at all — Maya is busy through this window.
    assert brain_invocations == [], (
        "brain should not be invoked during a meeting (busy_until > sim_time)"
    )
    # Queue still has the DM waiting.
    assert len(runtime.pending_triggers("person.maya")) == 1


# ---------------------------------------------------------------------------
# Backlog & dropped balls
# ---------------------------------------------------------------------------


def test_npc_backlog_only_handles_one_per_tick():
    """3 DMs arrive while Maya is in a meeting; after meeting she replies to ONE,
    and the other 2 remain queued for the next tick."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    _add_meeting(world, event_id="cal.m", start=5, end=60,
                 attendees=["person.maya"])

    def brain_fn(ctx):
        if not ctx.triggers:
            return NpcBrainOutput()
        # Reply to the most-recent trigger.
        trig = ctx.triggers[0]
        ch_id = trig.payload.get("channel_id", "")
        # Echo the body so we can tell which DM the brain picked.
        body_in = world.messages[trig.payload["message_id"]].body
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={
                "channel_id": ch_id, "body": f"re: {body_in}"}),
        ])

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)
    scheduler.register_handler("calendar_event_start", runtime.on_calendar_event_start)
    scheduler.schedule(
        fire_at=5, kind="calendar_event_start",
        payload={"event_id": "cal.m"},
    )

    # Send 3 DMs while Maya is busy. The agent's chat.dm advances sim_time
    # by 1 each, so they land at 1, 2, 3.
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    for body in ["q1", "q2", "q3"]:
        agent_registry.dispatch(ToolCall(
            tool="chat.dm", args={"recipient_id": "person.maya", "body": body},
        ))

    # Three DMs queued.
    assert len(runtime.pending_triggers("person.maya")) == 3

    # Drive past the meeting end.
    scheduler.advance_to(75)

    # Maya replied exactly once (single-decision-per-tick). Her first
    # post-meeting poll picked the freshest DM (q3) since recency is the
    # default priority.
    maya_replies = [m.body for m in world.messages.values()
                    if m.sender_id == "person.maya"]
    assert len(maya_replies) == 1
    assert maya_replies[0] == "re: q3"
    # Two DMs remain queued for next tick.
    pending = runtime.pending_triggers("person.maya")
    assert len(pending) == 2
    pending_bodies = {
        world.messages[t.payload["message_id"]].body for t in pending
    }
    assert pending_bodies == {"q1", "q2"}


def test_npc_drops_balls_when_backlog_exceeds_polls():
    """If many DMs land and the run ends before NPC can drain them all,
    leftover triggers stay in the queue as an observable signal."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    def brain_fn(ctx):
        if not ctx.triggers:
            return NpcBrainOutput()
        # Reply to the freshest.
        trig = ctx.triggers[0]
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.dm", args={
                "recipient_id": "person.tpm", "body": "ack"}),
        ])

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=15)

    # Pile up 5 DMs all at sim_time near 0. Default tick=15; the agent's
    # chat.dm cost is 1, so Maya's first poll fires at t=15, then 31, 47, ...
    # (each tick the chat.send reply costs 1 minute on top of tick_size).
    agent_registry = _registry_for(world, scheduler, "person.tpm")
    for body in ["a", "b", "c", "d", "e"]:
        agent_registry.dispatch(ToolCall(
            tool="chat.dm", args={"recipient_id": "person.maya", "body": body},
        ))

    # Let two polls fire (t=15 and t=31). 5 DMs - 2 picks = 3 remain.
    scheduler.advance_to(35)
    pending = runtime.pending_triggers("person.maya")
    assert len(pending) == 3, f"expected 3 dropped balls, got {len(pending)}"


# ---------------------------------------------------------------------------
# Tick-aware hop cap (peer-to-peer NPC chatter)
# ---------------------------------------------------------------------------


def test_hop_cap_blocks_runaway_npc_to_npc_chatter():
    """Two NPCs echoing each other in a channel: hop-cap stops the cascade."""
    world = _make_world()
    world.add_person(Person(id="person.alice", display_name="Alice", role="eng"))
    world.add_person(Person(id="person.bob", display_name="Bob", role="eng"))
    world.add_channel(Channel(id="channel.x", name="x",
                              members=["person.alice", "person.bob"]))
    world.agent_id = None  # No agent — all chatter is NPC↔NPC
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    policy = NpcPolicy(
        allowed_tools=["chat.send"],
        delays={"default": DelaySpec(kind="fixed", value_seconds=60)},
    )

    def echo(ctx):
        if not ctx.triggers:
            return NpcBrainOutput()
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
        policies={"person.alice": policy, "person.bob": policy},
        npc_registries=npc_registries,
        tick_size_minutes=DEFAULT_TICK,
        hop_cap=3,
    )

    # Seed: Alice mentions Bob in the channel.
    alice_reg = npc_registries["person.alice"]
    alice_reg.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.x", "body": "@bob hi",
              "mentions": ["person.bob"]},
    ))
    # Run sim long enough for the cascade to cross the hop cap.
    scheduler.advance(1000)

    cap_drops = [d for d in runtime.dropped if d["reason"] == "hop_cap"]
    assert len(cap_drops) > 0


def test_agent_origin_does_not_consume_hop_quota():
    """Many agent → NPC messages never trip the hop cap."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)
    brain = StubBrain(lambda ctx: NpcBrainOutput())
    runtime = _build_runtime(world, scheduler, brain, policy, hop_cap=3)

    agent_reg = _registry_for(world, scheduler, "person.tpm")
    for _ in range(10):
        agent_reg.dispatch(ToolCall(
            tool="chat.dm", args={"recipient_id": "person.maya", "body": "hi"},
        ))
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
        return NpcBrainOutput(tool_calls=[
            ToolCall(tool="chat.send", args={"body": f"call_{calls['n']}"}),
        ])

    out1 = cache.get_or_compute("scn", 0, "evt.1", stochastic_compute)
    out2 = cache.get_or_compute("scn", 0, "evt.1", stochastic_compute)
    out3 = cache.get_or_compute("scn", 0, "evt.1", stochastic_compute)
    assert out1.tool_calls[0].args["body"] == "call_1"
    assert out1 == out2 == out3
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Triggers are presented most-recent-first
# ---------------------------------------------------------------------------


def test_triggers_ordered_most_recent_first_in_context():
    """The brain receives triggers sorted by sim_time descending — recency is
    the default priority signal."""
    world = _make_world()
    _, _, policy = _add_agent_and_npc(world)
    scheduler = Scheduler()
    wire_notification_dispatcher(world)

    seen: list[list[NpcTrigger]] = []
    def brain_fn(ctx):
        seen.append(list(ctx.triggers))
        return NpcBrainOutput()

    brain = StubBrain(brain_fn)
    runtime = _build_runtime(world, scheduler, brain, policy, tick_size_minutes=60)

    # DMs at sim_time 1, 2, 3 (chat.dm advances sim_time by 1 each).
    agent_reg = _registry_for(world, scheduler, "person.tpm")
    for body in ["first", "second", "third"]:
        agent_reg.dispatch(ToolCall(
            tool="chat.dm", args={"recipient_id": "person.maya", "body": body},
        ))

    # First poll at 60. Brain should see [third (sim_time=3), second (2), first (1)].
    scheduler.advance_to(60)
    assert len(seen) == 1
    bodies = [
        world.messages[t.payload["message_id"]].body for t in seen[0]
    ]
    assert bodies == ["third", "second", "first"]
