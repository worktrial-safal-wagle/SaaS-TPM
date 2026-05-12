"""Tests for the `abandon.current` tool (Phase 3)."""

from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.abandon import ABANDON_TRANSITION_COST_MIN, abandon_ops


def _setup(start_time: int = 0, busy_until: int = 0) -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler(start_time=start_time)
    world.add_person(Person(
        id="person.tpm", display_name="TPM", role="tpm",
        is_agent=True, busy_until=busy_until,
    ))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(abandon_ops())
    return world, scheduler, reg


def test_abandon_truncates_busy_until():
    """Agent busy until T+30, calls abandon at T+10 → busy_until = T+12."""
    world, scheduler, reg = _setup(start_time=10, busy_until=30)
    result = reg.dispatch(ToolCall(tool="abandon.current", args={}))
    assert result.ok
    assert result.result["abandoned"] is True
    person = world.get_person("person.tpm")
    # current (10) + ABANDON_TRANSITION_COST_MIN (2) = 12
    assert person.busy_until == 10 + ABANDON_TRANSITION_COST_MIN
    assert result.result["previous_busy_until"] == 30


def test_abandon_charges_transition_cost():
    """The 2-min cost is the context-switch penalty."""
    world, scheduler, reg = _setup(start_time=10, busy_until=30)
    result = reg.dispatch(ToolCall(tool="abandon.current", args={}))
    assert result.ok
    assert result.cost_minutes == ABANDON_TRANSITION_COST_MIN
    assert scheduler.sim_time == 10 + ABANDON_TRANSITION_COST_MIN


def test_abandon_when_not_busy_is_no_op_with_warning():
    """Agent not busy → success with abandoned=False and a warning."""
    world, scheduler, reg = _setup(start_time=20, busy_until=0)
    result = reg.dispatch(ToolCall(tool="abandon.current", args={}))
    assert result.ok
    assert result.result["abandoned"] is False
    assert "warning" in result.result
    # busy_until is unchanged
    assert world.get_person("person.tpm").busy_until == 0


def test_abandon_when_not_busy_still_charges_cost():
    """The agent still pays the 2-min cost for the unnecessary call —
    we don't want to reward 'just check if I'm busy' as a free probe."""
    world, scheduler, reg = _setup(start_time=20, busy_until=0)
    result = reg.dispatch(ToolCall(tool="abandon.current", args={}))
    assert result.ok
    assert result.cost_minutes == ABANDON_TRANSITION_COST_MIN
    assert scheduler.sim_time == 20 + ABANDON_TRANSITION_COST_MIN


def test_abandon_truncates_ongoing_60_min_action():
    """Agent busy until T+60 (a long doc.create-style action), calls
    abandon at T+10 → busy_until = T+12, truncating the remaining 50 min."""
    world, scheduler, reg = _setup(start_time=10, busy_until=70)
    result = reg.dispatch(ToolCall(tool="abandon.current", args={}))
    assert result.ok
    assert result.result["abandoned"] is True
    person = world.get_person("person.tpm")
    assert person.busy_until == 10 + ABANDON_TRANSITION_COST_MIN
    # Long action was originally 60 minutes from T; abandon at T+10 means
    # 48 minutes of remaining "long-action" time were truncated.
    assert result.result["previous_busy_until"] == 70


def test_abandon_busy_equal_to_current_is_not_busy():
    """If busy_until == current, the agent is effectively NOT busy any
    more — the action just ended. abandon is a no-op."""
    world, scheduler, reg = _setup(start_time=30, busy_until=30)
    result = reg.dispatch(ToolCall(tool="abandon.current", args={}))
    assert result.ok
    assert result.result["abandoned"] is False
    assert world.get_person("person.tpm").busy_until == 30
