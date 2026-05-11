from __future__ import annotations

import pytest

from sim.scheduler import Scheduler
from sim.store import Channel, Person, World
from sim.store.notifications import wire_notification_dispatcher
from sim.tools import ToolCall, ToolRegistry
from sim.tools.chat import chat_ops


def _world_with_two_people() -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.maya", display_name="Maya", role="engineer"))
    world.add_channel(Channel(
        id="channel.eng", name="eng",
        members=["person.tpm", "person.maya"],
    ))
    world.add_channel(Channel(
        id="channel.exec", name="exec",
        members=["person.maya"], is_private=True,
    ))
    wire_notification_dispatcher(world)
    registry = ToolRegistry(world, scheduler, caller_id="person.tpm")
    registry.register_all(chat_ops())
    return world, scheduler, registry


def test_chat_send_creates_message_and_advances_clock():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng", "body": "hello team"},
    ))
    assert result.ok is True
    assert result.cost_minutes == 1
    assert result.sim_time == 1
    assert result.result["sender_id"] == "person.tpm"
    assert result.result["body"] == "hello team"


def test_chat_send_long_message_costs_more():
    world, scheduler, registry = _world_with_two_people()
    body = "a" * 500  # 500 chars → ceil(500/200)=3 min
    result = registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng", "body": body},
    ))
    assert result.ok
    assert result.cost_minutes == 3


def test_chat_send_to_private_channel_member_only_rejected():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.exec", "body": "hi"},
    ))
    assert result.ok is False
    assert "not a member" in result.error


def test_chat_send_empty_body_rejected():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng", "body": "   "},
    ))
    assert result.ok is False
    assert "empty" in result.error


def test_chat_send_unknown_mention_rejected():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng", "body": "hi", "mentions": ["person.ghost"]},
    ))
    assert result.ok is False
    assert "unknown person" in result.error


def test_chat_dm_creates_channel_and_message():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.dm",
        args={"recipient_id": "person.maya", "body": "private hi"},
    ))
    assert result.ok
    # DM channel exists with canonical id
    assert "dm.person.maya__person.tpm" in world.channels or "dm.person.tpm__person.maya" in world.channels
    # Maya should have a notification
    maya_notifs = [n for n in world.notifications.values() if n.recipient_id == "person.maya"]
    assert len(maya_notifs) == 1
    assert maya_notifs[0].kind == "chat_message"


def test_chat_dm_to_self_rejected():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.dm",
        args={"recipient_id": "person.tpm", "body": "self talk"},
    ))
    assert result.ok is False


def test_chat_read_respects_acl():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.read",
        args={"channel_id": "channel.exec"},
    ))
    assert result.ok is False
    assert "not a member" in result.error


def test_chat_read_returns_messages_since():
    world, scheduler, registry = _world_with_two_people()
    # Send three messages
    for i in range(3):
        registry.dispatch(ToolCall(
            tool="chat.send",
            args={"channel_id": "channel.eng", "body": f"msg{i}"},
        ))
    # Read all
    result = registry.dispatch(ToolCall(
        tool="chat.read",
        args={"channel_id": "channel.eng"},
    ))
    assert result.ok
    assert len(result.result["messages"]) == 3
    # Each send: handler timestamps the message at current sim_time,
    # then registry advances by cost. So msg0 is at t=0, msg1 at t=1, msg2 at t=2.
    # `since=0` returns messages strictly after t=0 → msg1 and msg2.
    result = registry.dispatch(ToolCall(
        tool="chat.read",
        args={"channel_id": "channel.eng", "since": 0},
    ))
    assert result.ok
    bodies = [m["body"] for m in result.result["messages"]]
    assert "msg0" not in bodies
    assert "msg1" in bodies
    assert "msg2" in bodies


def test_chat_list_filters_to_visible_channels():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(tool="chat.list", args={}))
    assert result.ok
    channel_ids = {c["id"] for c in result.result["channels"]}
    assert "channel.eng" in channel_ids
    assert "channel.exec" not in channel_ids  # private, TPM not member


def test_chat_mention_creates_notification_for_mentioned_person():
    world, scheduler, registry = _world_with_two_people()
    registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng", "body": "@maya please look",
              "mentions": ["person.maya"]},
    ))
    maya_notifs = [n for n in world.notifications.values() if n.recipient_id == "person.maya"]
    assert len(maya_notifs) == 1


def test_chat_public_message_no_mention_no_notification():
    world, scheduler, registry = _world_with_two_people()
    registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng", "body": "broadcast"},
    ))
    # No notification on a public channel without mention
    other_notifs = [n for n in world.notifications.values() if n.recipient_id != "person.tpm"]
    assert other_notifs == []


def test_invalid_args_returns_structured_error():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.send",
        args={"channel_id": "channel.eng"},  # missing body
    ))
    assert result.ok is False
    assert "invalid args" in result.error.lower()


def test_unknown_tool_returns_structured_error():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(tool="chat.delete", args={}))
    assert result.ok is False
    assert "unknown tool" in result.error
