"""In-memory world aggregate.

The single source of truth for simulation state. Mutations are *only* applied
through `World` methods, which always emit a typed event via `_emit`. NPC
runtime and tools subscribe to those events to react to changes.

Critical invariant: a scenario seed inserting a message via `world.add_message`
is bit-identical in effect to the agent's `chat.send` tool doing the same.
There is no special "seed mode" — composability depends on this.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sim.store.entities import (
    CalendarEvent,
    Channel,
    Doc,
    DocVersion,
    Email,
    EmailThread,
    MeetingTranscript,
    Message,
    Notification,
    Person,
    Task,
)

Subscriber = Callable[[str, dict[str, Any]], None]


class WorldSnapshot(BaseModel):
    """JSON-serializable snapshot of the entire world state.

    Lists are sorted by id where applicable to keep snapshots byte-stable.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = ""
    seed: int = 0
    scenario_origin_minute_of_week: int = 0
    agent_id: str | None = None

    people: list[Person] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    email_threads: list[EmailThread] = Field(default_factory=list)
    emails: list[Email] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    docs: list[Doc] = Field(default_factory=list)
    calendar: list[CalendarEvent] = Field(default_factory=list)
    meeting_transcripts: list[MeetingTranscript] = Field(default_factory=list)
    notifications: list[Notification] = Field(default_factory=list)


