from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Person, World
from sim.store.notifications import wire_notification_dispatcher
from sim.tools import ToolCall, ToolRegistry
from sim.tools.email import email_ops


def _setup() -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.ceo", display_name="CEO", role="ceo"))
    world.add_person(Person(id="person.maya", display_name="Maya", role="engineer"))
    wire_notification_dispatcher(world)
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(email_ops())
    return world, scheduler, reg


def test_email_send_starts_new_thread():
    world, scheduler, reg = _setup()
    result = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "subject": "Launch status",
              "body": "All systems green. Shipping Friday."},
    ))
    assert result.ok
    assert result.result["subject"] == "Launch status"
    thread_id = result.result["thread_id"]
    assert thread_id in world.email_threads
    # CEO has an unread notification
    ceo_notifs = [n for n in world.notifications.values() if n.recipient_id == "person.ceo"]
    assert len(ceo_notifs) == 1
    assert ceo_notifs[0].kind == "email"


def test_email_send_cost_scales_with_body_length():
    world, scheduler, reg = _setup()
    short = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "subject": "Short", "body": "hi"},
    ))
    assert short.cost_minutes == 3  # base only
    long_body = "x" * 900  # 600 chars over base → 2 extra mins
    long = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "subject": "Long", "body": long_body},
    ))
    assert long.cost_minutes == 3 + 2  # base + 2 mins for length


def test_email_reply_threads_correctly():
    world, scheduler, reg = _setup()
    first = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "subject": "Q", "body": "ping"},
    ))
    thread_id = first.result["thread_id"]
    reply = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "body": "follow up", "thread_id": thread_id},
    ))
    assert reply.ok
    assert reply.result["thread_id"] == thread_id
    # Two emails in the thread
    assert len(world.emails_in_thread(thread_id)) == 2


def test_email_send_missing_subject_when_starting_new_thread():
    world, scheduler, reg = _setup()
    result = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "body": "no subject"},
    ))
    assert result.ok is False
    assert "subject required" in result.error


def test_email_send_unknown_recipient_rejected():
    world, scheduler, reg = _setup()
    result = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ghost"], "subject": "x", "body": "x"},
    ))
    assert result.ok is False
    assert "unknown person" in result.error
    assert "person.ghost" in result.error


def test_email_list_filters_to_caller_and_orders_newest_first():
    world, scheduler, reg = _setup()
    # CEO writes to TPM
    ceo_reg = ToolRegistry(world, scheduler, caller_id="person.ceo")
    ceo_reg.register_all(email_ops())
    ceo_reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.tpm"], "subject": "Status?", "body": "today?"},
    ))
    ceo_reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.tpm"], "subject": "Again", "body": "now?"},
    ))
    result = reg.dispatch(ToolCall(tool="email.list", args={}))
    assert result.ok
    emails = result.result["emails"]
    assert len(emails) == 2
    assert emails[0]["sim_time"] >= emails[1]["sim_time"]


def test_email_read_marks_thread_read():
    world, scheduler, reg = _setup()
    ceo_reg = ToolRegistry(world, scheduler, caller_id="person.ceo")
    ceo_reg.register_all(email_ops())
    sent = ceo_reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.tpm"], "subject": "Q", "body": "hi"},
    ))
    thread_id = sent.result["thread_id"]
    result = reg.dispatch(ToolCall(tool="email.read", args={"thread_id": thread_id}))
    assert result.ok
    assert result.result["subject"] == "Q"
    # Now email.list should show no unread
    listed = reg.dispatch(ToolCall(tool="email.list", args={"only_unread": True}))
    assert listed.result["emails"] == []


def test_email_read_acl_blocks_non_participant():
    world, scheduler, reg = _setup()
    maya_reg = ToolRegistry(world, scheduler, caller_id="person.maya")
    maya_reg.register_all(email_ops())
    sent = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "subject": "secret", "body": "stuff"},
    ))
    thread_id = sent.result["thread_id"]
    result = maya_reg.dispatch(ToolCall(tool="email.read", args={"thread_id": thread_id}))
    assert result.ok is False
    assert "not a participant" in result.error
