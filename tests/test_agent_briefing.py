"""Tests for `BriefingAssembler.build_delta_briefing` (Phase 5).

The delta briefing is the "react to what's new" surface used on subsequent
ticks. Same Briefing pydantic shape as the full `build(...)`, but filtered
to items with sim_time > since.

Tick 1 still uses the full `build(...)` — those tests live in
`test_agent_driver.py`. Here we focus on the delta-filter logic plus the
new `current_busy_state` field.
"""

from __future__ import annotations

from sim.agent.briefing import BriefingAssembler, render_briefing
from sim.scheduler import Scheduler
from sim.store import (
    CalendarEvent, Channel, Email, EmailThread, Message, Notification,
    Person, Task, World,
)


def _seed_world() -> tuple[World, Scheduler]:
    """A small but realistic world: TPM agent, two NPC peers, a couple of
    channels, no scenario_origin assumption (zero is fine for these tests)."""
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(
        id="person.tpm", display_name="TPM", role="tpm",
        team="exec", is_agent=True,
    ))
    world.add_person(Person(
        id="person.kai", display_name="Kai", role="eng", team="eng",
    ))
    world.add_person(Person(
        id="person.maya", display_name="Maya", role="eng", team="eng",
    ))
    world.add_channel(Channel(
        id="channel.eng", name="eng",
        members=["person.tpm", "person.kai", "person.maya"],
    ))
    return world, scheduler


# ---------------------------------------------------------------------------
# Delta-filter: messages
# ---------------------------------------------------------------------------


def test_delta_messages_before_since_are_excluded():
    """A message sent before `since` shouldn't appear in the delta."""
    world, scheduler = _seed_world()
    world.add_message(Message(
        id="msg.1", channel_id="channel.eng", sender_id="person.kai",
        body="old news", sim_time=5,
    ))
    # Advance scheduler so the assembler's "now" is sensible.
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    flat = [m for ch in delta.unread_chats for m in ch["messages"]]
    assert all(m["id"] != "msg.1" for m in flat), \
        "messages at or before since should be excluded"


def test_delta_messages_after_since_are_included():
    """A message sent strictly after `since` shows up in delta.unread_chats."""
    world, scheduler = _seed_world()
    world.add_message(Message(
        id="msg.new", channel_id="channel.eng", sender_id="person.kai",
        body="just now", sim_time=15,
    ))
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    flat = [m for ch in delta.unread_chats for m in ch["messages"]]
    assert any(m["id"] == "msg.new" for m in flat), \
        "messages after since should be included"


def test_delta_excludes_messages_sent_by_actor_themselves():
    """The agent doesn't need to react to their own outbound messages — the
    delta only highlights inbound activity."""
    world, scheduler = _seed_world()
    world.add_message(Message(
        id="msg.self", channel_id="channel.eng", sender_id="person.tpm",
        body="hi team", sim_time=15,
    ))
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    flat = [m for ch in delta.unread_chats for m in ch["messages"]]
    assert all(m["id"] != "msg.self" for m in flat)


def test_delta_skips_private_channels_actor_isnt_in():
    """ACL still applies to deltas: a private channel the actor isn't a
    member of contributes nothing, even if there are new messages."""
    world, scheduler = _seed_world()
    world.add_channel(Channel(
        id="channel.exec", name="exec",
        members=["person.maya"], is_private=True,
    ))
    world.add_message(Message(
        id="msg.secret", channel_id="channel.exec", sender_id="person.maya",
        body="just for execs", sim_time=15,
    ))
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    channel_ids = {ch["channel_id"] for ch in delta.unread_chats}
    assert "channel.exec" not in channel_ids


# ---------------------------------------------------------------------------
# Delta-filter: emails
# ---------------------------------------------------------------------------


