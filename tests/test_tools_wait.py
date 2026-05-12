from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Person, World
from sim.tools import ToolCall, ToolRegistry
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


def test_wait_for_next_event_is_not_a_registered_tool():
    """`wait.for_next_event` was deprecated in the tick-driven rewrite.
    The tool should not be registered any more; calling it should fail
    with the standard unknown-tool error path."""
    world, scheduler, reg = _registry()
    result = reg.dispatch(ToolCall(tool="wait.for_next_event", args={}))
    assert result.ok is False
    assert "unknown tool" in result.error
