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


# ---------------------------------------------------------------------------
# DM resolution via recipient_id (Option B fix for canonical-direction errors)
# ---------------------------------------------------------------------------


def test_chat_read_by_recipient_id_resolves_dm_channel():
    """Agent can call chat.read with recipient_id and skip channel_id entirely."""
    world, scheduler, registry = _world_with_two_people()
    # Send a DM first so the channel exists.
    registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "hi maya"},
    ))
    # Read without constructing a channel_id.
    result = registry.dispatch(ToolCall(
        tool="chat.read", args={"recipient_id": "person.maya"},
    ))
    assert result.ok, result.error
    assert len(result.result["messages"]) == 1
    assert result.result["messages"][0]["body"] == "hi maya"
    # The resolved channel_id is the canonical alphabetically-sorted form.
    assert result.result["channel_id"] == "dm.person.maya__person.tpm"


def test_chat_read_by_recipient_id_works_regardless_of_id_construction():
    """The whole point: agent can't accidentally pass dm.tpm__maya (wrong direction)."""
    world, scheduler, registry = _world_with_two_people()
    registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "ping"},
    ))
    # Even though "tpm" sorts AFTER "maya", recipient_id resolution doesn't care.
    result = registry.dispatch(ToolCall(
        tool="chat.read", args={"recipient_id": "person.maya"},
    ))
    assert result.ok


def test_chat_read_rejects_both_channel_and_recipient():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.read",
        args={"channel_id": "channel.eng", "recipient_id": "person.maya"},
    ))
    assert not result.ok
    assert "exactly one" in result.error


def test_chat_read_rejects_neither_channel_nor_recipient():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(tool="chat.read", args={}))
    assert not result.ok
    assert "exactly one" in result.error


def test_chat_read_by_recipient_id_unknown_person():
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(
        tool="chat.read", args={"recipient_id": "person.ghost"},
    ))
    assert not result.ok
    assert "person" in result.error.lower()


def test_chat_mark_read_by_recipient_id():
    world, scheduler, registry = _world_with_two_people()
    registry.dispatch(ToolCall(
        tool="chat.dm", args={"recipient_id": "person.maya", "body": "hi"},
    ))
    result = registry.dispatch(ToolCall(
        tool="chat.mark_read", args={"recipient_id": "person.maya"},
    ))
    assert result.ok
    assert result.result["channel_id"] == "dm.person.maya__person.tpm"


def test_chat_read_with_channel_id_still_works():
    """Regression: existing channel_id usage isn't broken."""
    world, scheduler, registry = _world_with_two_people()
    registry.dispatch(ToolCall(
        tool="chat.send", args={"channel_id": "channel.eng", "body": "in eng"},
    ))
    result = registry.dispatch(ToolCall(
        tool="chat.read", args={"channel_id": "channel.eng"},
    ))
    assert result.ok
    assert len(result.result["messages"]) == 1


# ---------------------------------------------------------------------------
# Presence surfacing on chat.list (Phase 5)
# ---------------------------------------------------------------------------


def _dm_channel(world: World, a: str, b: str) -> str:
    """Helper: create a DM channel and return its canonical id."""
    from sim.tools.chat import _dm_channel_id
    cid = _dm_channel_id(a, b)
    if cid not in world.channels:
        world.add_channel(Channel(
            id=cid, name=f"DM: {a} <-> {b}",
            members=sorted([a, b]), is_private=True, is_dm=True,
        ))
    return cid


def test_chat_list_includes_presence_for_dm_peer_available():
    """For a DM channel, chat.list reports presence for the *other* member.
    With no busy_until set, in_meeting is False."""
    world, scheduler, registry = _world_with_two_people()
    _dm_channel(world, "person.tpm", "person.maya")
    result = registry.dispatch(ToolCall(tool="chat.list", args={}))
    assert result.ok
    dms = [c for c in result.result["channels"] if c["is_dm"]]
    assert len(dms) == 1
    presence = dms[0].get("presence")
    assert presence is not None, "DM channel should carry a presence block"
    assert presence == {
        "in_meeting": False,
        "available_at": None,
        "current_event_title": None,
    }


def test_chat_list_presence_reports_in_meeting_when_busy():
    """If the DM peer's busy_until > now, presence reports in_meeting=True
    and available_at is the unblock sim_time."""
    world, scheduler, registry = _world_with_two_people()
    _dm_channel(world, "person.tpm", "person.maya")
    world.people["person.maya"].busy_until = 60
    result = registry.dispatch(ToolCall(tool="chat.list", args={}))
    assert result.ok
    dms = [c for c in result.result["channels"] if c["is_dm"]]
    assert dms[0]["presence"]["in_meeting"] is True
    assert dms[0]["presence"]["available_at"] == 60


def test_chat_list_presence_reports_current_event_title():
    """If a calendar event covers `now` for the DM peer, the event title is
    included in the presence projection."""
    from sim.store import CalendarEvent
    world, scheduler, registry = _world_with_two_people()
    _dm_channel(world, "person.tpm", "person.maya")
    world.add_calendar_event(CalendarEvent(
        id="cal.launch_review", title="Launch review",
        start_sim_time=0, end_sim_time=60,
        organizer_id="person.tpm", attendees=["person.maya"],
    ))
    world.people["person.maya"].busy_until = 60
    result = registry.dispatch(ToolCall(tool="chat.list", args={}))
    assert result.ok
    dms = [c for c in result.result["channels"] if c["is_dm"]]
    assert dms[0]["presence"]["current_event_title"] == "Launch review"


def test_chat_list_non_dm_channels_have_no_presence():
    """Presence is only meaningful for DM channels (one peer). Public/team
    channels don't get a presence block."""
    world, scheduler, registry = _world_with_two_people()
    result = registry.dispatch(ToolCall(tool="chat.list", args={}))
    assert result.ok
    eng = [c for c in result.result["channels"] if c["id"] == "channel.eng"][0]
    assert "presence" not in eng