def test_delta_emails_filter_by_since_and_recipient():
    """Emails to/cc the actor with sim_time > since show up; older or
    unrelated emails don't."""
    world, scheduler = _seed_world()
    world.add_email_thread(EmailThread(
        id="thread.1", subject="Audit",
        participants=["person.tpm", "person.kai"],
    ))
    # Old email — before since
    world.add_email(Email(
        id="email.old", thread_id="thread.1", sender_id="person.kai",
        to=["person.tpm"], body="old", sim_time=5,
    ))
    # New email — after since
    world.add_email(Email(
        id="email.new", thread_id="thread.1", sender_id="person.kai",
        to=["person.tpm"], body="new", sim_time=15,
    ))
    # New email but to someone else — should not surface
    world.add_email_thread(EmailThread(
        id="thread.2", subject="Unrelated", participants=["person.kai", "person.maya"],
    ))
    world.add_email(Email(
        id="email.other", thread_id="thread.2", sender_id="person.maya",
        to=["person.kai"], body="not for tpm", sim_time=15,
    ))
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    thread_ids = {e["thread_id"] for e in delta.unread_emails}
    assert "thread.1" in thread_ids
    # email.old was before since, but thread.1 surfaces from email.new (the
    # newest hit per thread). So thread.1 is in delta — but never from old.
    assert "thread.2" not in thread_ids
    # The reported email_id should be the newest, not the old one.
    [hit] = [e for e in delta.unread_emails if e["thread_id"] == "thread.1"]
    assert hit["email_id"] == "email.new"


# ---------------------------------------------------------------------------
# Delta-filter: task board (status changes)
# ---------------------------------------------------------------------------


def test_delta_task_board_includes_status_changes_since():
    """A task whose status was updated after `since` shows up in the delta
    project board."""
    world, scheduler = _seed_world()
    world.add_task(Task(
        id="task.X-1", project="P", title="x1", assignee_id="person.kai",
        priority="P1",
    ))
    # Build the assembler *first* so it subscribes before we mutate.
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    # Advance time so the status change is stamped at sim_time=15.
    scheduler.advance(15)
    world.update_task_status("task.X-1", "In Progress")
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    ids = [t.task_id for t in delta.project_board]
    assert "task.X-1" in ids


def test_delta_task_board_excludes_status_changes_before_since():
    """A status change stamped at or before `since` is filtered out."""
    world, scheduler = _seed_world()
    world.add_task(Task(
        id="task.X-1", project="P", title="x1", assignee_id="person.kai",
        priority="P1",
    ))
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    scheduler.advance(5)
    world.update_task_status("task.X-1", "In Progress")  # stamped at 5
    scheduler.advance(20)  # now at 25
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    ids = [t.task_id for t in delta.project_board]
    assert "task.X-1" not in ids, \
        "status change before since should not surface in the delta"


def test_delta_task_board_falls_back_to_comments_without_scheduler():
    """Without a scheduler wired in, the assembler can't observe live status
    events. It falls back to using the newest comment's sim_time as a proxy
    for 'this task moved'."""
    world, scheduler = _seed_world()
    # Note: scheduler intentionally NOT passed to the assembler.
    assembler = BriefingAssembler(world, end_sim_time=480)
    from sim.store.entities import TaskComment
    world.add_task(Task(
        id="task.X-1", project="P", title="x1", assignee_id="person.kai",
        priority="P1",
        comments=[TaskComment(author_id="person.kai", body="moved", sim_time=15)],
    ))
    delta = assembler.build_delta_briefing("person.tpm", since=10, now_sim_time=20)
    ids = [t.task_id for t in delta.project_board]
    assert "task.X-1" in ids


# ---------------------------------------------------------------------------
# Calendar window: independent of `since`
# ---------------------------------------------------------------------------


