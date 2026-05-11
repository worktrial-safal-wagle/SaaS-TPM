"""`notifications.*` tool operations.

Most notifications arrive as part of every `ToolResult.notifications` bundle.
This tool exposes an explicit on-demand inbox view, plus mark-read.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.scheduler import Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.costs import COST_NOTIF_LIST, COST_NOTIF_MARK_READ
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


class NotificationsListArgs(BaseModel):
    only_unseen: bool = True
    limit: int | None = None


def notifications_list(world: World, scheduler: Scheduler, args: NotificationsListArgs, caller_id: str) -> dict[str, Any]:
    out = []
    for n in sorted(world.notifications.values(), key=lambda n: (-n.created_at, n.id)):
        if n.recipient_id != caller_id:
            continue
        if args.only_unseen and n.seen:
            continue
        out.append(n.model_dump())
        if args.limit is not None and len(out) >= args.limit:
            break
    return {"notifications": out}


class NotificationsMarkReadArgs(BaseModel):
    notification_id: str


def notifications_mark_read(world: World, scheduler: Scheduler, args: NotificationsMarkReadArgs, caller_id: str) -> dict[str, Any]:
    n = world.notifications.get(args.notification_id)
    if n is None:
        raise ToolError(suggest_id("notification", args.notification_id, world.notifications.keys()))
    if n.recipient_id != caller_id:
        raise ToolError(f"notification not addressed to caller")
    world.mark_notification_seen(args.notification_id)
    return {"notification_id": args.notification_id, "seen": True}


def notifications_ops() -> list[ToolOp]:
    return [
        ToolOp("notifications.list", NotificationsListArgs, COST_NOTIF_LIST, notifications_list,
               description="List unseen notifications addressed to you. Most also arrive in each ToolResult's notifications bundle automatically."),
        ToolOp("notifications.mark_read", NotificationsMarkReadArgs, COST_NOTIF_MARK_READ, notifications_mark_read,
               description="Mark a single notification as seen."),
    ]
