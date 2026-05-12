"""Scenario loader.

A scenario is a *directory bundle*. The loader:

  1. Parses `scenario.yaml`.
  2. Parses every `personas/*.yaml`.
  3. Parses each optional `seed/*.yaml` (channels, chats, emails, tasks, docs, calendar).
  4. Parses optional `events.yaml` (pre-scheduled future events).
  5. Parses optional `eval.yaml` (kept opaque here; consumed by the grader).
  6. Hydrates a `World` and a list of scheduled `Event`s.

There is no scenario-specific Python — every scenario is pure data. This is
what makes scenario authoring scale without prompt spaghetti.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sim.npc.policy import NpcPolicy
from sim.scenario.schema import (
    EvalYaml,
    EventsYaml,
    PersonaYaml,
    ScenarioYaml,
    ScheduledEventYaml,
    SeedCalendarYaml,
    SeedChannelYaml,
    SeedChatYaml,
    SeedDocYaml,
    SeedEmailThreadYaml,
    SeedEmailYaml,
    SeedTaskYaml,
)
from sim.scheduler import Event, Scheduler
from sim.store import (
    CalendarEvent,
    Channel,
    Doc,
    DocVersion,
    Email,
    EmailThread,
    Message,
    Person,
    Task,
    World,
)


@dataclass
class LoadedScenario:
    """The outputs of `load_scenario` — what the rest of the system consumes."""

    config: ScenarioYaml
    world: World
    scheduler: Scheduler
    npc_policies: dict[str, NpcPolicy] = field(default_factory=dict)
    eval_ground_truth: EvalYaml = field(default_factory=EvalYaml)
    scheduled_events: list[ScheduledEventYaml] = field(default_factory=list)


def _read_yaml(path: Path) -> Any:
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


def _hhmm_to_minutes(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def load_scenario(
    path: str | Path,
    *,
    tick_size_minutes: int | None = None,
) -> LoadedScenario:
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"scenario directory not found: {root}")

    config = ScenarioYaml.model_validate(_read_yaml(root / "scenario.yaml"))
    # CLI-supplied tick size overrides the per-scenario default.
    if tick_size_minutes is not None:
        config = config.model_copy(update={"tick_size_minutes": tick_size_minutes})

    # Origin minute-of-week (0..10079); needed for business-hours math.
    origin = config.start.weekday * 24 * 60 + _hhmm_to_minutes(config.start.time)

    world = World(
        scenario_id=config.id,
        seed=config.seed,
        scenario_origin_minute_of_week=origin,
    )
    scheduler = Scheduler(start_time=0)
    npc_policies: dict[str, NpcPolicy] = {}

    # ------ personas
    persona_dir = root / "personas"
    if persona_dir.is_dir():
        for p_path in sorted(persona_dir.glob("*.yaml")):
            persona = PersonaYaml.model_validate(_read_yaml(p_path))
            person = Person(
                id=persona.id, display_name=persona.display_name, role=persona.role,
                team=persona.team, manager_id=persona.manager_id,
                is_agent=persona.is_agent,
                timezone_offset_minutes=persona.timezone_offset_minutes,
                working_hours=persona.working_hours or config.default_working_hours,
                responsiveness=persona.responsiveness,
                persona_notes=persona.persona_notes,
                knowledge=dict(persona.knowledge),
            )
            world.add_person(person)
            if persona.npc_policy is not None:
                npc_policies[persona.id] = NpcPolicy(
                    allowed_tools=list(persona.npc_policy.allowed_tools),
                    delays=dict(persona.npc_policy.delays),
                )

    # Set agent_id if not yet set (in case is_agent flag was missing on all personas)
    if world.agent_id is None and config.agent_id in world.people:
        world.agent_id = config.agent_id

    # ------ seeds
    _load_seed_channels(root, world)
    _load_seed_chats(root, world)
    _load_seed_emails(root, world)
    _load_seed_tasks(root, world)
    _load_seed_docs(root, world)
    _load_seed_calendar(root, world)

    # ------ events
    events_yaml = root / "events.yaml"
    scheduled_events: list[ScheduledEventYaml] = []
    if events_yaml.is_file():
        events = EventsYaml.model_validate(_read_yaml(events_yaml))
        scheduled_events = list(events.events)
        for e in scheduled_events:
            scheduler.schedule(
                fire_at=e.fire_at,
                kind=e.kind,
                payload={
                    "actor": e.actor,
                    "params": dict(e.params),
                },
                handler=_make_event_handler(e.kind, world),
            )

    # ------ eval (kept opaque — consumed by grader later)
    eval_yaml = root / "eval.yaml"
    eval_truth = EvalYaml()
    if eval_yaml.is_file():
        eval_truth = EvalYaml.model_validate(_read_yaml(eval_yaml))

    # Always schedule hourly agent_heartbeat events through end_sim_time.
    for h in range(1, (config.end_sim_time // 60) + 1):
        scheduler.schedule(
            fire_at=h * 60, kind="agent_heartbeat", payload={},
        )

    return LoadedScenario(
        config=config, world=world, scheduler=scheduler,
        npc_policies=npc_policies,
        eval_ground_truth=eval_truth,
        scheduled_events=scheduled_events,
    )


def _load_seed_channels(root: Path, world: World) -> None:
    path = root / "seed" / "channels.yaml"
    if not path.is_file():
        return
    data = _read_yaml(path)
    for raw in data.get("channels", []):
        c = SeedChannelYaml.model_validate(raw)
        world.add_channel(Channel(
            id=c.id, name=c.name, members=list(c.members),
            is_private=c.is_private, is_dm=c.is_dm, topic=c.topic,
        ))


def _load_seed_chats(root: Path, world: World) -> None:
    path = root / "seed" / "chats.yaml"
    if not path.is_file():
        return
    data = _read_yaml(path)
    for raw in data.get("messages", []):
        m = SeedChatYaml.model_validate(raw)
        world.add_message(Message(
            id=m.id, channel_id=m.channel_id, sender_id=m.sender_id,
            body=m.body, sim_time=m.sim_time, mentions=list(m.mentions),
            thread_root_id=m.thread_root_id,
        ))


def _load_seed_emails(root: Path, world: World) -> None:
    path = root / "seed" / "emails.yaml"
    if not path.is_file():
        return
    data = _read_yaml(path)
    for raw in data.get("threads", []):
        t = SeedEmailThreadYaml.model_validate(raw)
        world.add_email_thread(EmailThread(
            id=t.id, subject=t.subject, participants=list(t.participants),
        ))
    for raw in data.get("emails", []):
        e = SeedEmailYaml.model_validate(raw)
        world.add_email(Email(
            id=e.id, thread_id=e.thread_id, sender_id=e.sender_id,
            to=list(e.to), cc=list(e.cc), body=e.body, sim_time=e.sim_time,
        ))


def _load_seed_tasks(root: Path, world: World) -> None:
    path = root / "seed" / "tasks.yaml"
    if not path.is_file():
        return
    data = _read_yaml(path)
    for raw in data.get("tasks", []):
        t = SeedTaskYaml.model_validate(raw)
        world.add_task(Task(
            id=t.id, project=t.project, title=t.title, description=t.description,
            status=t.status, assignee_id=t.assignee_id, reporter_id=t.reporter_id,
            priority=t.priority, estimated_effort_seconds=t.estimated_effort_seconds,
            remaining_effort_seconds=t.remaining_effort_seconds or t.estimated_effort_seconds,
            deadline_sim_time=t.deadline_sim_time, depends_on=list(t.depends_on),
        ))


def _load_seed_docs(root: Path, world: World) -> None:
    path = root / "seed" / "docs.yaml"
    if not path.is_file():
        return
    data = _read_yaml(path)
    for raw in data.get("docs", []):
        d = SeedDocYaml.model_validate(raw)
        world.add_doc(Doc(
            id=d.id, title=d.title, created_by=d.created_by,
            acl_view=d.acl_view, acl_edit=d.acl_edit,
        ))
        if d.body:
            world.add_doc_version(d.id, DocVersion(
                version=1, author_id=d.created_by, body=d.body, sim_time=d.sim_time,
            ))


def _load_seed_calendar(root: Path, world: World) -> None:
    path = root / "seed" / "calendar.yaml"
    if not path.is_file():
        return
    data = _read_yaml(path)
    for raw in data.get("events", []):
        c = SeedCalendarYaml.model_validate(raw)
        world.add_calendar_event(CalendarEvent(
            id=c.id, title=c.title,
            start_sim_time=c.start_sim_time, end_sim_time=c.end_sim_time,
            organizer_id=c.organizer_id, attendees=list(c.attendees),
            location=c.location, agenda=c.agenda,
        ))


# ---------------------------------------------------------------------------
# Pre-scheduled event handlers
# ---------------------------------------------------------------------------


def _make_event_handler(kind: str, world: World):
    if kind == "npc_send_chat":
        return _make_chat_sender(world, kind="message")
    if kind == "npc_send_dm":
        return _make_chat_sender(world, kind="dm")
    if kind == "npc_send_email":
        return _make_email_sender(world)
    # Heartbeats and calendar starts are "marker" events with no side effect
    # beyond their kind. They're still useful: the NPC runtime listens for
    # `calendar_event_start` to set attendees' `busy_until`, and downstream
    # tooling can scan them.
    return None


def _make_chat_sender(world: World, *, kind: str):
    def handler(event: Event, scheduler: Scheduler) -> None:
        actor = event.payload["actor"]
        params = event.payload["params"]
        body = params["body"]
        mentions = list(params.get("mentions", []))
        if kind == "dm":
            recipient = params["recipient_id"]
            lo, hi = sorted([actor, recipient])
            channel_id = f"dm.{lo}__{hi}"
            if world.get_channel(channel_id) is None:
                world.add_channel(Channel(
                    id=channel_id, name=f"DM: {actor} ↔ {recipient}",
                    members=sorted([actor, recipient]),
                    is_private=True, is_dm=True,
                ))
        else:
            channel_id = params["channel_id"]
        idx = sum(1 for m in world.messages.values() if m.channel_id == channel_id)
        world.add_message(Message(
            id=f"msg.{channel_id}.{idx + 1}",
            channel_id=channel_id, sender_id=actor, body=body,
            sim_time=scheduler.sim_time, mentions=mentions,
        ))
    return handler


def _make_email_sender(world: World):
    def handler(event: Event, scheduler: Scheduler) -> None:
        actor = event.payload["actor"]
        params = event.payload["params"]
        thread_id = params.get("thread_id")
        if not thread_id:
            thread_id = f"thread.scheduled.{event.event_id}"
            world.add_email_thread(EmailThread(
                id=thread_id, subject=params["subject"],
                participants=sorted({actor, *params.get("to", []), *params.get("cc", [])}),
            ))
        world.add_email(Email(
            id=f"email.scheduled.{event.event_id}",
            thread_id=thread_id, sender_id=actor,
            to=list(params.get("to", [])),
            cc=list(params.get("cc", [])),
            body=params.get("body", ""),
            sim_time=scheduler.sim_time,
        ))
    return handler
