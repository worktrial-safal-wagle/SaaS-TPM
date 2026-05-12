"""Top-level runtime assembly.

Builds the full simulation runtime from a `LoadedScenario`:
  - Wires the notification dispatcher.
  - Creates a `MeetingRunner` and binds it to the meetings tools.
  - Builds the agent's `ToolRegistry` and a per-NPC `ToolRegistry`.
  - Constructs the `NpcRuntime` and schedules its initial polls.
  - Wires `calendar_event_start` to mark NPC attendees busy_until end.
  - Returns everything the agent driver needs.

A reviewer's main path is: `load_scenario(...)` → `build_runtime(...)` →
`AgentDriver.run()`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sim.npc import BrainCache, NpcBrain, NpcRuntime, StubBrain
from sim.npc.brain import NpcBrainContext, NpcBrainOutput
from sim.npc.meetings import MeetingRunner
from sim.scenario.loader import LoadedScenario
from sim.scheduler import Event, Scheduler, schedule_actor_poll
from sim.store import World
from sim.store.notifications import wire_notification_dispatcher
from sim.tools import ToolRegistry, all_ops
from sim.tools.meetings import configure_meetings


@dataclass
class Runtime:
    scenario: LoadedScenario
    world: World
    scheduler: Scheduler
    agent_registry: ToolRegistry
    npc_registries: dict[str, ToolRegistry]
    npc_runtime: NpcRuntime
    meeting_runner: MeetingRunner
    brain_cache: BrainCache


def _default_stub_brain() -> NpcBrain:
    def respond(ctx: NpcBrainContext) -> NpcBrainOutput:
        # Conservative default: NPCs stay silent unless they're an explicit
        # speaker turn (meetings).
        if ctx.trigger_kind == "meeting_turn":
            return NpcBrainOutput(
                speech=f"({ctx.npc_id} contributes to the discussion.)",
            )
        return NpcBrainOutput()
    return StubBrain(respond)


def build_runtime(scenario: LoadedScenario, *, brain: NpcBrain | None = None) -> Runtime:
    world = scenario.world
    scheduler = scenario.scheduler

    wire_notification_dispatcher(world)

    agent_id = scenario.config.agent_id
    agent_registry = ToolRegistry(world, scheduler, caller_id=agent_id)
    agent_registry.register_all(all_ops())

    # Each NPC gets its own ToolRegistry so caller_id is correct.
    npc_registries: dict[str, ToolRegistry] = {}
    for npc_id in scenario.npc_policies:
        reg = ToolRegistry(world, scheduler, caller_id=npc_id)
        reg.register_all(all_ops())
        npc_registries[npc_id] = reg

    brain_cache = BrainCache()
    chosen_brain = brain or _default_stub_brain()

    npc_runtime = NpcRuntime(
        world, scheduler, chosen_brain,
        policies=dict(scenario.npc_policies),
        npc_registries=npc_registries,
        brain_cache=brain_cache,
        rng_seed=scenario.config.seed,
        tick_size_minutes=scenario.config.tick_size_minutes,
    )

    meeting_runner = MeetingRunner(world, scheduler, chosen_brain, brain_cache)
    configure_meetings(meeting_runner)

    # Schedule calendar_event_start markers so the NPC runtime can set
    # `busy_until` on attendees when each meeting begins. Without the
    # per-event schedule the handler never fires and NPCs would happily
    # reply to DMs mid-meeting.
    scheduler.register_handler(
        "calendar_event_start", npc_runtime.on_calendar_event_start,
    )
    for evt in scenario.world.calendar.values():
        scheduler.schedule(
            fire_at=evt.start_sim_time, kind="calendar_event_start",
            payload={"event_id": evt.id},
        )

    # Schedule the agent's first actor_poll. `schedule_actor_poll`
    # computes fire_at = max(0 + tick_size, busy_until=0) = tick_size, so
    # the agent gets its first decision at sim_time = tick_size_minutes.
    # Without this initial schedule the tick-driven AgentDriver would
    # never see a poll fire. The agent is assumed to exist (loader sets
    # agent_id from `is_agent: true`).
    if world.get_person(agent_id) is not None:
        schedule_actor_poll(
            scheduler, world, agent_id, scenario.config.tick_size_minutes,
        )

    return Runtime(
        scenario=scenario, world=world, scheduler=scheduler,
        agent_registry=agent_registry, npc_registries=npc_registries,
        npc_runtime=npc_runtime, meeting_runner=meeting_runner,
        brain_cache=brain_cache,
    )
