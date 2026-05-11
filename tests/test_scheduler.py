from __future__ import annotations

import pytest

from sim.scheduler import Event, Scheduler


def test_starts_at_zero_by_default():
    s = Scheduler()
    assert s.sim_time == 0
    assert s.peek_next_fire_at() is None


def test_starts_at_explicit_time():
    s = Scheduler(start_time=540)
    assert s.sim_time == 540


def test_schedule_returns_event_with_monotonic_id():
    s = Scheduler()
    e1 = s.schedule(fire_at=10, kind="a")
    e2 = s.schedule(fire_at=10, kind="b")
    assert e1.event_id == 0
    assert e2.event_id == 1


def test_cannot_schedule_in_the_past():
    s = Scheduler(start_time=100)
    with pytest.raises(ValueError):
        s.schedule(fire_at=50, kind="a")


def test_cannot_advance_backwards():
    s = Scheduler(start_time=100)
    with pytest.raises(ValueError):
        s.advance_to(50)


def test_advance_negative_minutes_rejected():
    s = Scheduler()
    with pytest.raises(ValueError):
        s.advance(-1)


def test_advance_with_no_events_just_moves_clock():
    s = Scheduler()
    fired = s.advance(60)
    assert s.sim_time == 60
    assert fired == []


def test_events_fire_when_due():
    s = Scheduler()
    s.schedule(fire_at=10, kind="due")
    s.schedule(fire_at=100, kind="later")
    fired = s.advance(10)
    assert [e.kind for e in fired] == ["due"]
    assert s.sim_time == 10
    # Later event still pending
    assert s.peek_next_fire_at() == 100


def test_same_fire_at_resolves_by_event_id():
    """Tiebreak is deterministic — replay depends on this invariant."""
    s = Scheduler()
    s.schedule(fire_at=5, kind="first")
    s.schedule(fire_at=5, kind="second")
    s.schedule(fire_at=5, kind="third")
    fired = s.advance(5)
    assert [e.kind for e in fired] == ["first", "second", "third"]


def test_events_drain_in_fire_at_order_then_event_id():
    s = Scheduler()
    s.schedule(fire_at=10, kind="A")
    s.schedule(fire_at=5, kind="B")
    s.schedule(fire_at=5, kind="C")
    s.schedule(fire_at=20, kind="D")
    fired = s.advance(20)
    assert [e.kind for e in fired] == ["B", "C", "A", "D"]


def test_handler_fires_with_event_and_scheduler():
    s = Scheduler()
    seen: list[tuple[Event, int]] = []

    def handler(event, sched):
        seen.append((event, sched.sim_time))

    s.schedule(fire_at=15, kind="ping", payload={"x": 1}, handler=handler)
    s.advance(15)
    assert len(seen) == 1
    event, sim_time_at_fire = seen[0]
    assert event.kind == "ping"
    assert event.payload == {"x": 1}
    assert sim_time_at_fire == 15


def test_default_handler_registered_by_kind():
    s = Scheduler()
    calls: list[str] = []
    s.register_handler("kind_a", lambda e, _: calls.append(f"a:{e.event_id}"))
    s.register_handler("kind_b", lambda e, _: calls.append(f"b:{e.event_id}"))
    s.schedule(fire_at=1, kind="kind_a")
    s.schedule(fire_at=1, kind="kind_b")
    s.advance(1)
    assert calls == ["a:0", "b:1"]


def test_event_specific_handler_overrides_default():
    s = Scheduler()
    s.register_handler("k", lambda e, _: pytest.fail("default should not fire"))
    fired_by: list[str] = []
    s.schedule(fire_at=1, kind="k", handler=lambda e, _: fired_by.append("specific"))
    s.advance(1)
    assert fired_by == ["specific"]


def test_handler_can_schedule_more_events():
    s = Scheduler()
    record: list[str] = []

    def secondary(event, sched):
        record.append(f"secondary@{sched.sim_time}")

    def primary(event, sched):
        record.append(f"primary@{sched.sim_time}")
        sched.schedule(fire_at=sched.sim_time + 3, kind="secondary", handler=secondary)

    s.schedule(fire_at=5, kind="primary", handler=primary)
    s.advance(10)
    # Primary fired at 5, scheduled secondary at 8, which is <= 10 so also fired in same advance
    assert record == ["primary@5", "secondary@8"]


def test_recursive_scheduling_terminates():
    """A handler that schedules a same-time-or-later event must not infinite-loop."""
    s = Scheduler()
    counter = {"n": 0}

    def chain(event, sched):
        counter["n"] += 1
        if counter["n"] < 5:
            # Schedule the next one strictly in the future, but still within advance window.
            sched.schedule(fire_at=sched.sim_time + 1, kind="chain", handler=chain)

    s.schedule(fire_at=1, kind="chain", handler=chain)
    s.advance(100)
    assert counter["n"] == 5


def test_advance_to_specific_time():
    s = Scheduler()
    s.schedule(fire_at=10, kind="a")
    s.schedule(fire_at=25, kind="b")
    fired = s.advance_to(20)
    assert [e.kind for e in fired] == ["a"]
    assert s.sim_time == 20


def test_pending_view_does_not_pop():
    s = Scheduler()
    s.schedule(fire_at=10, kind="a")
    s.schedule(fire_at=20, kind="b")
    pending_before = s.pending()
    pending_again = s.pending()
    assert [e.kind for e in pending_before] == ["a", "b"]
    assert [e.kind for e in pending_again] == ["a", "b"]


def test_event_payload_defaults_to_empty_dict():
    s = Scheduler()
    e = s.schedule(fire_at=1, kind="x")
    assert e.payload == {}


def test_event_with_no_handler_just_drops():
    """An event with neither a specific nor default handler is still consumed by drain."""
    s = Scheduler()
    s.schedule(fire_at=1, kind="orphan")
    fired = s.advance(1)
    assert len(fired) == 1
    assert s.peek_next_fire_at() is None


def test_advance_zero_minutes_drains_due_events():
    """advance(0) should still fire any events at the current sim_time."""
    s = Scheduler(start_time=5)
    s.schedule(fire_at=5, kind="now")
    fired = s.advance(0)
    assert [e.kind for e in fired] == ["now"]