def test_delta_calendar_shows_next_4_hours():
    """Calendar events in the next 4 sim-hours appear regardless of `since`
    — the agent always wants the forward window."""
    world, scheduler = _seed_world()
    # Now = 30; window = [30, 30+240] = [30, 270]
    world.add_calendar_event(CalendarEvent(
        id="cal.soon", title="Soon",
        start_sim_time=60, end_sim_time=120,
        organizer_id="person.tpm", attendees=["person.tpm"],
    ))
    world.add_calendar_event(CalendarEvent(
        id="cal.far", title="Far away",
        start_sim_time=500, end_sim_time=560,
        organizer_id="person.tpm", attendees=["person.tpm"],
    ))
    scheduler.advance(30)
    assembler = BriefingAssembler(world, end_sim_time=2000, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    titles = {e["title"] for e in delta.upcoming_calendar}
    assert "Soon" in titles
    assert "Far away" not in titles


# ---------------------------------------------------------------------------
# Notifications delta
# ---------------------------------------------------------------------------


def test_delta_notifications_new_only():
    """Only unseen notifications with created_at > since appear."""
    world, scheduler = _seed_world()
    world.add_notification(Notification(
        id="notif.old", recipient_id="person.tpm", kind="x",
        payload={}, created_at=5,
    ))
    world.add_notification(Notification(
        id="notif.new", recipient_id="person.tpm", kind="x",
        payload={"why": "fresh"}, created_at=15,
    ))
    # A new notification for someone else — should not appear.
    world.add_notification(Notification(
        id="notif.other", recipient_id="person.kai", kind="x",
        payload={}, created_at=15,
    ))
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    ids = {n["id"] for n in delta.notifications_new}
    assert ids == {"notif.new"}
    assert delta.unread_notifications_count == 1


def test_delta_notifications_skip_already_seen():
    """A notification with created_at > since but already marked seen is
    excluded — the agent already saw it."""
    world, scheduler = _seed_world()
    world.add_notification(Notification(
        id="notif.seen", recipient_id="person.tpm", kind="x",
        payload={}, created_at=15, seen=True,
    ))
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    assert delta.notifications_new == []


# ---------------------------------------------------------------------------
# current_busy_state
# ---------------------------------------------------------------------------


def test_delta_current_busy_state_none_when_free():
    world, scheduler = _seed_world()
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    assert delta.current_busy_state is None


def test_delta_current_busy_state_reports_calendar_event():
    """When the agent is mid-meeting, current_busy_state names the event."""
    world, scheduler = _seed_world()
    world.add_calendar_event(CalendarEvent(
        id="cal.standup", title="Standup",
        start_sim_time=10, end_sim_time=40,
        organizer_id="person.tpm", attendees=["person.tpm"],
    ))
    world.people["person.tpm"].busy_until = 40
    scheduler.advance(20)  # now = 20, mid-event
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    assert delta.current_busy_state is not None
    state = delta.current_busy_state
    assert state["busy_until"] == 40
    assert state["current_event"] == {
        "event_id": "cal.standup", "title": "Standup", "end_sim_time": 40,
    }


def test_delta_current_busy_state_event_none_when_no_covering_event():
    """busy_until set without a covering calendar event (e.g., a long
    action) reports busy_until but current_event=None."""
    world, scheduler = _seed_world()
    world.people["person.tpm"].busy_until = 90
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    assert delta.current_busy_state == {
        "busy_until": 90, "current_event": None,
    }


# ---------------------------------------------------------------------------
# Shape compatibility
# ---------------------------------------------------------------------------


def test_delta_uses_same_briefing_model():
    """The delta briefing reuses the `Briefing` pydantic model — no new
    variant. Downstream consumers see the same shape with `since_sim_time`
    set."""
    from sim.agent.briefing import Briefing
    world, scheduler = _seed_world()
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    assert isinstance(delta, Briefing)
    assert delta.since_sim_time == 10
    # Pre-existing fields still serialize.
    json = delta.model_dump_json()
    assert "since_sim_time" in json
    assert "notifications_new" in json
    assert "current_busy_state" in json


def test_full_briefing_remains_callable_with_no_delta_fields_set():
    """The original `build(...)` method is untouched. Its result has
    since_sim_time=None — full-mode framing."""
    world, scheduler = _seed_world()
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    full = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    assert full.since_sim_time is None
    assert full.notifications_new == []
    assert full.current_busy_state is None


def test_delta_renders_with_since_framing():
    """The rendered markdown should signal delta mode so the agent treats
    the briefing as 'react to changes', not 'survey the world'."""
    world, scheduler = _seed_world()
    scheduler.advance(20)
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    delta = assembler.build_delta_briefing("person.tpm", since=10)
    md = render_briefing(delta)
    assert "DELTA" in md
    assert "Since your last check at sim_time=10" in md


def test_full_render_does_not_show_delta_framing():
    """The full briefing's rendered output keeps its original headline."""
    world, scheduler = _seed_world()
    assembler = BriefingAssembler(world, end_sim_time=480, scheduler=scheduler)
    full = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    md = render_briefing(full)
    assert "DELTA" not in md
    assert "Since your last check" not in md
