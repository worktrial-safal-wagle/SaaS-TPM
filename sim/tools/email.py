"""`email.*` tool operations.

Email threads are first-class. `email.send` either replies into an existing
thread (`thread_id` given) or starts a new one with the given subject.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sim.scheduler import Scheduler
from sim.store import Email, EmailThread, World
from sim.tools.base import ToolOp
from sim.tools.costs import COST_EMAIL_LIST, COST_EMAIL_READ, cost_email_send
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


# ---------------------------------------------------------------------------
# email.send
# ---------------------------------------------------------------------------


class EmailSendArgs(BaseModel):
    to: list[str] = Field(min_length=1)
    cc: list[str] = Field(default_factory=list)
    subject: str | None = None  # required when starting a new thread
    body: str
    thread_id: str | None = None  # if set, this is a reply


def _next_email_id(world: World) -> str:
    return f"email.{len(world.emails) + 1}"


def _next_thread_id(world: World) -> str:
    return f"thread.{len(world.email_threads) + 1}"


def email_send(world: World, scheduler: Scheduler, args: EmailSendArgs, caller_id: str) -> dict[str, Any]:
    if not args.body.strip():
        raise ToolError("email body cannot be empty")
    for rid in (*args.to, *args.cc):
        if rid not in world.people:
            raise ToolError(suggest_id("person", rid, world.people.keys()))

    if args.thread_id:
        thread = world.email_threads.get(args.thread_id)
        if thread is None:
            raise ToolError(suggest_id("thread", args.thread_id, world.email_threads.keys()))
    else:
        if not args.subject:
            raise ToolError("subject required when starting a new thread")
        thread = EmailThread(
            id=_next_thread_id(world),
            subject=args.subject,
            participants=sorted({caller_id, *args.to, *args.cc}),
        )
        world.add_email_thread(thread)

    # Ensure caller and recipients are recorded as thread participants
    updated_participants = sorted(set(thread.participants) | {caller_id, *args.to, *args.cc})
    thread.participants = updated_participants

    email = Email(
        id=_next_email_id(world),
        thread_id=thread.id,
        sender_id=caller_id,
        to=list(args.to),
        cc=list(args.cc),
        body=args.body,
        sim_time=scheduler.sim_time,
    )
    world.add_email(email)
    return {"email": email.model_dump(), "thread_id": thread.id, "subject": thread.subject}


# ---------------------------------------------------------------------------
# email.list
# ---------------------------------------------------------------------------


class EmailListArgs(BaseModel):
    only_unread: bool = False
    since: int | None = None


def email_list(world: World, scheduler: Scheduler, args: EmailListArgs, caller_id: str) -> dict[str, Any]:
    out = []
    for email in sorted(world.emails.values(), key=lambda e: (-e.sim_time, e.id)):
        if caller_id not in email.to and caller_id not in email.cc:
            continue
        if args.since is not None and email.sim_time <= args.since:
            continue
        unread = caller_id not in email.read_by
        if args.only_unread and not unread:
            continue
        thread = world.email_threads.get(email.thread_id)
        out.append({
            "email_id": email.id,
            "thread_id": email.thread_id,
            "subject": thread.subject if thread else "",
            "sender_id": email.sender_id,
            "sim_time": email.sim_time,
            "unread": unread,
        })
    return {"emails": out}


# ---------------------------------------------------------------------------
# email.read
# ---------------------------------------------------------------------------


class EmailReadArgs(BaseModel):
    thread_id: str


def email_read(world: World, scheduler: Scheduler, args: EmailReadArgs, caller_id: str) -> dict[str, Any]:
    thread = world.email_threads.get(args.thread_id)
    if thread is None:
        raise ToolError(suggest_id("thread", args.thread_id, world.email_threads.keys()))
    if caller_id not in thread.participants:
        raise ToolError(f"not a participant of thread: {args.thread_id}")
    emails = world.emails_in_thread(args.thread_id)
    for email in emails:
        if caller_id not in email.read_by:
            email.read_by.append(caller_id)
    return {
        "thread_id": thread.id,
        "subject": thread.subject,
        "emails": [e.model_dump() for e in emails],
    }


# ---------------------------------------------------------------------------
# Op bundle
# ---------------------------------------------------------------------------


def email_ops() -> list[ToolOp]:
    return [
        ToolOp("email.send", EmailSendArgs, cost_email_send, email_send,
               description="Send an email. Pass `thread_id` to reply into an existing thread; otherwise `subject` is required."),
        ToolOp("email.list", EmailListArgs, COST_EMAIL_LIST, email_list,
               description="List your inbox (newest first). `only_unread=true` filters."),
        ToolOp("email.read", EmailReadArgs, COST_EMAIL_READ, email_read,
               description="Read all emails in a thread; marks them as read for you."),
    ]
