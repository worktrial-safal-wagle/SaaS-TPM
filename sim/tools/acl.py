"""Access-control helpers used by tool handlers.

Tools enforce ACL at the boundary so neither the agent nor an NPC can see
data they shouldn't. Helpers here are pure functions on World state.
"""

from __future__ import annotations

from sim.store import World


def channel_visible_to(world: World, channel_id: str, person_id: str) -> bool:
    """True iff `person_id` is a member of `channel_id` (or the channel is public)."""
    channel = world.get_channel(channel_id)
    if channel is None:
        return False
    if not channel.is_private:
        return True
    return person_id in channel.members


def doc_view_allowed(world: World, doc_id: str, person_id: str) -> bool:
    doc = world.docs.get(doc_id)
    if doc is None:
        return False
    if doc.acl_view is None:
        return True
    return person_id in doc.acl_view


def doc_edit_allowed(world: World, doc_id: str, person_id: str) -> bool:
    doc = world.docs.get(doc_id)
    if doc is None:
        return False
    allow = doc.acl_edit if doc.acl_edit is not None else doc.acl_view
    if allow is None:
        return True
    return person_id in allow
