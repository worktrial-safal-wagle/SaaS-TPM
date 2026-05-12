"""World entity models.

All entities are pydantic models so JSON round-trips are free and schemas are
auto-derivable. IDs are human-readable string slugs (`person.maya`,
`task.PROJ-42`) to keep snapshots diffable.

Times are integer minutes since scenario start (sim_time). The `wall_clock_minute`
mapping to weekday/time-of-day lives in `worktime.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Entity(BaseModel):
    """Base for all world entities. Mutable; equality by id; stable JSON ordering."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class WorkingHours(_Entity):
    """A person's working hours in their *local* day.

    `start_minute` and `end_minute` are minute-of-day (0..1439).
    `weekdays` is the set of weekdays they work, 0=Mon..6=Sun.
    """

    start_minute: int = 9 * 60
    end_minute: int = 17 * 60
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])


class Person(_Entity):
    id: str
    display_name: str
    role: str
    team: str | None = None
    manager_id: str | None = None
    is_agent: bool = False
    # Minutes east of scenario origin; e.g. +180 for someone 3h ahead of the
    # scenario clock. Working hours are interpreted in this offset.
    timezone_offset_minutes: int = 0
    working_hours: WorkingHours = Field(default_factory=WorkingHours)
    # Multiplier on sampled NPC reply delays. Junior eng = 2.5; VP = 0.3.
    responsiveness: float = 1.0
    # Free-form persona hints used by the NPC brain prompt.
    persona_notes: str = ""
    # Knowledge state the loader can seed (used by NPC brain context).
    knowledge: dict[str, str] = Field(default_factory=dict)
    # Tick scheduling: when this actor should next be polled
    next_poll_at: int = 0
    # Tick scheduling: actor unavailable until this sim_time
    busy_until: int = 0


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class Channel(_Entity):
    id: str
    name: str
    members: list[str] = Field(default_factory=list)
    is_private: bool = False
    is_dm: bool = False
    topic: str = ""


class Message(_Entity):
    id: str
    channel_id: str
    sender_id: str
    body: str
    sim_time: int
    thread_root_id: str | None = None
    mentions: list[str] = Field(default_factory=list)
    read_by: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class EmailThread(_Entity):
    id: str
    subject: str
    participants: list[str] = Field(default_factory=list)


class Email(_Entity):
    id: str
    thread_id: str
    sender_id: str
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    body: str = ""
    sim_time: int
    read_by: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


TaskStatus = Literal["Backlog", "Todo", "In Progress", "Blocked", "In Review", "Done"]
TaskPriority = Literal["P0", "P1", "P2", "P3"]


class TaskComment(_Entity):
    author_id: str
    body: str
    sim_time: int


class Task(_Entity):
    id: str
    project: str
    title: str
    description: str = ""
    status: TaskStatus = "Backlog"
    assignee_id: str | None = None
    reporter_id: str | None = None
    priority: TaskPriority = "P2"
    estimated_effort_seconds: int | None = None
    remaining_effort_seconds: int | None = None
    deadline_sim_time: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    comments: list[TaskComment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


class DocVersion(_Entity):
    version: int
    author_id: str
    body: str
    sim_time: int


class DocComment(_Entity):
    author_id: str
    body: str
    sim_time: int
    anchor: str | None = None


class Doc(_Entity):
    id: str
    title: str
    created_by: str
    versions: list[DocVersion] = Field(default_factory=list)
    comments: list[DocComment] = Field(default_factory=list)
    # None = visible to all employees. Otherwise an explicit allowlist.
    acl_view: list[str] | None = None
    # None = same as acl_view.
    acl_edit: list[str] | None = None


# ---------------------------------------------------------------------------
# Calendar / Meetings
# ---------------------------------------------------------------------------


class CalendarEvent(_Entity):
    id: str
    title: str
    start_sim_time: int
    end_sim_time: int
    organizer_id: str
    attendees: list[str] = Field(default_factory=list)
    location: str = ""
    agenda: str = ""
    # Filled in by `meetings.attend` when the agent attends live.
    attended_by_agent: bool = False


class TranscriptTurn(_Entity):
    speaker_id: str
    body: str
    sim_time: int


class MeetingTranscript(_Entity):
    meeting_id: str
    turns: list[TranscriptTurn] = Field(default_factory=list)
    generated_at: int
    synthesized: bool = False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class Notification(_Entity):
    id: str
    recipient_id: str
    kind: str
    payload: dict = Field(default_factory=dict)
    created_at: int
    seen: bool = False
