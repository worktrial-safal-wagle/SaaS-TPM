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

Two flavours of briefing share one model:
  - `build(...)` — the full briefing, used on tick 1 and as a fallback. Shows
    *all* currently-unread material and the agent's open commitments.
  - `build_delta_briefing(...)` — a delta briefing for subsequent ticks. The
    same `Briefing` shape, but populated with only items that changed since
    a `since` sim-time, framed as "what happened while you were away."
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sim.scheduler import Scheduler
from sim.store import World
from sim.store.entities import Message, Notification, Person, Task
from sim.store.worktime import MINUTES_PER_DAY


WEEKDAY_LABEL = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class BriefingProjectBoardItem(BaseModel):
    """An item on the TPM's ambient project board.

    The board is rendered each turn as the live state of open work across
    the team — what a real TPM has visible in their tracker tab. This
    eliminates the need for the agent to spend turns calling `tasks.list`
    just to recreate a view of the board.
    """

    model_config = ConfigDict(extra="forbid")
    task_id: str
    title: str
    status: str
    assignee_id: str | None
    priority: str
    deadline_sim_time: int | None = None
    # True if any of the task's `depends_on` entries is still not Done.
    blocked: bool = False


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
    # For read tools (chat.read, email.read, tasks.get, docs.read, etc.) the
    # full result body, capped at ~2000 chars. Only the most recent few read
    # calls have this rendered in the briefing — see render_briefing. Without
    # it the agent re-reads the same artifact on consecutive turns because
    # the truncated `result_summary` cuts off message bodies mid-sentence.
    result_full: str | None = None


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
    # The team's open task board: top not-Done tasks across all assignees,
    # sorted by (priority, deadline, id). Capped to keep the briefing bounded.
    # See the BriefingProjectBoardItem docstring for the design rationale.
    project_board: list[BriefingProjectBoardItem]
    upcoming_calendar: list[dict[str, Any]]
    recent_actions: list[BriefingRecentAction] = Field(default_factory=list)
    # Number of consecutive recent turns that did not advance sim_time. Surfaced
    # as a warning in the rendered briefing when high — the agent is looping.
    sim_time_stalled_for_turns: int = 0
    last_verdict: dict[str, Any] | None = None
    tool_summary: list[str] = Field(default_factory=list)
    # ------------------------------------------------------------------
    # Delta-mode fields.
    #
    # Populated by `BriefingAssembler.build_delta_briefing`. None / empty in
    # full briefings so existing consumers continue to work unchanged.
    #
    # `since_sim_time` is the watermark: every other delta field lists only
    # items with `sim_time > since_sim_time`. The rendered briefing changes
    # its headline framing when this is set ("Since your last check at
    # sim_time X, ...") to nudge the agent toward "react to what's new"
    # instead of "re-survey the whole world".
    # ------------------------------------------------------------------
    since_sim_time: int | None = None
    # New notifications since `since_sim_time` (delta-mode only; full briefing
    # uses `unread_notifications_count`).
    notifications_new: list[dict[str, Any]] = Field(default_factory=list)
    # The agent's busy state at `now_sim_time`. Schema:
    #   {"busy_until": int, "current_event": {"event_id": str, "title": str,
    #     "end_sim_time": int} | None}
    # `current_event` is filled iff a calendar event covers `now_sim_time`.
    # busy_until may also be set by long actions that aren't tied to events —
    # in that case `current_event` is None and only `busy_until` is reported.
    current_busy_state: dict[str, Any] | None = None