class World:
    """Aggregate root for all simulation state.

    Subscribers are registered at wire-up time; not serialized.
    """

    def __init__(
        self,
        scenario_id: str = "",
        seed: int = 0,
        scenario_origin_minute_of_week: int = 540,  # Mon 09:00
        agent_id: str | None = None,
    ) -> None:
        self.scenario_id = scenario_id
        self.seed = seed
        self.scenario_origin_minute_of_week = scenario_origin_minute_of_week
        self.agent_id = agent_id

        self.people: dict[str, Person] = {}
        self.channels: dict[str, Channel] = {}
        self.messages: dict[str, Message] = {}
        self.email_threads: dict[str, EmailThread] = {}
        self.emails: dict[str, Email] = {}
        self.tasks: dict[str, Task] = {}
        self.docs: dict[str, Doc] = {}
        self.calendar: dict[str, CalendarEvent] = {}
        self.meeting_transcripts: dict[str, MeetingTranscript] = {}
        self.notifications: dict[str, Notification] = {}

        self._subscribers: dict[str, list[Subscriber]] = {}

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, event_name: str, handler: Subscriber) -> None:
        self._subscribers.setdefault(event_name, []).append(handler)

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        for handler in self._subscribers.get(event_name, ()):
            handler(event_name, payload)

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------

    def add_person(self, person: Person) -> Person:
        if person.id in self.people:
            raise ValueError(f"person already exists: {person.id}")
        self.people[person.id] = person
        if person.is_agent:
            self.agent_id = person.id
        self._emit("person_added", {"person_id": person.id})
        return person

    def get_person(self, person_id: str) -> Person | None:
        return self.people.get(person_id)

    def list_people(self) -> Iterable[Person]:
        return self.people.values()

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def add_channel(self, channel: Channel) -> Channel:
        if channel.id in self.channels:
            raise ValueError(f"channel already exists: {channel.id}")
        self.channels[channel.id] = channel
        self._emit("channel_added", {"channel_id": channel.id})
        return channel

    def get_channel(self, channel_id: str) -> Channel | None:
        return self.channels.get(channel_id)

    def add_message(self, message: Message) -> Message:
        if message.id in self.messages:
            raise ValueError(f"message already exists: {message.id}")
        if message.channel_id not in self.channels:
            raise ValueError(f"unknown channel: {message.channel_id}")
        if message.sender_id not in self.people:
            raise ValueError(f"unknown sender: {message.sender_id}")
        self.messages[message.id] = message
        self._emit(
            "message_inserted",
            {
                "message_id": message.id,
                "channel_id": message.channel_id,
                "sender_id": message.sender_id,
                "mentions": list(message.mentions),
                "sim_time": message.sim_time,
            },
        )
        return message

    def messages_in_channel(self, channel_id: str) -> list[Message]:
        return sorted(
            (m for m in self.messages.values() if m.channel_id == channel_id),
            key=lambda m: (m.sim_time, m.id),
        )

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    def add_email_thread(self, thread: EmailThread) -> EmailThread:
        if thread.id in self.email_threads:
            raise ValueError(f"thread already exists: {thread.id}")
        self.email_threads[thread.id] = thread
        self._emit("email_thread_created", {"thread_id": thread.id})
        return thread

    def add_email(self, email: Email) -> Email:
        if email.id in self.emails:
            raise ValueError(f"email already exists: {email.id}")
        if email.thread_id not in self.email_threads:
            raise ValueError(f"unknown thread: {email.thread_id}")
        self.emails[email.id] = email
        self._emit(
            "email_inserted",
            {
                "email_id": email.id,
                "thread_id": email.thread_id,
                "sender_id": email.sender_id,
                "to": list(email.to),
                "cc": list(email.cc),
                "sim_time": email.sim_time,
            },
        )
        return email

    def emails_in_thread(self, thread_id: str) -> list[Email]:
        return sorted(
            (e for e in self.emails.values() if e.thread_id == thread_id),
            key=lambda e: (e.sim_time, e.id),
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def add_task(self, task: Task) -> Task:
        if task.id in self.tasks:
            raise ValueError(f"task already exists: {task.id}")
        self.tasks[task.id] = task
        self._emit(
            "task_created",
            {
                "task_id": task.id,
                "project": task.project,
                "assignee_id": task.assignee_id,
                "status": task.status,
                "priority": task.priority,
            },
        )
        return task

    def update_task_status(self, task_id: str, status: str) -> Task:
        task = self.tasks[task_id]
        previous = task.status
        task.status = status  # type: ignore[assignment]
        self._emit(
            "task_status_changed",
            {"task_id": task_id, "from": previous, "to": status},
        )
        return task

    def assign_task(self, task_id: str, assignee_id: str | None) -> Task:
        task = self.tasks[task_id]
        previous = task.assignee_id
        task.assignee_id = assignee_id
        self._emit(
            "task_assigned",
            {"task_id": task_id, "from": previous, "to": assignee_id},
        )
        return task

    def log_work(self, task_id: str, seconds: int) -> Task:
        task = self.tasks[task_id]
        if task.remaining_effort_seconds is None:
            raise ValueError(f"task {task_id} has no remaining_effort_seconds")
        task.remaining_effort_seconds = max(0, task.remaining_effort_seconds - seconds)
        self._emit(
            "task_work_logged",
            {
                "task_id": task_id,
                "seconds": seconds,
                "remaining_effort_seconds": task.remaining_effort_seconds,
            },
        )
        return task

    # ------------------------------------------------------------------
    # Docs
    # ------------------------------------------------------------------

    def add_doc(self, doc: Doc) -> Doc:
        if doc.id in self.docs:
            raise ValueError(f"doc already exists: {doc.id}")
        self.docs[doc.id] = doc
        self._emit("doc_created", {"doc_id": doc.id, "title": doc.title})
        return doc

    def add_doc_version(self, doc_id: str, version: DocVersion) -> Doc:
        doc = self.docs[doc_id]
        expected_version = (doc.versions[-1].version + 1) if doc.versions else 1
        if version.version != expected_version:
            raise ValueError(
                f"doc {doc_id} expected version {expected_version}, got {version.version}"
            )
        doc.versions.append(version)
        self._emit(
            "doc_edited",
            {"doc_id": doc_id, "version": version.version, "author_id": version.author_id},
        )
        return doc

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------

    def add_calendar_event(self, event: CalendarEvent) -> CalendarEvent:
        if event.id in self.calendar:
            raise ValueError(f"calendar event already exists: {event.id}")
        if event.end_sim_time <= event.start_sim_time:
            raise ValueError(
                f"calendar event {event.id}: end must be > start"
            )
        self.calendar[event.id] = event
        self._emit(
            "calendar_event_created",
            {
                "event_id": event.id,
                "start_sim_time": event.start_sim_time,
                "end_sim_time": event.end_sim_time,
                "attendees": list(event.attendees),
            },
        )
        return event

    def add_meeting_transcript(self, transcript: MeetingTranscript) -> MeetingTranscript:
        if transcript.meeting_id in self.meeting_transcripts:
            raise ValueError(f"transcript already exists: {transcript.meeting_id}")
        self.meeting_transcripts[transcript.meeting_id] = transcript
        self._emit(
            "meeting_transcript_added",
            {"meeting_id": transcript.meeting_id, "synthesized": transcript.synthesized},
        )
        return transcript

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def add_notification(self, notification: Notification) -> Notification:
        if notification.id in self.notifications:
            raise ValueError(f"notification already exists: {notification.id}")
        self.notifications[notification.id] = notification
        self._emit(
            "notification_created",
            {
                "notification_id": notification.id,
                "recipient_id": notification.recipient_id,
                "kind": notification.kind,
            },
        )
        return notification

    def mark_notification_seen(self, notification_id: str) -> Notification:
        n = self.notifications[notification_id]
        n.seen = True
        self._emit("notification_seen", {"notification_id": notification_id})
        return n

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self) -> WorldSnapshot:
        return WorldSnapshot(
            scenario_id=self.scenario_id,
            seed=self.seed,
            scenario_origin_minute_of_week=self.scenario_origin_minute_of_week,
            agent_id=self.agent_id,
            people=sorted(self.people.values(), key=lambda p: p.id),
            channels=sorted(self.channels.values(), key=lambda c: c.id),
            messages=sorted(self.messages.values(), key=lambda m: (m.sim_time, m.id)),
            email_threads=sorted(self.email_threads.values(), key=lambda t: t.id),
            emails=sorted(self.emails.values(), key=lambda e: (e.sim_time, e.id)),
            tasks=sorted(self.tasks.values(), key=lambda t: t.id),
            docs=sorted(self.docs.values(), key=lambda d: d.id),
            calendar=sorted(self.calendar.values(), key=lambda c: (c.start_sim_time, c.id)),
            meeting_transcripts=sorted(
                self.meeting_transcripts.values(), key=lambda t: t.meeting_id
            ),
            notifications=sorted(self.notifications.values(), key=lambda n: (n.created_at, n.id)),
        )

    def to_json(self) -> str:
        # Sort keys for byte-stable snapshots across runs.
        return self.snapshot().model_dump_json(indent=2, by_alias=False)

    @classmethod
    def from_snapshot(cls, snapshot: WorldSnapshot) -> "World":
        world = cls(
            scenario_id=snapshot.scenario_id,
            seed=snapshot.seed,
            scenario_origin_minute_of_week=snapshot.scenario_origin_minute_of_week,
            agent_id=snapshot.agent_id,
        )
        for p in snapshot.people:
            world.people[p.id] = p
        for c in snapshot.channels:
            world.channels[c.id] = c
        for m in snapshot.messages:
            world.messages[m.id] = m
        for t in snapshot.email_threads:
            world.email_threads[t.id] = t
        for e in snapshot.emails:
            world.emails[e.id] = e
        for t in snapshot.tasks:
            world.tasks[t.id] = t
        for d in snapshot.docs:
            world.docs[d.id] = d
        for c in snapshot.calendar:
            world.calendar[c.id] = c
        for t in snapshot.meeting_transcripts:
            world.meeting_transcripts[t.meeting_id] = t
        for n in snapshot.notifications:
            world.notifications[n.id] = n
        return world

    @classmethod
    def from_json(cls, raw: str) -> "World":
        return cls.from_snapshot(WorldSnapshot.model_validate_json(raw))
