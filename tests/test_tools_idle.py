"""Tests for the `idle.until` tool (Phase 3)."""

from __future__ import annotations

from sim.scheduler import EVENT_KIND_ACTOR_POLL, Scheduler
from sim.store import Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.idle import IDLE_UNTIL_CAP_MIN, IDLE_UNTIL_COST_MIN, idle_ops


def _setup(start_time: int = 0) -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler(start_time=start_time)
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(idle_ops())
    return world, scheduler, reg


def test_idle_until_basic_30_min_skip():
    """idle for 30 min → next_poll_at = current + 30."""
    world, scheduler, reg = _setup(start_time=15)
    result = reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 45},
    ))
    assert result.ok
    assert result.result["next_poll_at"] == 45
    assert world.get_person("person.tpm").next_poll_at == 45


def test_idle_until_advances_sim_time_by_cost():
    """The tool charges IDLE_UNTIL_COST_MIN (1 sim-min) regardless of target."""
    world, scheduler, reg = _setup(start_time=15)
    result = reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 45},
    ))
    assert result.ok
    assert result.cost_minutes == IDLE_UNTIL_COST_MIN
    assert scheduler.sim_time == 15 + IDLE_UNTIL_COST_MIN


def test_idle_until_clamps_over_cap():
    """idle for 200 min → clamped to current + 120 (the cap)."""
    world, scheduler, reg = _setup(start_time=10)
    result = reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 210},
    ))
    assert result.ok
    # Cap is 120 sim-min from current=10 → next_poll_at = 130.
    assert result.result["next_poll_at"] == 10 + IDLE_UNTIL_CAP_MIN
    assert world.get_person("person.tpm").next_poll_at == 10 + IDLE_UNTIL_CAP_MIN
    # The warning explicitly mentions the cap so the agent can adjust.
    assert "warning" in result.result
    assert str(IDLE_UNTIL_CAP_MIN) in result.result["warning"]


def test_idle_until_past_target_clamps_to_at_least_current_plus_one():
    """idle for past target → at least current + 1 (strictly future)."""
    world, scheduler, reg = _setup(start_time=100)
    result = reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 50},  # in the past
    ))
    assert result.ok
    # The new poll must fire strictly after the post-cost-advance sim_time
    # so it doesn't drain silently inside the registry's advance. At least
    # current + 1 (the spec contract).
    next_poll_at = world.get_person("person.tpm").next_poll_at
    assert next_poll_at >= 100 + 1


def test_idle_until_schedules_actor_poll_event():
    """A fresh actor_poll for the caller is in the heap at the new fire time."""
    world, scheduler, reg = _setup(start_time=15)
    reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 60},
    ))
    polls = [
        e for e in scheduler.pending()
        if e.kind == EVENT_KIND_ACTOR_POLL and e.payload.get("actor_id") == "person.tpm"
    ]
    assert any(p.fire_at == 60 for p in polls), (
        f"expected actor_poll@60, got fire_ats={[p.fire_at for p in polls]}"
    )


def test_idle_until_event_id_payload_is_deterministic():
    """payload event_id matches the actor_poll.<id>.<fire_at> pattern."""
    world, scheduler, reg = _setup(start_time=20)
    reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 80},
    ))
    polls = [
        e for e in scheduler.pending()
        if e.kind == EVENT_KIND_ACTOR_POLL and e.payload.get("actor_id") == "person.tpm"
    ]
    assert polls, "no actor_poll scheduled"
    expected = "actor_poll.person.tpm.80"
    assert any(p.payload.get("event_id") == expected for p in polls)


def test_idle_until_accepts_or_on_event_arg_and_ignores_it():
    """`or_on_event` is reserved for a future wake-on-event variant; accepted
    and ignored today so agents that pass it don't trigger a validation error."""
    world, scheduler, reg = _setup(start_time=15)
    result = reg.dispatch(ToolCall(
        tool="idle.until",
        args={"target_sim_time": 45, "or_on_event": "chat_to_agent"},
    ))
    assert result.ok
    assert result.result["next_poll_at"] == 45


def test_idle_until_zero_delta_clamped_to_future():
    """idle.until(target=current) clamps to a strictly future time."""
    world, scheduler, reg = _setup(start_time=50)
    result = reg.dispatch(ToolCall(
        tool="idle.until", args={"target_sim_time": 50},
    ))
    assert result.ok
    # current + 1 is the minimum logical target; after the cost advance the
    # poll lands at >= current + 2 so it doesn't drain inside the advance.
    assert result.result["next_poll_at"] >= 50 + 1
