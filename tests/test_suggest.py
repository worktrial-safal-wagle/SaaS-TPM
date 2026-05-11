from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Channel, Person, World
from sim.store.notifications import wire_notification_dispatcher
from sim.tools import ToolCall, ToolRegistry
from sim.tools.chat import chat_ops
from sim.tools.email import email_ops
from sim.tools.suggest import suggest_id


def test_suggest_id_returns_close_match():
    msg = suggest_id("channel", "general",
                     ["channel.general", "channel.engineering", "channel.audit-log"])
    assert "channel.general" in msg
    assert "did you mean" in msg


def test_suggest_id_falls_back_to_sample_when_no_close_match():
    msg = suggest_id("thread", "completely_unrelated_thing",
                     ["thread.welcome", "thread.q2_roadmap", "thread.legal_review"])
    assert "no close match" in msg
    assert "thread.welcome" in msg  # sample shows real ids


def test_unknown_channel_error_contains_suggestion():
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_channel(Channel(id="channel.general", name="general", members=["person.tpm"]))
    wire_notification_dispatcher(world)
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(chat_ops())
    result = reg.dispatch(ToolCall(
        tool="chat.send", args={"channel_id": "general", "body": "hi"},
    ))
    assert result.ok is False
    # The error should suggest the right id
    assert "channel.general" in result.error
    assert "did you mean" in result.error


def test_unknown_thread_error_contains_suggestion():
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.ceo", display_name="CEO", role="ceo"))
    wire_notification_dispatcher(world)
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(email_ops())
    # First, send to create a real thread
    sent = reg.dispatch(ToolCall(
        tool="email.send",
        args={"to": ["person.ceo"], "subject": "welcome", "body": "hi"},
    ))
    real_thread_id = sent.result["thread_id"]
    # Now request a similar-but-wrong id
    result = reg.dispatch(ToolCall(
        tool="email.read", args={"thread_id": "thread.welcom"},  # typo
    ))
    assert result.ok is False
    assert real_thread_id in result.error
    assert "did you mean" in result.error
