"""Briefing assembler — what the agent sees at the start of each turn.

The briefing is a *structured* pydantic model first (testable, version-able),
rendered to markdown for the LLM at the boundary.

Sections, in order:
  1. Now: sim_time + wall-clock label.
  2. Persona reminder.
  3. Unread notifications (the urgent inbox).
  4. Unread chats grouped by channel.
  5. Unread emails (thread-level).
  6. Open commitments — DMs/mentions to the agent without a reply yet,
     plus tasks the agent owns.
  7. Upcoming calendar window (next 4 hours).
  8. Latest windowed-evaluator verdict, if any.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sim.store import World
from sim.store.entities import Message, Notification, Task
from sim.store.worktime import MINUTES_PER_DAY


WEEKDAY_LABEL = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class BriefingTaskDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    title: str
    status: str
    assignee_id: str | None
    priority: str


class BriefingCommitment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # "dm_awaiting_reply", "task_owned", "mention_awaiting_reply"
    detail: dict[str, Any]


class BriefingRecentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn: int
    sim_time: int
    tool: str
    args_summary: str  # short, truncated repr
    ok: bool
    error: str | None = None
    result_summary: str | None = None  # short, truncated repr of the result


class Briefing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    now_sim_time: int
    now_label: str
    end_sim_time: int
    agent_persona: dict[str, Any]
    unread_notifications_count: int
    unread_chats: list[dict[str, Any]]  # [{channel_id, channel_name, messages: [...]}]
    unread_emails: list[dict[str, Any]]
    open_commitments: list[BriefingCommitment]
    task_deltas: list[BriefingTaskDelta]
    upcoming_calendar: list[dict[str, Any]]
    recent_actions: list[BriefingRecentAction] = Field(default_factory=list)
    # Number of consecutive recent turns that did not advance sim_time. Surfaced
    # as a warning in the rendered briefing when high — the agent is looping.
    sim_time_stalled_for_turns: int = 0
    last_verdict: dict[str, Any] | None = None
    tool_summary: list[str] = Field(default_factory=list)


class BriefingAssembler:
    def __init__(self, world: World, *, end_sim_time: int) -> None:
        self.world = world
        self.end_sim_time = end_sim_time

    def build(
        self,
        now_sim_time: int,
        last_turn_sim_time: int,
        *,
        agent_id: str | None = None,
        last_verdict: dict[str, Any] | None = None,
        tool_names: list[str] | None = None,
        recent_actions: list["BriefingRecentAction"] | None = None,
        sim_time_stalled_for_turns: int = 0,
    ) -> Briefing:
        agent_id = agent_id or self.world.agent_id
        assert agent_id is not None, "world.agent_id must be set"
        agent = self.world.get_person(agent_id)
        assert agent is not None

        # Unread notifications addressed to the agent
        agent_notifs: list[Notification] = [
            n for n in self.world.notifications.values()
            if n.recipient_id == agent_id and not n.seen
        ]
        agent_notifs.sort(key=lambda n: (n.created_at, n.id))

        # Unread chats grouped by channel
        unread_chats = self._unread_chats(agent_id)

        # Unread emails (thread-level summary)
        unread_emails = self._unread_emails(agent_id)

        # Open commitments
        commitments = self._open_commitments(agent_id, now_sim_time)

        # Task deltas since last turn
        task_deltas = [
            BriefingTaskDelta(
                task_id=t.id, title=t.title, status=t.status,
                assignee_id=t.assignee_id, priority=t.priority,
            )
            for t in self.world.tasks.values()
            if t.assignee_id == agent_id or self._task_recently_touched(t, last_turn_sim_time)
        ]
        task_deltas.sort(key=lambda d: (d.priority, d.task_id))

        # Upcoming calendar (next 4 sim-hours)
        window_end = now_sim_time + 240
        upcoming = [
            {
                "event_id": e.id, "title": e.title,
                "start_sim_time": e.start_sim_time, "end_sim_time": e.end_sim_time,
                "agenda": e.agenda,
            }
            for e in self.world.calendar.values()
            if agent_id in e.attendees
            and now_sim_time <= e.start_sim_time <= window_end
        ]
        upcoming.sort(key=lambda d: d["start_sim_time"])

        return Briefing(
            now_sim_time=now_sim_time,
            now_label=self._wall_clock_label(now_sim_time),
            end_sim_time=self.end_sim_time,
            agent_persona={
                "id": agent.id, "display_name": agent.display_name,
                "role": agent.role, "team": agent.team,
                "persona_notes": agent.persona_notes,
            },
            unread_notifications_count=len(agent_notifs),
            unread_chats=unread_chats,
            unread_emails=unread_emails,
            open_commitments=commitments,
            task_deltas=task_deltas,
            upcoming_calendar=upcoming,
            recent_actions=list(recent_actions or []),
            sim_time_stalled_for_turns=sim_time_stalled_for_turns,
            last_verdict=last_verdict,
            tool_summary=list(tool_names or []),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _unread_chats(self, agent_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for channel in sorted(self.world.channels.values(), key=lambda c: c.id):
            if agent_id not in channel.members and not (not channel.is_private):
                continue
            unread_messages: list[Message] = [
                m for m in self.world.messages_in_channel(channel.id)
                if agent_id not in m.read_by and m.sender_id != agent_id
            ]
            if not unread_messages:
                continue
            out.append({
                "channel_id": channel.id, "channel_name": channel.name,
                "is_dm": channel.is_dm, "count": len(unread_messages),
                "messages": [
                    {
                        "id": m.id, "sender_id": m.sender_id,
                        "sim_time": m.sim_time, "body": m.body,
                    }
                    for m in unread_messages[-10:]
                ],
            })
        return out

    def _unread_emails(self, agent_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        for email in sorted(self.world.emails.values(), key=lambda e: (-e.sim_time, e.id)):
            if agent_id not in email.to and agent_id not in email.cc:
                continue
            if agent_id in email.read_by:
                continue
            if email.thread_id in seen_threads:
                continue
            seen_threads.add(email.thread_id)
            thread = self.world.email_threads.get(email.thread_id)
            out.append({
                "thread_id": email.thread_id, "email_id": email.id,
                "subject": thread.subject if thread else "",
                "sender_id": email.sender_id, "sim_time": email.sim_time,
                "snippet": email.body[:200],
            })
        return out

    def _open_commitments(self, agent_id: str, now_sim_time: int) -> list[BriefingCommitment]:
        out: list[BriefingCommitment] = []
        # Tasks owned by the agent that aren't yet Done
        for task in self.world.tasks.values():
            if task.assignee_id == agent_id and task.status != "Done":
                out.append(BriefingCommitment(
                    kind="task_owned",
                    detail={"task_id": task.id, "title": task.title,
                            "status": task.status, "priority": task.priority,
                            "deadline_sim_time": task.deadline_sim_time},
                ))
        # DMs/mentions where the most recent message in the conversation
        # isn't from the agent — the agent owes a reply.
        for channel in self.world.channels.values():
            if not (channel.is_dm or agent_id in channel.members):
                continue
            msgs = self.world.messages_in_channel(channel.id)
            if not msgs:
                continue
            last = msgs[-1]
            if last.sender_id == agent_id:
                continue
            owes_reply = channel.is_dm or agent_id in last.mentions
            if owes_reply:
                out.append(BriefingCommitment(
                    kind="dm_awaiting_reply" if channel.is_dm else "mention_awaiting_reply",
                    detail={"channel_id": channel.id, "from": last.sender_id,
                            "body": last.body[:200], "sim_time": last.sim_time},
                ))
        return out

    def _task_recently_touched(self, task: Task, last_turn_sim_time: int) -> bool:
        return any(c.sim_time > last_turn_sim_time for c in task.comments)

    def _wall_clock_label(self, sim_time: int) -> str:
        absolute = self.world.scenario_origin_minute_of_week + sim_time
        day_index = absolute // MINUTES_PER_DAY
        weekday = day_index % 7
        minute = absolute % MINUTES_PER_DAY
        hh, mm = divmod(minute, 60)
        week_number = day_index // 7
        suffix = f" (W+{week_number})" if week_number else ""
        return f"{WEEKDAY_LABEL[weekday]} {hh:02d}:{mm:02d}{suffix}"


# ---------------------------------------------------------------------------
# Markdown rendering for the LLM
# ---------------------------------------------------------------------------


def render_briefing(briefing: Briefing) -> str:
    lines: list[str] = []
    lines.append(f"# Briefing — {briefing.now_label} (sim_time={briefing.now_sim_time})")
    lines.append("")
    lines.append(f"_End of scenario at sim_time={briefing.end_sim_time}._")
    lines.append("")
    lines.append(f"**You are {briefing.agent_persona['display_name']} ({briefing.agent_persona['role']}).**")
    lines.append("")
    lines.append(briefing.agent_persona.get("persona_notes", "") or "")
    lines.append("")
    if briefing.sim_time_stalled_for_turns >= 3:
        lines.append(
            f"## ⚠️ Stall warning: you've spent {briefing.sim_time_stalled_for_turns} "
            f"consecutive turns at sim_time={briefing.now_sim_time}. "
            "Stop reading — take a write action (send, comment, update_status, "
            "edit) or call `wait.for_next_event` to advance the clock."
        )
        lines.append("")
    if briefing.recent_actions:
        lines.append("## Your recent actions + results (most recent first)")
        lines.append("_You already did these AND have the results below. Do NOT re-read the same artifact — use the result that's already in hand._")
        for a in briefing.recent_actions[-10:][::-1]:
            ok = "ok" if a.ok else f"ERR: {a.error}"
            lines.append(f"- turn {a.turn} (t={a.sim_time}): `{a.tool}` {a.args_summary} → {ok}")
            if a.result_summary:
                lines.append(f"    result: {a.result_summary}")
        lines.append("")
    if briefing.last_verdict:
        lines.append("## Last evaluator feedback")
        lines.append(f"_score={briefing.last_verdict.get('score')}, category={briefing.last_verdict.get('category')}_")
        lines.append(briefing.last_verdict.get("rationale", ""))
        lines.append("")
    lines.append(f"## Unread notifications: {briefing.unread_notifications_count}")
    lines.append("")
    if briefing.unread_chats:
        lines.append("## Unread chats")
        for ch in briefing.unread_chats:
            lines.append(f"### {ch['channel_name']} ({ch['count']} new)")
            for m in ch["messages"]:
                lines.append(f"- _{m['sender_id']} @ t={m['sim_time']}_: {m['body']}")
            lines.append("")
    if briefing.unread_emails:
        lines.append("## Unread email threads")
        for e in briefing.unread_emails:
            lines.append(f"- **{e['subject']}** (from {e['sender_id']}, t={e['sim_time']}): {e['snippet']}")
        lines.append("")
    if briefing.open_commitments:
        lines.append("## Your open commitments")
        for c in briefing.open_commitments:
            if c.kind == "task_owned":
                lines.append(f"- TASK [{c.detail['priority']}] {c.detail['task_id']}: {c.detail['title']} ({c.detail['status']})")
            else:
                lines.append(f"- {c.kind}: {c.detail.get('from')} — {c.detail.get('body','')[:120]}")
        lines.append("")
    if briefing.task_deltas:
        lines.append("## Task board snapshot")
        for t in briefing.task_deltas[:15]:
            lines.append(f"- [{t.priority}] {t.task_id} — {t.title} ({t.status}, assignee={t.assignee_id})")
        lines.append("")
    if briefing.upcoming_calendar:
        lines.append("## Calendar (next 4h)")
        for e in briefing.upcoming_calendar:
            lines.append(f"- {e['title']} t={e['start_sim_time']}..{e['end_sim_time']} — {e['agenda'][:120]}")
        lines.append("")
    return "\n".join(lines)
