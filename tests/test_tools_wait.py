from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.costs import AGENT_VISIBLE_EVENT_KINDS
from sim.tools.wait import wait_ops


def _registry() -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(wait_ops())
    return world, scheduler, reg


def test_wait_until_advances_to_target():
    world, scheduler, reg = _registry()
    result = reg.dispatch(ToolCall(
        tool="wait.until", args={"target_sim_time": 120},
    ))
    assert result.ok
    assert result.cost_minutes == 120
    assert scheduler.sim_time == 120


def test_wait_until_past_rejected():
    world, scheduler, reg = _registry()
    scheduler.advance(100)
    result = reg.dispatch(ToolCall(
        tool="wait.until", args={"target_sim_time": 50},
    ))
    assert result.ok is False
    assert "past" in result.error


def test_wait_until_same_time_no_op():
    world, scheduler, reg = _registry()
    scheduler.advance(50)
    result = reg.dispatch(ToolCall(
        tool="wait.until", args={"target_sim_time": 50},
    ))
    assert result.ok
    assert result.cost_minutes == 0


def test_wait_for_next_event_with_no_event_returns_error():
    world, scheduler, reg = _registry()
    result = reg.dispatch(ToolCall(tool="wait.for_next_event", args={}))
    assert result.ok is False
    assert "no agent-visible event" in result.error


def test_wait_for_next_event_advances_to_earliest_visible():
    world, scheduler, reg = _registry()
    # Schedule one non-visible event at 10, one visible at 30
    assert "background_npc_tick" not in AGENT_VISIBLE_EVENT_KINDS
    scheduler.schedule(fire_at=10, kind="background_npc_tick")
    scheduler.schedule(fire_at=30, kind="agent_heartbeat")
    scheduler.schedule(fire_at=50, kind="email_to_agent")
    result = reg.dispatch(ToolCall(tool="wait.for_next_event", args={}))
    assert result.ok
    # Should advance to 30, the first agent-visible event.
    # All events at fire_at <= 30 fire (both the 10 and the 30 events drain).
    assert scheduler.sim_time == 30
    assert result.cost_minutes == 30


def test_wait_for_next_event_drains_due_when_already_pending():
    world, scheduler, reg = _registry()
    scheduler.advance(20)
    scheduler.schedule(fire_at=20, kind="chat_to_agent")
    result = reg.dispatch(ToolCall(tool="wait.for_next_event", args={}))
    assert result.ok
    assert result.cost_minutes == 0
    # Event has fired (drained)
    assert scheduler.peek_next_fire_at() is None
