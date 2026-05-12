"""Tests for personal-cadence actor poll scheduling (Phase 2)."""

from __future__ import annotations

import pytest

from sim.scheduler import (
    EVENT_KIND_ACTOR_POLL,
    Scheduler,
    schedule_actor_poll,
)
from sim.store import World
from sim.store.entities import Person


def _make_world_with_person(
    person_id: str,
    *,
    busy_until: int = 0,
    is_agent: bool = False,
) -> World:
    world = World()
    world.add_person(
        Person(
            id=person_id,
            display_name=person_id,
            role="ic",
            is_agent=is_agent,
            busy_until=busy_until,
        )
    )
    return world


def test_actor_poll_basic():
    """At sim_time=100, tick=15, busy_until=0 → poll fires at 115."""
    world = _make_world_with_person("person.alex")
    scheduler = Scheduler(start_time=100)

    event = schedule_actor_poll(scheduler, world, "person.alex", 15)

    assert event.fire_at == 115
    assert event.kind == EVENT_KIND_ACTOR_POLL
    assert event.kind == "actor_poll"
    assert event.payload["actor_id"] == "person.alex"
    assert scheduler.peek_next_fire_at() == 115


def test_actor_poll_respects_busy_until():
    """busy_until=200 trumps sim_time + tick_size when busy_until is later."""
    world = _make_world_with_person("person.busy", busy_until=200)
    scheduler = Scheduler(start_time=100)

    event = schedule_actor_poll(scheduler, world, "person.busy", 15)

    # max(100 + 15, 200) = 200
    assert event.fire_at == 200
    assert event.payload["actor_id"] == "person.busy"


def test_actor_poll_busy_until_in_past_is_ignored():
    """If busy_until < sim_time + tick, plain cadence wins."""
    world = _make_world_with_person("person.free", busy_until=50)
    scheduler = Scheduler(start_time=100)

    event = schedule_actor_poll(scheduler, world, "person.free", 15)

    # max(100 + 15, 50) = 115
    assert event.fire_at == 115


def test_two_actors_independent_polls():
    """Two actors with different sim_times + tick sizes fire in expected order."""
    world = World()
    world.add_person(Person(id="person.a", display_name="A", role="ic"))
    world.add_person(Person(id="person.b", display_name="B", role="ic"))

    # Actor A scheduled first at sim_time=100 with tick_size=15 → fire_at=115
    scheduler = Scheduler(start_time=100)
    event_a = schedule_actor_poll(scheduler, world, "person.a", 15)
    assert event_a.fire_at == 115

    # Advance clock to 110, then schedule B with tick_size=20 → fire_at=130
    scheduler.advance(10)
    assert scheduler.sim_time == 110
    event_b = schedule_actor_poll(scheduler, world, "person.b", 20)
    assert event_b.fire_at == 130

    # Drain — both should fire in order
    fired = scheduler.advance_to(200)
    poll_events = [e for e in fired if e.kind == EVENT_KIND_ACTOR_POLL]
    assert len(poll_events) == 2
    assert poll_events[0].payload["actor_id"] == "person.a"
    assert poll_events[0].fire_at == 115
    assert poll_events[1].payload["actor_id"] == "person.b"
    assert poll_events[1].fire_at == 130


def test_actor_poll_event_id_deterministic():
    """Same actor, same sim_time → consistent event_id pattern in payload.

    Mirrors the `npc_reaction.<trigger_kind>.<message_id>.<npc_id>` pattern
    used in `sim/npc/runtime.py`. For actor polls the pattern is
    `actor_poll.<actor_id>.<fire_at>`.
    """
    world = _make_world_with_person("person.det")
    scheduler = Scheduler(start_time=100)

    e1 = schedule_actor_poll(scheduler, world, "person.det", 15)
    expected = "actor_poll.person.det.115"
    assert e1.payload["event_id"] == expected

    # Re-scheduling the same actor at the same sim_time + same tick should
    # produce the same event_id payload string. The numeric Event.event_id
    # increments (used internally for heap tiebreaking), but the
    # deterministic payload string stays stable.
    e2 = schedule_actor_poll(scheduler, world, "person.det", 15)
    assert e2.payload["event_id"] == expected
    assert e2.payload["event_id"] == e1.payload["event_id"]

    # And the pattern is the expected dotted form.
    assert e1.payload["event_id"].startswith("actor_poll.")
    assert "person.det" in e1.payload["event_id"]
    assert e1.payload["event_id"].endswith("115")


def test_actor_poll_unknown_actor_raises():
    """Scheduling a poll for an actor not in the world fails loudly."""
    world = World()
    scheduler = Scheduler(start_time=100)
    with pytest.raises(ValueError, match="unknown actor"):
        schedule_actor_poll(scheduler, world, "person.ghost", 15)