class BriefingAssembler:
    def __init__(
        self,
        world: World,
        *,
        end_sim_time: int,
        scheduler: Scheduler | None = None,
    ) -> None:
        self.world = world
        self.end_sim_time = end_sim_time
        # Scheduler is optional for backwards compatibility with existing
        # call sites. When provided, the assembler subscribes to
        # `task_status_changed` events and stamps them with the current
        # `scheduler.sim_time`. Without a scheduler, the delta briefing
        # falls back to using `task.comments[-1].sim_time` as the change
        # signal — coarser, but still useful.
        self._scheduler = scheduler
        # (sim_time, task_id, from_status, to_status) records, append-only.
        self._task_status_history: list[tuple[int, str, str, str]] = []
        if scheduler is not None:
            world.subscribe("task_status_changed", self._on_task_status_changed)

    def _on_task_status_changed(self, _event_name: str, payload: dict[str, Any]) -> None:
        # World emits task_status_changed without a sim_time, so we stamp
        # using the live scheduler clock at firing time.
        assert self._scheduler is not None
        self._task_status_history.append((
            self._scheduler.sim_time,
            str(payload["task_id"]),
            str(payload["from"]),
            str(payload["to"]),
        ))

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

        # Project board: top not-Done tasks across all assignees. The TPM's
        # ambient view of the team's work — not filtered to "tasks I own,"
        # since the TPM coordinates everyone else's work.
        project_board = self._build_project_board()

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
            project_board=project_board,
            upcoming_calendar=upcoming,
            recent_actions=list(recent_actions or []),
            sim_time_stalled_for_turns=sim_time_stalled_for_turns,
            last_verdict=last_verdict,
            tool_summary=list(tool_names or []),
        )

    # ------------------------------------------------------------------
    # Delta briefing
    # ------------------------------------------------------------------

    def build_delta_briefing(
        self,
        actor_id: str,
        since: int,
        *,
        now_sim_time: int | None = None,
        last_verdict: dict[str, Any] | None = None,
        tool_names: list[str] | None = None,
        recent_actions: list["BriefingRecentAction"] | None = None,
        sim_time_stalled_for_turns: int = 0,
    ) -> Briefing:
        """Build a delta-flavoured briefing: "what changed since `since`."

        Same `Briefing` shape as `build(...)`; differs in:
          - `since_sim_time` is set (rendered prose framing changes).
          - `unread_chats` includes only messages with `sim_time > since`
            that are visible to `actor_id`.
          - `unread_emails` includes only emails to/cc the actor with
            `sim_time > since`.
          - `project_board` is filtered to tasks whose status changed since
            `since`. The shape per item is unchanged (priority, status,
            assignee, blocked) so renderers can reuse the same code path.
          - `upcoming_calendar` is the next 4 sim-hours of events the actor
            attends (independent of `since`; the agent always wants the
            forward window in view).
          - `notifications_new` is the list of unseen notifications addressed
            to the actor with `created_at > since`.
          - `current_busy_state` reflects the actor's `busy_until` field plus
            (if applicable) the calendar event covering `now_sim_time`.

        `open_commitments` is intentionally still the *current* commitments —
        a delta should call out unfulfilled obligations even if they
        originated before `since`, because forgetting them is the failure mode
        the field exists to prevent.
        """
        now = now_sim_time if now_sim_time is not None else (
            self._scheduler.sim_time if self._scheduler is not None else since
        )
        actor = self.world.get_person(actor_id)
        assert actor is not None, f"unknown actor: {actor_id}"

        # New messages, scoped to channels the actor can see, after `since`.
        delta_chats = self._delta_chats(actor_id, since)
        # New emails to/cc actor after `since`.
        delta_emails = self._delta_emails(actor_id, since)
        # Tasks whose status changed since `since` — surfaced as the
        # delta-flavoured project board.
        delta_board = self._delta_task_board(since)
        # Upcoming calendar (next 4 sim-hours from `now`).
        window_end = now + 240
        upcoming = [
            {
                "event_id": e.id, "title": e.title,
                "start_sim_time": e.start_sim_time, "end_sim_time": e.end_sim_time,
                "agenda": e.agenda,
            }
            for e in self.world.calendar.values()
            if actor_id in e.attendees
            and now <= e.start_sim_time <= window_end
        ]
        upcoming.sort(key=lambda d: d["start_sim_time"])

        # New notifications since the watermark.
        new_notifs: list[Notification] = [
            n for n in self.world.notifications.values()
            if n.recipient_id == actor_id
            and not n.seen
            and n.created_at > since
        ]
        new_notifs.sort(key=lambda n: (n.created_at, n.id))

        # Open commitments — always the *current* state.
        commitments = self._open_commitments(actor_id, now)

        # Current busy state.
        busy_state = self._current_busy_state(actor, now)

        return Briefing(
            now_sim_time=now,
            now_label=self._wall_clock_label(now),
            end_sim_time=self.end_sim_time,
            agent_persona={
                "id": actor.id, "display_name": actor.display_name,
                "role": actor.role, "team": actor.team,
                "persona_notes": actor.persona_notes,
            },
            unread_notifications_count=len(new_notifs),
            unread_chats=delta_chats,
            unread_emails=delta_emails,
            open_commitments=commitments,
            project_board=delta_board,
            upcoming_calendar=upcoming,
            recent_actions=list(recent_actions or []),
            sim_time_stalled_for_turns=sim_time_stalled_for_turns,
            last_verdict=last_verdict,
            tool_summary=list(tool_names or []),
            since_sim_time=since,
            notifications_new=[
                {
                    "id": n.id, "kind": n.kind,
                    "created_at": n.created_at, "payload": dict(n.payload),
                }
                for n in new_notifs
            ],
            current_busy_state=busy_state,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _delta_chats(self, actor_id: str, since: int) -> list[dict[str, Any]]:
        """Messages with `sim_time > since` visible to `actor_id`, grouped
        by channel — analogous to `_unread_chats` but scoped by time rather
        than by the read_by cursor. We deliberately include messages the
        actor has already marked read: the delta is "what arrived while you
        were away", not "what you haven't acknowledged."
        """
        out: list[dict[str, Any]] = []
        for channel in sorted(self.world.channels.values(), key=lambda c: c.id):
            # Visible if non-private, or if actor is a member.
            if channel.is_private and actor_id not in channel.members:
                continue
            new_messages: list[Message] = [
                m for m in self.world.messages_in_channel(channel.id)
                if m.sim_time > since and m.sender_id != actor_id
            ]
            if not new_messages:
                continue
            out.append({
                "channel_id": channel.id, "channel_name": channel.name,
                "is_dm": channel.is_dm, "count": len(new_messages),
                "messages": [
                    {
                        "id": m.id, "sender_id": m.sender_id,
                        "sim_time": m.sim_time, "body": m.body,
                    }
                    for m in new_messages[-10:]
                ],
            })
        return out

    def _delta_emails(self, actor_id: str, since: int) -> list[dict[str, Any]]:
        """Emails to/cc `actor_id` with `sim_time > since`. Deduped by thread
        — only the newest hit per thread is reported, matching `_unread_emails`'s
        shape so renderers don't need to branch.
        """
        out: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        for email in sorted(self.world.emails.values(), key=lambda e: (-e.sim_time, e.id)):
            if actor_id not in email.to and actor_id not in email.cc:
                continue
            if email.sim_time <= since:
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

    def _delta_task_board(self, since: int) -> list[BriefingProjectBoardItem]:
        """Tasks whose status changed since `since`.

        Status-change history is collected via `task_status_changed` events
        when a scheduler is wired in at construction time. When no scheduler
        was passed, we fall back to using `task.comments[-1].sim_time` as a
        proxy for "this task moved" — coarser, but still functional for
        sites that build the assembler standalone.
        """
        task_ids: list[str]
        if self._scheduler is not None:
            task_ids = [
                tid for (ts, tid, _f, _t) in self._task_status_history
                if ts > since
            ]
        else:
            task_ids = [
                t.id for t in self.world.tasks.values()
                if t.comments and t.comments[-1].sim_time > since
            ]
        # Dedupe while preserving the most recent ordering by status change.
        seen: set[str] = set()
        ordered_ids: list[str] = []
        for tid in reversed(task_ids):
            if tid in seen:
                continue
            seen.add(tid)
            ordered_ids.append(tid)
        ordered_ids.reverse()

        tasks_by_id = self.world.tasks

        def _is_blocked(task: Task) -> bool:
            return any(
                tasks_by_id.get(dep) is not None
                and tasks_by_id[dep].status != "Done"
                for dep in task.depends_on
            )

        out: list[BriefingProjectBoardItem] = []
        for tid in ordered_ids[: self.PROJECT_BOARD_CAP]:
            t = tasks_by_id.get(tid)
            if t is None:
                continue  # task was deleted (shouldn't happen, but be safe)
            out.append(BriefingProjectBoardItem(
                task_id=t.id, title=t.title, status=t.status,
                assignee_id=t.assignee_id, priority=t.priority,
                deadline_sim_time=t.deadline_sim_time,
                blocked=_is_blocked(t),
            ))
        return out

    def _current_busy_state(self, actor: Person, now: int) -> dict[str, Any] | None:
        """Build the `current_busy_state` payload for the delta briefing.

        Returns None when the actor isn't busy (`busy_until <= now`). When
        busy, returns at least `{busy_until: int}`; if a calendar event the
        actor attends covers `now`, also returns `current_event` with the
        event id, title, and end time. busy_until set by non-calendar reasons
        (e.g., a long action) reports `current_event=None`.
        """
        if actor.busy_until <= now:
            return None
        covering = None
        for evt in self.world.calendar.values():
            if actor.id not in evt.attendees:
                continue
            if evt.start_sim_time <= now < evt.end_sim_time:
                covering = evt
                break
        return {
            "busy_until": int(actor.busy_until),
            "current_event": (
                {
                    "event_id": covering.id,
                    "title": covering.title,
                    "end_sim_time": covering.end_sim_time,
                }
                if covering else None
            ),
        }

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

    # Maximum entries shown on the project board. Bounded so the briefing
    # doesn't grow unboundedly on big scenarios — the agent is expected to
    # use `tasks.list` only when it needs to see something the board cuts off.
    PROJECT_BOARD_CAP = 20

    _PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

    def _build_project_board(self) -> list[BriefingProjectBoardItem]:
        tasks_by_id = self.world.tasks

        def _is_blocked(task: Task) -> bool:
            return any(
                tasks_by_id.get(dep) is not None
                and tasks_by_id[dep].status != "Done"
                for dep in task.depends_on
            )

        not_done = [t for t in tasks_by_id.values() if t.status != "Done"]
        not_done.sort(key=lambda t: (
            self._PRIORITY_RANK.get(t.priority, 9),
            t.deadline_sim_time if t.deadline_sim_time is not None else 10**9,
            t.id,
        ))
        return [
            BriefingProjectBoardItem(
                task_id=t.id, title=t.title, status=t.status,
                assignee_id=t.assignee_id, priority=t.priority,
                deadline_sim_time=t.deadline_sim_time,
                blocked=_is_blocked(t),
            )
            for t in not_done[: self.PROJECT_BOARD_CAP]
        ]

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
    is_delta = briefing.since_sim_time is not None
    if is_delta:
        lines.append(
            f"# Briefing — {briefing.now_label} (sim_time={briefing.now_sim_time}) — DELTA"
        )
        lines.append("")
        lines.append(
            f"_Since your last check at sim_time={briefing.since_sim_time}, "
            "here is what's new. Treat this as a **react to changes** turn — "
            "you do not need to re-survey the world. Full state is still "
            "available via tool calls._"
        )
        lines.append("")
    else:
        lines.append(f"# Briefing — {briefing.now_label} (sim_time={briefing.now_sim_time})")
        lines.append("")
    lines.append(f"_End of scenario at sim_time={briefing.end_sim_time}._")
    lines.append("")
    lines.append(f"**You are {briefing.agent_persona['display_name']} ({briefing.agent_persona['role']}).**")
    lines.append("")
    lines.append(briefing.agent_persona.get("persona_notes", "") or "")
    lines.append("")
    if briefing.current_busy_state is not None:
        s = briefing.current_busy_state
        ev = s.get("current_event")
        if ev:
            lines.append(
                f"## Currently in `{ev['event_id']}` (\"{ev['title']}\") "
                f"until sim_time={s['busy_until']}."
            )
        else:
            lines.append(
                f"## Currently busy until sim_time={s['busy_until']}."
            )
        lines.append("")
    if briefing.sim_time_stalled_for_turns >= 3:
        lines.append(
            f"## ⚠️ Stall warning: you've spent {briefing.sim_time_stalled_for_turns} "
            f"consecutive turns at sim_time={briefing.now_sim_time}. "
            "Stop reading — take a write action (send, comment, update_status, "
            "edit) or call `idle.until` to defer to a later sim_time."
        )
        lines.append("")
    if briefing.recent_actions:
        # Last 10 actions in reverse (most recent first). For the most recent
        # 5 *read* tool calls we render the FULL result body (preserved on
        # BriefingRecentAction.result_full) — without this, agents re-read the
        # same artifact consecutively because result_summary truncates message
        # bodies mid-sentence. Older reads + all non-read calls keep the terse
        # result_summary.
        READ_TOOLS = {
            "chat.read", "email.read", "docs.read", "tasks.get",
            "calendar.get", "directory.get", "meetings.get_transcript",
        }
        last_10 = briefing.recent_actions[-10:][::-1]
        recent_reads_with_full: set[int] = set()
        read_seen = 0
        for i, a in enumerate(last_10):
            if a.tool in READ_TOOLS and a.result_full:
                if read_seen < 5:
                    recent_reads_with_full.add(i)
                read_seen += 1
        lines.append("## Your recent actions + results (most recent first)")
        lines.append(
            "_You already did these AND have the results below. Do NOT re-read "
            "the same artifact — use the result that's already in hand. Recent "
            "read results are shown in full; older reads are summarised._"
        )
        for i, a in enumerate(last_10):
            ok = "ok" if a.ok else f"ERR: {a.error}"
            lines.append(f"- turn {a.turn} (t={a.sim_time}): `{a.tool}` {a.args_summary} → {ok}")
            if i in recent_reads_with_full:
                indented = "\n".join("    " + ln for ln in (a.result_full or "").splitlines())
                lines.append(f"    full result:\n{indented}")
            elif a.result_summary:
                lines.append(f"    result: {a.result_summary}")
        lines.append("")
    if briefing.last_verdict:
        lines.append("## Last evaluator feedback")
        lines.append(f"_score={briefing.last_verdict.get('score')}, category={briefing.last_verdict.get('category')}_")
        lines.append(briefing.last_verdict.get("rationale", ""))
        lines.append("")
    if is_delta:
        lines.append(f"## New notifications since t={briefing.since_sim_time}: {briefing.unread_notifications_count}")
    else:
        lines.append(f"## Unread notifications: {briefing.unread_notifications_count}")
    lines.append("")
    if briefing.unread_chats:
        lines.append("## New messages" if is_delta else "## Unread chats")
        for ch in briefing.unread_chats:
            lines.append(f"### {ch['channel_name']} ({ch['count']} new)")
            for m in ch["messages"]:
                lines.append(f"- _{m['sender_id']} @ t={m['sim_time']}_: {m['body']}")
            lines.append("")
    if briefing.unread_emails:
        lines.append("## New email threads" if is_delta else "## Unread email threads")
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
    if briefing.project_board:
        if is_delta:
            lines.append("## Tasks that moved since your last check")
            lines.append("_Tasks whose status changed since "
                         f"sim_time={briefing.since_sim_time}._")
        else:
            lines.append("## Project board (open tasks across the team)")
            lines.append("_Live state, sorted by priority then deadline. Top "
                         f"{len(briefing.project_board)} shown._")
        for t in briefing.project_board:
            suffix_parts: list[str] = []
            if t.deadline_sim_time is not None:
                suffix_parts.append(f"due t={t.deadline_sim_time}")
            if t.blocked:
                suffix_parts.append("BLOCKED")
            suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            assignee = t.assignee_id or "unassigned"
            lines.append(
                f"- [{t.priority}] {t.task_id} — {t.title} "
                f"({t.status}, assignee={assignee}){suffix}"
            )
        lines.append("")
    if briefing.upcoming_calendar:
        lines.append("## Calendar (next 4h)")
        for e in briefing.upcoming_calendar:
            lines.append(f"- {e['title']} t={e['start_sim_time']}..{e['end_sim_time']} — {e['agenda'][:120]}")
        lines.append("")
    return "\n".join(lines)
