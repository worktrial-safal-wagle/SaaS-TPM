"""Cross-reference validator for a loaded scenario.

Catches the kinds of errors that pydantic schema validation can't:
  - dangling task dependencies
  - personas referenced in events/seeds but not defined
  - eval objectives pointing at nonexistent entities
  - working-hours config that excludes every meeting an attendee is invited to
  - pre-scheduled events with fire_at outside the scenario window

Lint is pure validation — no LLM, no World mutation, fast.
"""

from __future__ import annotations

from dataclasses import dataclass

from sim.scenario.loader import LoadedScenario, load_scenario
from sim.store.worktime import is_in_hours


@dataclass
class LintIssue:
    severity: str  # "error" or "warning"
    code: str
    message: str


def lint_scenario(scenario: LoadedScenario) -> list[LintIssue]:
    issues: list[LintIssue] = []
    world = scenario.world

    # Agent must exist
    if scenario.config.agent_id not in world.people:
        issues.append(LintIssue("error", "missing_agent",
            f"agent_id '{scenario.config.agent_id}' has no persona file"))

    # Manager chain
    for p in world.people.values():
        if p.manager_id and p.manager_id not in world.people:
            issues.append(LintIssue("error", "unknown_manager",
                f"person {p.id} has manager_id={p.manager_id} but no such person"))

    # Channel members
    for c in world.channels.values():
        for m in c.members:
            if m not in world.people:
                issues.append(LintIssue("error", "unknown_member",
                    f"channel {c.id} lists unknown member {m}"))

    # Task references
    for t in world.tasks.values():
        if t.assignee_id and t.assignee_id not in world.people:
            issues.append(LintIssue("error", "unknown_assignee",
                f"task {t.id} assignee {t.assignee_id} not in people"))
        if t.reporter_id and t.reporter_id not in world.people:
            issues.append(LintIssue("error", "unknown_reporter",
                f"task {t.id} reporter {t.reporter_id} not in people"))
        for dep in t.depends_on:
            if dep not in world.tasks:
                issues.append(LintIssue("error", "unknown_dependency",
                    f"task {t.id} depends_on missing task {dep}"))

    # Email participants
    for thread in world.email_threads.values():
        for p in thread.participants:
            if p not in world.people:
                issues.append(LintIssue("error", "unknown_thread_participant",
                    f"thread {thread.id} participant {p} not in people"))

    # Calendar
    for evt in world.calendar.values():
        for att in evt.attendees:
            if att not in world.people:
                issues.append(LintIssue("error", "unknown_attendee",
                    f"event {evt.id} attendee {att} not in people"))
        # Warn if any attendee's working hours fully exclude the meeting.
        for att in evt.attendees:
            person = world.get_person(att)
            if person is None:
                continue
            if not is_in_hours(
                world.scenario_origin_minute_of_week,
                evt.start_sim_time, person,
            ):
                issues.append(LintIssue("warning", "meeting_outside_hours",
                    f"event {evt.id} starts outside {att}'s working hours"))

    # Pre-scheduled events
    for e in scenario.scheduled_events:
        if e.fire_at < 0 or e.fire_at > scenario.config.end_sim_time:
            issues.append(LintIssue("error", "event_out_of_range",
                f"event {e.kind} fire_at={e.fire_at} outside scenario window"))
        if e.actor and e.actor not in world.people:
            issues.append(LintIssue("error", "unknown_event_actor",
                f"event {e.kind} actor {e.actor} not in people"))

    # Eval ground-truth references — best-effort, schemas are loose dicts.
    for obj in scenario.eval_ground_truth.objectives:
        task_id = obj.check.get("task_id") if isinstance(obj.check, dict) else None
        if task_id and task_id not in world.tasks:
            issues.append(LintIssue("error", "eval_unknown_task",
                f"objective {obj.id} references unknown task {task_id}"))
    for art in scenario.eval_ground_truth.artifacts:
        loc = art.locator
        if loc.get("kind") == "email_thread":
            tid = loc.get("thread_id")
            if tid and tid not in world.email_threads:
                issues.append(LintIssue("warning", "eval_unknown_thread",
                    f"artifact {art.id} references unknown thread {tid}"))

    return issues


def lint_path(path) -> tuple[LoadedScenario, list[LintIssue]]:
    scenario = load_scenario(path)
    return scenario, lint_scenario(scenario)
