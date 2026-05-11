from __future__ import annotations

import pytest

from sim.store import (
    CalendarEvent,
    Channel,
    Doc,
    DocVersion,
    Email,
    EmailThread,
    Message,
    Person,
    Task,
    WorkingHours,
    World,
)
from sim.store.world import WorldSnapshot


def make_person(pid: str, **kwargs) -> Person:
    defaults = {"display_name": pid, "role": "engineer"}
    defaults.update(kwargs)
    return Person(id=pid, **defaults)


def test_add_person_and_lookup():
    w = World()
    w.add_person(make_person("person.alice"))
    assert w.get_person("person.alice").display_name == "person.alice"


def test_duplicate_person_rejected():
    w = World()
    w.add_person(make_person("person.alice"))
    with pytest.raises(ValueError):
        w.add_person(make_person("person.alice"))


def test_is_agent_flag_populates_world_agent_id():
    w = World()
    w.add_person(make_person("person.tpm", is_agent=True))
    assert w.agent_id == "person.tpm"


def test_subscribers_receive_typed_events():
    w = World()
    seen: list[tuple[str, dict]] = []
    w.subscribe("person_added", lambda name, payload: seen.append((name, payload)))
    w.add_person(make_person("person.alice"))
    assert seen == [("person_added", {"person_id": "person.alice"})]


def test_multiple_subscribers_receive_in_order():
    w = World()
    calls: list[str] = []
    w.subscribe("person_added", lambda n, p: calls.append("first"))
    w.subscribe("person_added", lambda n, p: calls.append("second"))
    w.add_person(make_person("person.a"))
    assert calls == ["first", "second"]


def test_message_requires_existing_channel_and_sender():
    w = World()
    w.add_person(make_person("person.alice"))
    with pytest.raises(ValueError):
        w.add_message(Message(
            id="msg.1", channel_id="channel.unknown", sender_id="person.alice",
            body="hi", sim_time=0,
        ))


def test_message_emits_event():
    w = World()
    w.add_person(make_person("person.alice"))
    w.add_channel(Channel(id="channel.general", name="general", members=["person.alice"]))
    seen: list[dict] = []
    w.subscribe("message_inserted", lambda n, p: seen.append(p))
    w.add_message(Message(
        id="msg.1", channel_id="channel.general", sender_id="person.alice",
        body="hello", sim_time=10, mentions=["person.bob"],
    ))
    assert seen == [{
        "message_id": "msg.1",
        "channel_id": "channel.general",
        "sender_id": "person.alice",
        "mentions": ["person.bob"],
        "sim_time": 10,
    }]


def test_messages_in_channel_sorted_by_sim_time():
    w = World()
    w.add_person(make_person("person.alice"))
    w.add_channel(Channel(id="channel.general", name="general", members=["person.alice"]))
    w.add_message(Message(id="msg.b", channel_id="channel.general",
                          sender_id="person.alice", body="b", sim_time=20))
    w.add_message(Message(id="msg.a", channel_id="channel.general",
                          sender_id="person.alice", body="a", sim_time=10))
    out = w.messages_in_channel("channel.general")
    assert [m.id for m in out] == ["msg.a", "msg.b"]


def test_email_requires_existing_thread():
    w = World()
    w.add_person(make_person("person.alice"))
    with pytest.raises(ValueError):
        w.add_email(Email(
            id="email.1", thread_id="thread.unknown", sender_id="person.alice",
            to=["person.bob"], body="hi", sim_time=0,
        ))


def test_task_status_transition_emits_from_to():
    w = World()
    w.add_task(Task(id="task.PROJ-1", project="proj", title="t", status="Todo"))
    seen: list[dict] = []
    w.subscribe("task_status_changed", lambda n, p: seen.append(p))
    w.update_task_status("task.PROJ-1", "In Progress")
    assert seen == [{"task_id": "task.PROJ-1", "from": "Todo", "to": "In Progress"}]


def test_log_work_decrements_remaining_effort():
    w = World()
    w.add_task(Task(
        id="task.x", project="p", title="t",
        estimated_effort_seconds=3600, remaining_effort_seconds=3600,
    ))
    w.log_work("task.x", 900)
    assert w.tasks["task.x"].remaining_effort_seconds == 2700


