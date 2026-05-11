"""Pydantic schemas for the scenario YAML bundle."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sim.npc.policy import DelaySpec
from sim.store.entities import WorkingHours


class _Yaml(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# scenario.yaml
# ---------------------------------------------------------------------------


class ScenarioStart(_Yaml):
    weekday: int = 0  # 0=Mon..6=Sun
    time: str = "09:00"  # HH:MM, in scenario timezone


class ScenarioYaml(_Yaml):
    id: str
    seed: int = 0
    agent_id: str
    start: ScenarioStart = Field(default_factory=ScenarioStart)
    # Sim minutes until the scenario "ends" (used by the final evaluator's
    # tier gating).
    end_sim_time: int = 5 * 24 * 60  # 5 days
    description: str = ""
    default_working_hours: WorkingHours = Field(default_factory=WorkingHours)


# ---------------------------------------------------------------------------
# personas/*.yaml
# ---------------------------------------------------------------------------


class NpcPolicyYaml(_Yaml):
    allowed_tools: list[str] = Field(default_factory=list)
    delays: dict[str, DelaySpec] = Field(default_factory=lambda: {"default": DelaySpec()})


class PersonaYaml(_Yaml):
    id: str
    display_name: str
    role: str
    team: str | None = None
    manager_id: str | None = None
    is_agent: bool = False
    timezone_offset_minutes: int = 0
    working_hours: WorkingHours | None = None
    responsiveness: float = 1.0
    persona_notes: str = ""
    knowledge: dict[str, str] = Field(default_factory=dict)
    npc_policy: NpcPolicyYaml | None = None


# ---------------------------------------------------------------------------
# seed/*.yaml
# ---------------------------------------------------------------------------


class SeedChannelYaml(_Yaml):
    id: str
    name: str
    members: list[str] = Field(default_factory=list)
    is_private: bool = False
    is_dm: bool = False
    topic: str = ""


class SeedChatYaml(_Yaml):
    id: str
    channel_id: str
    sender_id: str
    body: str
    sim_time: int
    mentions: list[str] = Field(default_factory=list)
    thread_root_id: str | None = None


class SeedEmailThreadYaml(_Yaml):
    id: str
    subject: str
    participants: list[str] = Field(default_factory=list)


class SeedEmailYaml(_Yaml):
    id: str
    thread_id: str
    sender_id: str
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    body: str = ""
    sim_time: int


class SeedTaskYaml(_Yaml):
    id: str
    project: str
    title: str
    description: str = ""
    status: str = "Backlog"
    assignee_id: str | None = None
    reporter_id: str | None = None
    priority: str = "P2"
    estimated_effort_seconds: int | None = None
    remaining_effort_seconds: int | None = None
    deadline_sim_time: int | None = None
    depends_on: list[str] = Field(default_factory=list)


class SeedDocYaml(_Yaml):
    id: str
    title: str
    created_by: str
    body: str = ""
    acl_view: list[str] | None = None
    acl_edit: list[str] | None = None
    sim_time: int = 0


class SeedCalendarYaml(_Yaml):
    id: str
    title: str
    start_sim_time: int
    end_sim_time: int
    organizer_id: str
    attendees: list[str] = Field(default_factory=list)
    location: str = ""
    agenda: str = ""


# ---------------------------------------------------------------------------
# events.yaml — pre-scheduled future events
# ---------------------------------------------------------------------------


EventKind = Literal[
    "npc_send_chat",
    "npc_send_dm",
    "npc_send_email",
    "agent_heartbeat",
    "calendar_event_start",
]


class ScheduledEventYaml(_Yaml):
    kind: EventKind
    fire_at: int
    actor: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class EventsYaml(_Yaml):
    events: list[ScheduledEventYaml] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# eval.yaml — ground truth for grading
# ---------------------------------------------------------------------------


class EvalObjectiveYaml(_Yaml):
    id: str
    description: str
    check: dict[str, Any]  # discriminated by check.kind


class EvalArtifactYaml(_Yaml):
    id: str
    description: str
    locator: dict[str, Any]
    rubric: list[str]


class EvalAntiHackYaml(_Yaml):
    id: str
    description: str
    signal: dict[str, Any]


class EvalHiddenFactYaml(_Yaml):
    id: str
    description: str
    source_locator: dict[str, Any]


class EvalYaml(_Yaml):
    objectives: list[EvalObjectiveYaml] = Field(default_factory=list)
    artifacts: list[EvalArtifactYaml] = Field(default_factory=list)
    anti_hack: list[EvalAntiHackYaml] = Field(default_factory=list)
    hidden_facts: list[EvalHiddenFactYaml] = Field(default_factory=list)
