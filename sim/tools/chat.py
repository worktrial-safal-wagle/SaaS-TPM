"""`chat.*` tool operations.

Channels are slack-like. DMs are modeled as private channels with `is_dm=True`
and the canonical id `dm.<sorted_a>__<sorted_b>` so they're easy to find.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sim.scheduler import Scheduler
from sim.store import Channel, Message, World
from sim.tools.acl import channel_visible_to
from sim.tools.base import ToolOp
from sim.tools.presence import presence_for_person
from sim.tools.suggest import suggest_id
from sim.tools.costs import (
    COST_CHAT_DM,
    COST_CHAT_LIST,
    COST_CHAT_MARK_READ,
    COST_CHAT_READ,
    cost_chat_send_by_length,
)
from sim.tools.registry import ToolError


# ---------------------------------------------------------------------------
# chat.send
# ---------------------------------------------------------------------------


class ChatSendArgs(BaseModel):
    channel_id: str
    body: str
    mentions: list[str] = Field(default_factory=list)
    thread_root_id: str | None = None


def _next_msg_id(world: World, channel_id: str) -> str:
    n = sum(1 for m in world.messages.values() if m.channel_id == channel_id)
    return f"msg.{channel_id}.{n + 1}"


def chat_send(world: World, scheduler: Scheduler, args: ChatSendArgs, caller_id: str) -> dict[str, Any]:
    if args.channel_id not in world.channels:
        raise ToolError(suggest_id("channel", args.channel_id, world.channels.keys()))
    if not channel_visible_to(world, args.channel_id, caller_id):
        raise ToolError(f"not a member of channel: {args.channel_id}")
    if not args.body.strip():
        raise ToolError("message body cannot be empty")
    for mention in args.mentions:
        if mention not in world.people:
            raise ToolError(suggest_id("person", mention, world.people.keys()))

    message = Message(
        id=_next_msg_id(world, args.channel_id),
        channel_id=args.channel_id,
        sender_id=caller_id,
        body=args.body,
        sim_time=scheduler.sim_time,
        mentions=list(args.mentions),
        thread_root_id=args.thread_root_id,
    )
    world.add_message(message)
    return message.model_dump()


# ---------------------------------------------------------------------------
# chat.dm
# ---------------------------------------------------------------------------


class ChatDmArgs(BaseModel):
    recipient_id: str
    body: str


def _dm_channel_id(a: str, b: str) -> str:
    lo, hi = sorted([a, b])
    return f"dm.{lo}__{hi}"


def _ensure_dm_channel(world: World, a: str, b: str) -> Channel:
    cid = _dm_channel_id(a, b)
    existing = world.get_channel(cid)
    if existing:
        return existing
    return world.add_channel(Channel(
        id=cid, name=f"DM: {a} ↔ {b}",
        members=sorted([a, b]), is_private=True, is_dm=True,
    ))


def chat_dm(world: World, scheduler: Scheduler, args: ChatDmArgs, caller_id: str) -> dict[str, Any]:
    if args.recipient_id not in world.people:
        raise ToolError(suggest_id("person", args.recipient_id, world.people.keys()))
    if args.recipient_id == caller_id:
        raise ToolError("cannot DM yourself")
    if not args.body.strip():
        raise ToolError("message body cannot be empty")
    channel = _ensure_dm_channel(world, caller_id, args.recipient_id)
    message = Message(
        id=_next_msg_id(world, channel.id),
        channel_id=channel.id,
        sender_id=caller_id,
        body=args.body,
        sim_time=scheduler.sim_time,
    )
    world.add_message(message)
    return message.model_dump()


# ---------------------------------------------------------------------------
# chat.read
# ---------------------------------------------------------------------------


class ChatReadArgs(BaseModel):
    channel_id: str | None = None
    # For DMs, you can pass `recipient_id` instead of `channel_id` and the tool
    # resolves to the canonical (alphabetically-sorted) DM channel for the
    # caller-recipient pair. This avoids the wrong-direction `dm.tpm__kai`
    # failure mode where models construct IDs from their own perspective.
    recipient_id: str | None = None
    since: int | None = None
    limit: int | None = None


def _resolve_channel_id(world: World, args: Any, caller_id: str) -> str:
    """Returns the channel_id from args, resolving `recipient_id` for DMs.

    Validates: exactly one of (channel_id, recipient_id) must be set. If
    `recipient_id`, both the recipient and the DM channel must exist —
    the DM is created on first `chat.dm` send, so a read before any DM
    activity returns 'no such channel'.
    """
    if (args.channel_id is None) == (args.recipient_id is None):
        raise ToolError("must provide exactly one of channel_id or recipient_id")
    if args.recipient_id is not None:
        if world.get_person(args.recipient_id) is None:
            raise ToolError(suggest_id("person", args.recipient_id, world.people.keys()))
        return _dm_channel_id(caller_id, args.recipient_id)
    return args.channel_id


def chat_read(world: World, scheduler: Scheduler, args: ChatReadArgs, caller_id: str) -> dict[str, Any]:
    channel_id = _resolve_channel_id(world, args, caller_id)
    if channel_id not in world.channels:
        raise ToolError(suggest_id("channel", channel_id, world.channels.keys()))
    if not channel_visible_to(world, channel_id, caller_id):
        raise ToolError(f"not a member of channel: {channel_id}")
    msgs = world.messages_in_channel(channel_id)
    if args.since is not None:
        msgs = [m for m in msgs if m.sim_time > args.since]
    if args.limit is not None:
        msgs = msgs[-args.limit:]
    # Mark as read for caller (mutation, but free)
    for m in msgs:
        if caller_id not in m.read_by:
            m.read_by.append(caller_id)
    return {"channel_id": channel_id, "messages": [m.model_dump() for m in msgs]}


# ---------------------------------------------------------------------------
# chat.list
# ---------------------------------------------------------------------------


class ChatListArgs(BaseModel):
    include_dms: bool = True


def chat_list(world: World, scheduler: Scheduler, args: ChatListArgs, caller_id: str) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for channel in sorted(world.channels.values(), key=lambda c: c.id):
        if channel.is_dm and not args.include_dms:
            continue
        if not channel_visible_to(world, channel.id, caller_id):
            continue
        entry: dict[str, Any] = {
            "id": channel.id, "name": channel.name, "is_dm": channel.is_dm,
            "is_private": channel.is_private, "members": list(channel.members),
            "topic": channel.topic,
        }
        # Presence for DMs: surface the *other* peer's availability so the
        # agent can decide whether a DM is likely to land vs. wait for them
        # to come out of a meeting. Same shape as `directory.presence`.
        if channel.is_dm:
            peer_id = next((m for m in channel.members if m != caller_id), None)
            if peer_id is not None:
                peer = world.get_person(peer_id)
                if peer is not None:
                    entry["presence"] = presence_for_person(
                        world, peer, scheduler.sim_time,
                    )
        out.append(entry)
    return {"channels": out}


# ---------------------------------------------------------------------------
# chat.mark_read
# ---------------------------------------------------------------------------


class ChatMarkReadArgs(BaseModel):
    channel_id: str | None = None
    recipient_id: str | None = None


def chat_mark_read(world: World, scheduler: Scheduler, args: ChatMarkReadArgs, caller_id: str) -> dict[str, Any]:
    channel_id = _resolve_channel_id(world, args, caller_id)
    if channel_id not in world.channels:
        raise ToolError(suggest_id("channel", channel_id, world.channels.keys()))
    if not channel_visible_to(world, channel_id, caller_id):
        raise ToolError(f"not a member of channel: {channel_id}")
    count = 0
    for m in world.messages_in_channel(channel_id):
        if caller_id not in m.read_by:
            m.read_by.append(caller_id)
            count += 1
    return {"channel_id": channel_id, "marked": count}


# ---------------------------------------------------------------------------
# Op bundle
# ---------------------------------------------------------------------------


def chat_ops() -> list[ToolOp]:
    return [
        ToolOp(name="chat.send", args_model=ChatSendArgs,
               cost=cost_chat_send_by_length, handler=chat_send,
               description="Send a chat message to a channel you're a member of. Cost scales with body length."),
        ToolOp(name="chat.dm", args_model=ChatDmArgs,
               cost=COST_CHAT_DM, handler=chat_dm,
               description="Send a direct message to another person. The DM channel is created on first use."),
        ToolOp(name="chat.read", args_model=ChatReadArgs,
               cost=COST_CHAT_READ, handler=chat_read,
               description="Read messages from a channel. Pass either `channel_id` for any channel, OR `recipient_id` for a DM (resolves to the right DM channel automatically — no need to construct the DM channel ID). Optionally filter by `since` sim_time."),
        ToolOp(name="chat.list", args_model=ChatListArgs,
               cost=COST_CHAT_LIST, handler=chat_list,
               description="List all channels visible to you."),
        ToolOp(name="chat.mark_read", args_model=ChatMarkReadArgs,
               cost=COST_CHAT_MARK_READ, handler=chat_mark_read,
               description="Mark all messages in a channel as read. Pass either `channel_id` or `recipient_id` (same as `chat.read`)."),
    ]