def test_log_work_does_not_underflow():
    w = World()
    w.add_task(Task(id="task.x", project="p", title="t",
                    estimated_effort_seconds=600, remaining_effort_seconds=600))
    w.log_work("task.x", 9999)
    assert w.tasks["task.x"].remaining_effort_seconds == 0


def test_log_work_requires_remaining_effort_set():
    w = World()
    w.add_task(Task(id="task.x", project="p", title="t"))
    with pytest.raises(ValueError):
        w.log_work("task.x", 100)


def test_doc_versions_must_be_sequential():
    w = World()
    w.add_doc(Doc(id="doc.spec", title="Spec", created_by="person.alice"))
    w.add_doc_version("doc.spec", DocVersion(
        version=1, author_id="person.alice", body="v1", sim_time=10,
    ))
    with pytest.raises(ValueError):
        w.add_doc_version("doc.spec", DocVersion(
            version=3, author_id="person.alice", body="v3", sim_time=20,
        ))
    w.add_doc_version("doc.spec", DocVersion(
        version=2, author_id="person.alice", body="v2", sim_time=20,
    ))
    assert [v.version for v in w.docs["doc.spec"].versions] == [1, 2]


def test_calendar_event_end_must_be_after_start():
    w = World()
    with pytest.raises(ValueError):
        w.add_calendar_event(CalendarEvent(
            id="cal.1", title="t",
            start_sim_time=100, end_sim_time=100,
            organizer_id="person.alice",
        ))


def test_snapshot_round_trip_byte_equal():
    w = World(scenario_id="smoke", seed=42)
    w.add_person(make_person("person.alice"))
    w.add_person(make_person("person.bob"))
    w.add_channel(Channel(id="channel.general", name="general",
                          members=["person.alice", "person.bob"]))
    w.add_message(Message(id="msg.1", channel_id="channel.general",
                          sender_id="person.alice", body="hi", sim_time=10))
    w.add_task(Task(id="task.X", project="proj", title="t", status="Todo"))

    raw1 = w.to_json()
    w2 = World.from_json(raw1)
    raw2 = w2.to_json()
    assert raw1 == raw2


def test_snapshot_messages_sorted_for_determinism():
    """Snapshot ordering must be stable regardless of insertion order."""
    w1 = World()
    w2 = World()
    for w in (w1, w2):
        w.add_person(make_person("person.alice"))
        w.add_channel(Channel(id="channel.general", name="general",
                              members=["person.alice"]))
    w1.add_message(Message(id="msg.a", channel_id="channel.general",
                           sender_id="person.alice", body="a", sim_time=10))
    w1.add_message(Message(id="msg.b", channel_id="channel.general",
                           sender_id="person.alice", body="b", sim_time=20))
    # Insert in reverse order
    w2.add_message(Message(id="msg.b", channel_id="channel.general",
                           sender_id="person.alice", body="b", sim_time=20))
    w2.add_message(Message(id="msg.a", channel_id="channel.general",
                           sender_id="person.alice", body="a", sim_time=10))
    assert w1.to_json() == w2.to_json()


def test_seed_insert_and_tool_insert_emit_same_event():
    """Composability invariant: a seed message and an agent message take the
    exact same code path."""
    captured: list[dict] = []
    w = World()
    w.add_person(make_person("person.alice"))
    w.add_channel(Channel(id="channel.x", name="x", members=["person.alice"]))
    w.subscribe("message_inserted", lambda n, p: captured.append(p))

    # "Seed" path
    w.add_message(Message(id="msg.seed", channel_id="channel.x",
                          sender_id="person.alice", body="seeded", sim_time=0))
    # "Tool" path - same call, different ergonomic origin
    w.add_message(Message(id="msg.tool", channel_id="channel.x",
                          sender_id="person.alice", body="from agent", sim_time=5))

    assert len(captured) == 2
    assert captured[0]["message_id"] == "msg.seed"
    assert captured[1]["message_id"] == "msg.tool"
    # Both events have identical shape
    assert set(captured[0].keys()) == set(captured[1].keys())
