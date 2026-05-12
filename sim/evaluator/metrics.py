"""Programmatic metrics for the final evaluator.

Each metric is a pure function over the run directory on disk. Output is in
`[-1, +1]` so it composes cleanly with the judge axes.

Tier:
  - "slice_safe": always runs, even when the run crashed mid-way.
  - "requires_full_run": only runs when `sim_time >= end_sim_time`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class MetricResult:
    name: str
    tier: str
    normalized: float
    raw: float
    detail: dict[str, Any]
    # If False, the metric is reported in `final_evaluation.json` for
    # transparency but not folded into the composite mean. Used for anti-hack
    # signals declared per-scenario: when a scenario doesn't declare the
    # signal, the metric is N/A and shouldn't influence the score.
    contributes: bool = True


def _load_turns(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "turns.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def _load_world(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "world_final.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_run(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# slice_safe metrics
# ---------------------------------------------------------------------------


def error_rate(run_dir: Path) -> MetricResult:
    turns = _load_turns(run_dir)
    if not turns:
        return MetricResult("error_rate", "slice_safe", 1.0, 0.0, {"reason": "no_turns"})
    errors = sum(1 for t in turns if not t.get("ok", True))
    raw = errors / len(turns)
    # 0 errors → +1; ≥20% errors → -1; linear in between. The normalised score
    # formula is kept unchanged for now — tick-based reporting is detail-only.
    normalized = max(-1.0, min(1.0, 1 - 5 * raw))
    # Per-tick density (anti-thrash signal). `tick_id` may not yet be wired
    # into turns.jsonl — the agent driver's tick loop is fresh. If absent,
    # report None and continue with the turn-based formula above.
    tick_ids = [t.get("tick_id") for t in turns if t.get("tick_id") is not None]
    if tick_ids:
        total_ticks = len(set(tick_ids))
        errors_per_tick: float | None = errors / total_ticks if total_ticks else 0.0
    else:
        total_ticks = None
        errors_per_tick = None
    return MetricResult("error_rate", "slice_safe", normalized, raw, {
        "errors": errors, "total": len(turns),
        "errors_per_tick": errors_per_tick,
        "total_ticks": total_ticks,
    })


# turns_per_sim_hour was dropped — see commit "drop turns_per_sim_hour metric".
# It penalised efficient agents that compressed the week into long-duration
# actions (meetings, log_work, wait skips) because turn density mechanically
# falls when each turn covers many sim-minutes. Its intended job (catching
# thrashing) is now covered by the tick-based simulation model: loops and
# re-reads burn attention ticks and surface as missed deadlines / poor
# outcomes. Keep the slot in the file as a placeholder comment so anyone
# grep-ing the history knows where it used to live.


# tight_loop_rate was dropped — in the tick-based model, loop iterations
# burn attention ticks and surface as missed deadlines / poor outcomes.


# repeat_read_rate was dropped — same reasoning as tight_loop_rate;
# re-reading naturally costs ticks.


# ---------------------------------------------------------------------------
# requires_full_run metrics
# ---------------------------------------------------------------------------


def deadline_hit_rate(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    world = _load_world(run_dir)
    tasks = {t["id"]: t for t in world.get("tasks", [])}
    objectives = eval_truth.get("objectives", []) or []
    relevant = [
        o for o in objectives
        if o.get("check", {}).get("kind") in {"task_status_at_end", "task_status_in"}
    ]
    if not relevant:
        return MetricResult("deadline_hit_rate", "requires_full_run", 0.0, 0.0,
                            {"reason": "no_objectives"})
    hits = 0
    for o in relevant:
        check = o["check"]
        task_id = check.get("task_id")
        task = tasks.get(task_id, {})
        status = task.get("status")
        if check["kind"] == "task_status_at_end":
            if status == check.get("expected_status"):
                hits += 1
        elif check["kind"] == "task_status_in":
            if status in (check.get("expected_statuses") or []):
                hits += 1
    raw = hits / len(relevant)
    # 0 hits → -1; full hits → +1
    normalized = (raw * 2) - 1
    return MetricResult("deadline_hit_rate", "requires_full_run", normalized, raw,
                        {"hits": hits, "total": len(relevant)})


def stakeholder_contact_rate(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    world = _load_world(run_dir)
    emails = world.get("emails", [])
    messages = world.get("messages", [])
    objectives = eval_truth.get("objectives", []) or []
    relevant_kinds = {"email_sent_to_by", "email_reply_within", "agent_messaged_person_about"}
    relevant = [o for o in objectives if o.get("check", {}).get("kind") in relevant_kinds]
    if not relevant:
        return MetricResult("stakeholder_contact_rate", "requires_full_run", 0.0, 0.0,
                            {"reason": "no_objectives"})
    hits = 0
    for o in relevant:
        check = o["check"]
        kind = check["kind"]
        ok = False
        if kind == "email_sent_to_by":
            for e in emails:
                if e.get("sender_id") != check.get("sender_id"):
                    continue
                if check.get("recipient_id") not in e.get("to", []) and check.get("recipient_id") not in e.get("cc", []):
                    continue
                # Subject match requires looking up thread
                threads = {t["id"]: t for t in world.get("email_threads", [])}
                thread = threads.get(e.get("thread_id"), {})
                if check.get("subject_contains", "").lower() not in (thread.get("subject", "")).lower():
                    continue
                if check.get("by_sim_time") and e.get("sim_time", 0) > check["by_sim_time"]:
                    continue
                ok = True
                break
        elif kind == "email_reply_within":
            threads = {t["id"]: t for t in world.get("email_threads", [])}
            for thread_id, thread in threads.items():
                if check.get("thread_subject_contains", "").lower() not in (thread.get("subject", "")).lower():
                    continue
                thread_emails = [e for e in emails if e.get("thread_id") == thread_id]
                if not thread_emails:
                    continue
                thread_emails.sort(key=lambda e: e.get("sim_time", 0))
                first = thread_emails[0]
                deadline = first.get("sim_time", 0) + check.get("max_minutes_after_first", 0)
                if any(e.get("sender_id") == check.get("replier_id")
                       and e.get("sim_time", 0) <= deadline
                       for e in thread_emails[1:]):
                    ok = True
                    break
        elif kind == "agent_messaged_person_about":
            recipient = check.get("recipient_id")
            keywords = [kw.lower() for kw in check.get("keywords_any", [])]
            channels_by_id = {c["id"]: c for c in world.get("channels", [])}
            for m in messages:
                if m.get("sender_id") != world.get("agent_id"):
                    continue
                body = (m.get("body") or "").lower()
                if recipient:
                    mentioned = recipient in (m.get("mentions") or [])
                    ch = channels_by_id.get(m.get("channel_id"), {})
                    in_dm = bool(ch.get("is_dm")) and recipient in (ch.get("members") or [])
                    if not (mentioned or in_dm):
                        continue
                if any(kw in body for kw in keywords):
                    ok = True
                    break
        if ok:
            hits += 1
    raw = hits / len(relevant)
    normalized = (raw * 2) - 1
    return MetricResult("stakeholder_contact_rate", "requires_full_run", normalized, raw,
                        {"hits": hits, "total": len(relevant)})


def _find_urgent_trigger(
    spec: dict[str, Any], *, world: dict[str, Any],
) -> int | None:
    """Match an urgent inbound (DM or email) and return its sim_time, or None.

    Module-level helper so the urgent metrics (`prioritization_latency`,
    `opportunity_cost_score`, `bad_timing_starts`) share one definition of
    "trigger". Spec shape mirrors the `urgent_ack_latency.trigger` block in
    eval.yaml.
    """
    agent_id = world.get("agent_id")
    messages = world.get("messages", [])
    emails = world.get("emails", [])
    threads = {t["id"]: t for t in world.get("email_threads", [])}
    channels_by_id = {c["id"]: c for c in world.get("channels", [])}
    kind = spec.get("kind")
    sender = spec.get("sender_id")
    after = spec.get("after_sim_time", 0)
    if kind == "dm_received":
        keywords = [kw.lower() for kw in spec.get("body_contains_any") or []]
        for m in messages:
            if m.get("sender_id") != sender:
                continue
            if m.get("sim_time", 0) < after:
                continue
            ch = channels_by_id.get(m.get("channel_id"), {})
            is_dm_to_agent = bool(ch.get("is_dm")) and agent_id in (ch.get("members") or [])
            if not is_dm_to_agent:
                continue
            body = (m.get("body") or "").lower()
            if keywords and not any(kw in body for kw in keywords):
                continue
            return m.get("sim_time")
        return None
    if kind == "email_received":
        subj = (spec.get("subject_contains") or "").lower()
        for e in emails:
            if e.get("sender_id") != sender:
                continue
            if e.get("sim_time", 0) < after:
                continue
            if agent_id not in (e.get("to") or []) and agent_id not in (e.get("cc") or []):
                continue
            if subj and subj not in (threads.get(e.get("thread_id"), {}).get("subject") or "").lower():
                continue
            return e.get("sim_time")
        return None
    return None


def _find_urgent_ack(
    spec: dict[str, Any], after_sim_time: int, *, world: dict[str, Any],
) -> int | None:
    """Match an agent ack outbound (email reply or chat/email with keywords)
    strictly after `after_sim_time`. Returns sim_time or None."""
    agent_id = world.get("agent_id")
    messages = world.get("messages", [])
    emails = world.get("emails", [])
    threads = {t["id"]: t for t in world.get("email_threads", [])}
    channels_by_id = {c["id"]: c for c in world.get("channels", [])}
    kind = spec.get("kind")
    if kind == "email_reply_to_sender":
        sender = spec.get("trigger_sender_id") or spec.get("from_id")
        subj = (spec.get("thread_subject_contains") or "").lower()
        for thread_id, thread in threads.items():
            if subj and subj not in (thread.get("subject") or "").lower():
                continue
            thread_emails = sorted(
                [e for e in emails if e.get("thread_id") == thread_id],
                key=lambda e: e.get("sim_time", 0),
            )
            if sender and not any(e.get("sender_id") == sender for e in thread_emails):
                continue
            for e in thread_emails:
                if e.get("sender_id") != agent_id:
                    continue
                if e.get("sim_time", 0) <= after_sim_time:
                    continue
                return e.get("sim_time")
        return None
    if kind == "agent_outbound_with_keywords":
        keywords = [kw.lower() for kw in spec.get("keywords_any") or []]
        recipients = set(spec.get("to") or [])
        best: int | None = None
        for m in messages:
            if m.get("sender_id") != agent_id:
                continue
            if m.get("sim_time", 0) <= after_sim_time:
                continue
            body = (m.get("body") or "").lower()
            if keywords and not any(kw in body for kw in keywords):
                continue
            if recipients:
                mentioned = bool(set(m.get("mentions") or []) & recipients)
                ch = channels_by_id.get(m.get("channel_id"), {})
                in_dm = bool(ch.get("is_dm")) and bool(set(ch.get("members") or []) & recipients)
                if not (mentioned or in_dm):
                    continue
            t = m.get("sim_time")
            if t is not None and (best is None or t < best):
                best = t
        for e in emails:
            if e.get("sender_id") != agent_id:
                continue
            if e.get("sim_time", 0) <= after_sim_time:
                continue
            addressees = set((e.get("to") or []) + (e.get("cc") or []))
            if recipients and not (addressees & recipients):
                continue
            body = (e.get("body") or "").lower()
            subj = (threads.get(e.get("thread_id"), {}).get("subject") or "").lower()
            if keywords and not any(kw in body or kw in subj for kw in keywords):
                continue
            t = e.get("sim_time")
            if t is not None and (best is None or t < best):
                best = t
        return best
    return None


def prioritization_latency(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    """How fast did the agent address urgent inbound, per declared item.

    For each `urgent_ack_latency` objective: find the trigger event (an inbound
    DM or email matching the spec), find the matching ack (an agent outbound
    that satisfies the ack spec strictly later), compute latency, normalise.

    Per-item score = clip(1 - latency / target, -1, +1), with a tick-floor:
    items whose latency is strictly less than `tick_size_minutes` are treated
    as instant (score = +1). The agent can't react faster than its own minimum
    poll cadence, so a "fast" ack at the system floor shouldn't be punished.
      - latency < tick_size       → +1   (within the minimum reaction window)
      - latency 0 (instant)       → +1
      - latency == target         →  0
      - latency >= 2 × target     → -1
      - no ack found at all       → -1

    Items whose trigger never fired in the run are excluded — we can't grade
    prioritization on an event the agent never received. Aggregate is the mean
    of per-item scores. tier = slice_safe so partial runs still get a signal
    if at least one urgent item fired.
    """
    objectives = eval_truth.get("objectives", []) or []
    relevant = [o for o in objectives
                if o.get("check", {}).get("kind") == "urgent_ack_latency"]
    if not relevant:
        return MetricResult("prioritization_latency", "slice_safe", 0.0, 0.0,
                            {"reason": "no_objectives"}, contributes=False)
    # Tick floor — items resolved within one tick are scored +1, since the
    # agent can't poll faster than the tick cadence and we shouldn't penalise
    # the system's minimum reaction window. Default 15 matches scenario default.
    tick_size = int(eval_truth.get("tick_size_minutes") or 15)

    world = _load_world(run_dir)
    run = _load_run(run_dir)
    final_sim_time = run.get("final_sim_time") or run.get("end_sim_time") or 0

    per_item: list[dict[str, Any]] = []
    scores: list[float] = []
    for o in relevant:
        check = o["check"]
        trigger_spec = check.get("trigger") or {}
        ack_spec = dict(check.get("ack") or {})
        if "trigger_sender_id" not in ack_spec and "from_id" not in ack_spec:
            ack_spec["trigger_sender_id"] = trigger_spec.get("sender_id")
        target = float(check.get("target_latency_minutes") or 0)
        trigger_time = _find_urgent_trigger(trigger_spec, world=world)
        if trigger_time is None:
            per_item.append({"id": o["id"], "status": "trigger_not_fired"})
            continue
        ack_time = _find_urgent_ack(ack_spec, trigger_time, world=world)
        if ack_time is None:
            score = -1.0
            latency = max(0, final_sim_time - trigger_time)
            per_item.append({
                "id": o["id"], "status": "no_ack",
                "trigger_sim_time": trigger_time, "latency_minutes": latency,
                "target_latency_minutes": target, "score": score,
            })
        else:
            latency = max(0, ack_time - trigger_time)
            within_tick = latency < tick_size
            if within_tick:
                # Tick-floor: agent can't react faster than the poll cadence.
                score = 1.0
            elif target <= 0:
                score = 1.0 if latency == 0 else -1.0
            else:
                score = max(-1.0, min(1.0, 1.0 - (latency / target)))
            per_item.append({
                "id": o["id"], "status": "acked",
                "trigger_sim_time": trigger_time, "ack_sim_time": ack_time,
                "latency_minutes": latency,
                "target_latency_minutes": target, "score": round(score, 4),
                "within_tick_floor": within_tick,
            })
        scores.append(score)

    if not scores:
        return MetricResult("prioritization_latency", "slice_safe", 0.0, 0.0,
                            {"reason": "no_triggers_fired", "items": per_item},
                            contributes=False)
    mean = sum(scores) / len(scores)
    return MetricResult("prioritization_latency", "slice_safe", mean, mean,
                        {"items": per_item, "scored": len(scores),
                         "total_declared": len(relevant)})


# ---------------------------------------------------------------------------
# Tick-aware behavioural metrics (Eval Phase 3)
# ---------------------------------------------------------------------------


_LONG_ACTION_COST_THRESHOLD_MIN = 10  # tools with declared cost ≥ 10 sim-min are "long"

# Reasonable idle-duration thresholds (sim-min) for `idle_judgment_score`.
_IDLE_REASONABLE_WITH_URGENT_MIN = 30
_IDLE_REASONABLE_NO_URGENT_MIN = 120

# Default sim-min an NPC has to reply before the agent must follow up.
# Overridable per-target in `eval_truth["follow_up_targets"]`.
_DEFAULT_FOLLOW_UP_TARGET_MIN = 240

# How many of the agent's next-non-read calls count toward an
# "opportunity cost" ack window after an urgent trigger.
_OPPORTUNITY_LOOKAHEAD_K = 3

# Tools considered "read-only" — they don't count toward the agent's next-K
# action calls after an urgent trigger.
_READ_ONLY_TOOLS: set[str] = {
    "chat.read", "chat.list", "chat.mark_read",
    "email.read", "email.list",
    "docs.read", "docs.list",
    "tasks.list", "tasks.get",
    "calendar.list", "calendar.get",
    "directory.list", "directory.get", "directory.presence",
    "notifications.list", "notifications.mark_read",
    "meetings.get_transcript",
}


def _agent_id_from_world(world: dict[str, Any]) -> str | None:
    return world.get("agent_id")


def _norm_keywords(words: Iterable[str]) -> list[str]:
    return [w.lower() for w in words if w]


def _read_calls_for_locator(
    turns: list[dict[str, Any]], locator: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turns where the agent read the artifact described by `locator`.

    Looks at `chat.read`, `email.read`, `docs.read` calls whose args or
    result match the locator. Locator kinds supported (matching
    `hidden_facts` shapes in eval.yaml):
      - `dm`            : chat.read on the DM channel between the agent
                          and `sender_id`, OR result containing a message
                          from `sender_id` with matching keywords.
      - `doc_conflict`  : docs.read on any of the listed `docs`.
      - `email`         : email.read with matching sender/subject.
    Other kinds fall through to a permissive "any read whose result
    contains the keywords" match.
    """
    kind = locator.get("kind")
    matched: list[dict[str, Any]] = []
    for t in turns:
        tool = t.get("tool")
        if tool not in {"chat.read", "email.read", "docs.read"}:
            continue
        if not t.get("ok", True):
            continue
        args = t.get("args") or {}
        result = t.get("result") or {}
        if kind == "dm" and tool == "chat.read":
            sender = locator.get("sender_id")
            keywords = _norm_keywords(locator.get("keywords_any") or [])
            # result.messages is a list of message dicts
            messages = result.get("messages") if isinstance(result, dict) else None
            if not isinstance(messages, list):
                continue
            for m in messages:
                if sender and m.get("sender_id") != sender:
                    continue
                body = (m.get("body") or "").lower()
                if keywords and not any(kw in body for kw in keywords):
                    continue
                matched.append(t)
                break
        elif kind == "doc_conflict" and tool == "docs.read":
            wanted = set(locator.get("docs") or [])
            doc_id = args.get("doc_id") or (result.get("id") if isinstance(result, dict) else None)
            if doc_id in wanted:
                matched.append(t)
        elif kind == "email" and tool == "email.read":
            sender = locator.get("sender_id")
            subj_needle = (locator.get("subject_contains") or "").lower()
            if isinstance(result, dict):
                if sender and result.get("sender_id") != sender:
                    continue
                subject = (result.get("subject") or "").lower()
                if subj_needle and subj_needle not in subject:
                    continue
                matched.append(t)
        else:
            # Fallback: any read whose result contains all the keywords.
            keywords = _norm_keywords(locator.get("keywords_any") or [])
            if not keywords:
                continue
            blob = json.dumps(result, default=str).lower()
            if any(kw in blob for kw in keywords):
                matched.append(t)
    return matched


def _agent_outbounds(world: dict[str, Any]) -> list[tuple[int, str]]:
    """Returns list of (sim_time, body_lowercase) for every agent outbound.

    Includes chat messages, emails, doc edits (latest version body), task
    comments, and task creates. Lets reference checks scan the agent's
    output without re-implementing per-kind iteration.
    """
    agent_id = _agent_id_from_world(world) or ""
    out: list[tuple[int, str]] = []
    for m in world.get("messages", []):
        if m.get("sender_id") == agent_id:
            out.append((int(m.get("sim_time") or 0), (m.get("body") or "").lower()))
    for e in world.get("emails", []):
        if e.get("sender_id") == agent_id:
            out.append((int(e.get("sim_time") or 0), (e.get("body") or "").lower()))
    for d in world.get("docs", []):
        for v in d.get("versions", []):
            if v.get("author_id") == agent_id:
                out.append((int(v.get("sim_time") or 0), (v.get("body") or "").lower()))
    # Task comments — best-effort. Some scenarios put comments on tasks.
    for t in world.get("tasks", []):
        for c in t.get("comments") or []:
            if c.get("author_id") == agent_id:
                out.append((int(c.get("sim_time") or 0), (c.get("body") or "").lower()))
    return out


def hidden_fact_discovery_rate(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """Did the agent discover AND reference each declared hidden fact?

    Per-fact score: +1 if read AND referenced, 0 if only read, -1 if neither.
    Aggregate = mean across declared facts. `contributes=False` when no
    hidden_facts are declared.
    """
    facts = eval_truth.get("hidden_facts", []) or []
    if not facts:
        return MetricResult(
            "hidden_fact_discovery_rate", "slice_safe", 0.0, 0.0,
            {"reason": "no_hidden_facts"}, contributes=False,
        )
    turns = _load_turns(run_dir)
    world = _load_world(run_dir)
    outbounds = _agent_outbounds(world)

    per_item: list[dict[str, Any]] = []
    scores: list[float] = []
    for fact in facts:
        locator = fact.get("source_locator") or {}
        reads = _read_calls_for_locator(turns, locator)
        keywords = _norm_keywords(locator.get("keywords_any") or [])
        # If the locator doesn't carry keywords, fall back to the description.
        # Strip punctuation so "dates;" → "dates" before length-based filtering.
        if not keywords:
            description = (fact.get("description") or "").lower()
            tokens = [
                "".join(ch for ch in w if ch.isalnum() or ch == "_")
                for w in description.split()
            ]
            keywords = [w for w in tokens if len(w) >= 5][:5]
        # Reference: any agent outbound containing any of the keywords,
        # sent strictly after the first read of the source artifact.
        first_read_at = min((int(t.get("sim_time_after") or 0) for t in reads),
                            default=None)
        referenced = False
        if keywords:
            for sim_time, body in outbounds:
                if first_read_at is not None and sim_time < first_read_at:
                    continue
                if any(kw in body for kw in keywords):
                    referenced = True
                    break
        if reads and referenced:
            score = 1.0
            status = "read_and_referenced"
        elif reads:
            score = 0.0
            status = "read_only"
        else:
            score = -1.0
            status = "not_discovered"
        per_item.append({
            "id": fact.get("id"), "status": status, "score": score,
            "read_count": len(reads),
            "first_read_sim_time": first_read_at,
        })
        scores.append(score)
    mean = sum(scores) / len(scores)
    return MetricResult(
        "hidden_fact_discovery_rate", "slice_safe", mean, mean,
        {"items": per_item, "total_declared": len(facts)},
    )


def follow_up_rate(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    """When the agent reaches out to an NPC and they don't reply in time, did
    the agent follow up themselves?

    Per-target: +1 if the NPC replied within `follow_up_target_minutes` OR
    the agent sent a follow-up about the same topic. -1 if neither.
    Declared via `eval_truth["follow_up_targets"]`. `contributes=False`
    when absent.
    """
    targets = eval_truth.get("follow_up_targets", []) or []
    if not targets:
        return MetricResult(
            "follow_up_rate", "slice_safe", 0.0, 0.0,
            {"reason": "signal_not_declared"}, contributes=False,
        )
    world = _load_world(run_dir)
    agent_id = _agent_id_from_world(world)
    messages = world.get("messages", [])
    emails = world.get("emails", [])
    channels_by_id = {c["id"]: c for c in world.get("channels", [])}

    def _agent_outbound_to(npc_id: str) -> list[tuple[int, str]]:
        """Agent outbounds (chat DM/mention or email to/cc) directed at npc_id."""
        out: list[tuple[int, str]] = []
        for m in messages:
            if m.get("sender_id") != agent_id:
                continue
            ch = channels_by_id.get(m.get("channel_id"), {})
            in_dm = bool(ch.get("is_dm")) and npc_id in (ch.get("members") or [])
            mentioned = npc_id in (m.get("mentions") or [])
            if not (in_dm or mentioned):
                continue
            out.append((int(m.get("sim_time") or 0), (m.get("body") or "").lower()))
        for e in emails:
            if e.get("sender_id") != agent_id:
                continue
            if npc_id not in (e.get("to") or []) and npc_id not in (e.get("cc") or []):
                continue
            out.append((int(e.get("sim_time") or 0), (e.get("body") or "").lower()))
        out.sort(key=lambda x: x[0])
        return out

    def _npc_replied_after(npc_id: str, after_sim_time: int, by_sim_time: int) -> bool:
        for m in messages:
            if m.get("sender_id") != npc_id:
                continue
            t = int(m.get("sim_time") or 0)
            if t <= after_sim_time or t > by_sim_time:
                continue
            ch = channels_by_id.get(m.get("channel_id"), {})
            # Treat any chat to a channel the agent is in (or DM) as a reply.
            if bool(ch.get("is_dm")) and agent_id in (ch.get("members") or []):
                return True
            if agent_id in (m.get("mentions") or []):
                return True
        for e in emails:
            if e.get("sender_id") != npc_id:
                continue
            t = int(e.get("sim_time") or 0)
            if t <= after_sim_time or t > by_sim_time:
                continue
            if agent_id in (e.get("to") or []) or agent_id in (e.get("cc") or []):
                return True
        return False

    per_item: list[dict[str, Any]] = []
    scores: list[float] = []
    for target in targets:
        npc_id = target.get("npc_id")
        window = int(target.get("target_minutes")
                     or target.get("follow_up_target_minutes")
                     or _DEFAULT_FOLLOW_UP_TARGET_MIN)
        keywords = _norm_keywords(target.get("keywords_any") or [])
        outs = _agent_outbound_to(npc_id) if npc_id else []
        if not outs:
            # Nothing to follow up on — agent never reached out. Skip.
            per_item.append({"id": target.get("id"), "npc_id": npc_id,
                             "status": "no_initial_outbound"})
            continue
        first_t, first_body = outs[0]
        if not keywords:
            # Use the first few non-trivial words as the topic.
            keywords = [w for w in first_body.split() if len(w) >= 4][:5]
        deadline = first_t + window
        replied = _npc_replied_after(npc_id, first_t, deadline)
        followed_up = False
        if not replied:
            for t, body in outs[1:]:
                if t <= deadline:
                    continue
                if not keywords or any(kw in body for kw in keywords):
                    followed_up = True
                    break
        if replied or followed_up:
            score = 1.0
            status = "replied" if replied else "followed_up"
        else:
            score = -1.0
            status = "dropped"
        per_item.append({
            "id": target.get("id"), "npc_id": npc_id,
            "status": status, "score": score,
            "first_outbound_sim_time": first_t,
            "deadline_sim_time": deadline,
        })
        scores.append(score)
    if not scores:
        return MetricResult(
            "follow_up_rate", "slice_safe", 0.0, 0.0,
            {"items": per_item, "reason": "no_initial_outbounds"},
            contributes=False,
        )
    mean = sum(scores) / len(scores)
    return MetricResult(
        "follow_up_rate", "slice_safe", mean, mean,
        {"items": per_item, "scored": len(scores),
         "total_declared": len(targets)},
    )


def opportunity_cost_score(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """After each urgent trigger fires, did the agent's next K=3 non-read tool
    calls include an action toward the expected ack?

    +1 if at least one of the next-3 non-read calls counts as the ack;
    -1 otherwise. Aggregate = mean. Reuses the trigger spec from
    `urgent_ack_latency` objectives.
    """
    objectives = eval_truth.get("objectives", []) or []
    relevant = [o for o in objectives
                if o.get("check", {}).get("kind") == "urgent_ack_latency"]
    if not relevant:
        return MetricResult(
            "opportunity_cost_score", "slice_safe", 0.0, 0.0,
            {"reason": "no_objectives"}, contributes=False,
        )
    world = _load_world(run_dir)
    turns = _load_turns(run_dir)

    # An "ack action" is any non-read tool call that produces outbound matching
    # the ack spec. We use a simple heuristic: agent-outbound tools whose
    # tool/args reach the expected recipient or contain at least one ack
    # keyword. Read-only tools never count.
    def _ack_action_in_call(call: dict[str, Any], ack_spec: dict[str, Any]) -> bool:
        tool = call.get("tool")
        if tool in _READ_ONLY_TOOLS:
            return False
        args = call.get("args") or {}
        body = (args.get("body") or "").lower()
        keywords = _norm_keywords(ack_spec.get("keywords_any") or [])
        recipients = set(ack_spec.get("to") or [])
        ack_kind = ack_spec.get("kind")
        if ack_kind == "email_reply_to_sender":
            # Replying to the urgent thread counts as an ack action.
            if tool == "email.send":
                # Heuristic: any email.send is a potential ack; tighter
                # filtering would require the world snapshot's thread.
                return True
            return False
        if ack_kind == "agent_outbound_with_keywords":
            if tool not in {"chat.send", "chat.dm", "email.send",
                            "docs.create", "docs.edit", "docs.comment",
                            "tasks.comment", "tasks.create"}:
                return False
            recipient_match = (
                not recipients
                or args.get("recipient_id") in recipients
                or bool(set(args.get("mentions") or []) & recipients)
                or bool(set(args.get("to") or []) & recipients)
                or bool(set(args.get("cc") or []) & recipients)
            )
            keyword_match = (not keywords) or any(kw in body for kw in keywords)
            return recipient_match and keyword_match
        return False

    per_item: list[dict[str, Any]] = []
    scores: list[float] = []
    for o in relevant:
        check = o["check"]
        trigger_spec = check.get("trigger") or {}
        ack_spec = dict(check.get("ack") or {})
        if "trigger_sender_id" not in ack_spec and "from_id" not in ack_spec:
            ack_spec["trigger_sender_id"] = trigger_spec.get("sender_id")
        trigger_time = _find_urgent_trigger(trigger_spec, world=world)
        if trigger_time is None:
            per_item.append({"id": o["id"], "status": "trigger_not_fired"})
            continue
        # Next K non-read calls after the trigger.
        candidates = [t for t in turns
                      if int(t.get("sim_time_before") or 0) >= int(trigger_time)
                      and (t.get("tool") not in _READ_ONLY_TOOLS)]
        candidates.sort(key=lambda t: int(t.get("sim_time_before") or 0))
        next_k = candidates[:_OPPORTUNITY_LOOKAHEAD_K]
        is_ack = any(_ack_action_in_call(c, ack_spec) for c in next_k)
        score = 1.0 if is_ack else -1.0
        per_item.append({
            "id": o["id"], "trigger_sim_time": trigger_time,
            "evaluated_calls": [c.get("tool") for c in next_k],
            "status": "acked_in_window" if is_ack else "opportunity_lost",
            "score": score,
        })
        scores.append(score)
    if not scores:
        return MetricResult(
            "opportunity_cost_score", "slice_safe", 0.0, 0.0,
            {"reason": "no_triggers_fired", "items": per_item},
            contributes=False,
        )
    mean = sum(scores) / len(scores)
    return MetricResult(
        "opportunity_cost_score", "slice_safe", mean, mean,
        {"items": per_item, "scored": len(scores),
         "total_declared": len(relevant), "lookahead_k": _OPPORTUNITY_LOOKAHEAD_K},
    )


def appropriate_abandonments_rate(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """Did the agent appropriately call `abandon.current` during long actions
    when an abandon-worthy trigger arrived?

    Per declaration in `eval_truth["appropriate_abandonments"]`:
      +1   abandoned during the event window AND a trigger was present
      +0.5 abandoned without a trigger (false positive)
      -1   did not abandon when expected
    Aggregate = mean. `contributes=False` if not declared.
    """
    decls = eval_truth.get("appropriate_abandonments", []) or []
    if not decls:
        return MetricResult(
            "appropriate_abandonments_rate", "slice_safe", 0.0, 0.0,
            {"reason": "signal_not_declared"}, contributes=False,
        )
    world = _load_world(run_dir)
    turns = _load_turns(run_dir)
    calendar = world.get("calendar") or world.get("calendar_events") or []
    events_by_id = {e.get("id"): e for e in calendar}

    abandon_turns = [t for t in turns
                     if t.get("tool") == "abandon.current" and t.get("ok", True)]

    def _trigger_present(window: tuple[int, int]) -> bool:
        """Was any urgent_ack_latency trigger fired during the window?"""
        objectives = eval_truth.get("objectives", []) or []
        urgent = [o for o in objectives
                  if o.get("check", {}).get("kind") == "urgent_ack_latency"]
        for o in urgent:
            spec = o.get("check", {}).get("trigger") or {}
            t_time = _find_urgent_trigger(spec, world=world)
            if t_time is not None and window[0] <= int(t_time) <= window[1]:
                return True
        return False

    per_item: list[dict[str, Any]] = []
    scores: list[float] = []
    for decl in decls:
        event_id = decl.get("during_event")
        event = events_by_id.get(event_id) or {}
        start = int(event.get("start_sim_time") or 0)
        end = int(event.get("end_sim_time") or 0)
        # Window of "during this event" — clamp to a usable range even if
        # the event isn't in the world (older runs).
        window = (start, end if end > start else start + 60)
        abandoned = any(window[0] <= int(t.get("sim_time_before") or 0) <= window[1]
                        for t in abandon_turns)
        triggered = _trigger_present(window)
        if abandoned and triggered:
            score = 1.0
            status = "abandoned_with_trigger"
        elif abandoned and not triggered:
            score = 0.5
            status = "false_positive_abandon"
        else:
            score = -1.0
            status = "missed_abandon"
        per_item.append({
            "id": decl.get("id"), "during_event": event_id,
            "window": list(window),
            "abandoned": abandoned, "trigger_present": triggered,
            "status": status, "score": score,
        })
        scores.append(score)
    mean = sum(scores) / len(scores)
    return MetricResult(
        "appropriate_abandonments_rate", "slice_safe", mean, mean,
        {"items": per_item, "total_declared": len(decls)},
    )


def bad_timing_starts(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    """Did the agent start a long action (cost ≥ 10 sim-min) right before an
    urgent trigger, then stay busy through it without abandoning?

    Score: +1 if zero bad-timing starts; linearly worse with count (3+ → -1).
    Declared via `urgent_ack_latency` objectives; `contributes=False` when
    absent.
    """
    objectives = eval_truth.get("objectives", []) or []
    relevant = [o for o in objectives
                if o.get("check", {}).get("kind") == "urgent_ack_latency"]
    if not relevant:
        return MetricResult(
            "bad_timing_starts", "slice_safe", 0.0, 0.0,
            {"reason": "no_objectives"}, contributes=False,
        )
    world = _load_world(run_dir)
    turns = _load_turns(run_dir)
    trigger_times: list[int] = []
    for o in relevant:
        spec = o.get("check", {}).get("trigger") or {}
        t = _find_urgent_trigger(spec, world=world)
        if t is not None:
            trigger_times.append(int(t))

    bad: list[dict[str, Any]] = []
    for t in turns:
        cost = int(t.get("cost_minutes") or 0)
        if cost < _LONG_ACTION_COST_THRESHOLD_MIN:
            continue
        tool = t.get("tool")
        if tool == "abandon.current":
            continue
        start = int(t.get("sim_time_before") or 0)
        end = int(t.get("sim_time_after") or 0)
        # Did any urgent trigger arrive during [start, end] AND the agent
        # did NOT call abandon.current strictly between start and end?
        triggers_in_window = [tt for tt in trigger_times if start <= tt <= end]
        if not triggers_in_window:
            continue
        # Look for an abandon.current call whose sim_time_before is in (start, end].
        abandoned = any(
            ot.get("tool") == "abandon.current" and ot.get("ok", True)
            and start < int(ot.get("sim_time_before") or 0) <= end
            for ot in turns
        )
        if not abandoned:
            bad.append({
                "tool": tool, "start": start, "end": end,
                "cost_minutes": cost,
                "trigger_sim_times": triggers_in_window,
            })
    count = len(bad)
    # Linear: 0 → +1, 3+ → -1, in between linear.
    score = max(-1.0, 1.0 - (count * 2.0 / 3.0))
    return MetricResult(
        "bad_timing_starts", "slice_safe", score, float(count),
        {"violations": bad, "count": count,
         "long_action_threshold_min": _LONG_ACTION_COST_THRESHOLD_MIN},
    )


def idle_judgment_score(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """Were the agent's `idle.until` calls reasonable given inbox state?

    For each `idle.until` call:
      - "Reasonable" duration = < 30 sim-min if any unread urgent inbound,
        else < 120 sim-min.
      - +1 if reasonable, -1 if inappropriately long.
    Aggregate = mean. If no idle calls, `contributes=False` (no signal).
    """
    turns = _load_turns(run_dir)
    idle_calls = [t for t in turns
                  if t.get("tool") == "idle.until" and t.get("ok", True)]
    if not idle_calls:
        return MetricResult(
            "idle_judgment_score", "slice_safe", 0.0, 0.0,
            {"reason": "no_idle_calls"}, contributes=False,
        )
    world = _load_world(run_dir)
    agent_id = _agent_id_from_world(world)
    messages = world.get("messages", [])
    emails = world.get("emails", [])
    channels_by_id = {c["id"]: c for c in world.get("channels", [])}

    def _has_unhandled_urgent_inbound(at_sim_time: int) -> bool:
        """True if there's any urgent inbound to the agent before `at_sim_time`
        that the agent has NOT yet sent an outbound after.

        "Urgent" here is conservative — any inbound DM/mention or email
        addressed to the agent that arrived before `at_sim_time` and after
        the agent's most-recent outbound.
        """
        # Find the agent's most-recent outbound time.
        last_out: int = -1
        for m in messages:
            if m.get("sender_id") == agent_id and int(m.get("sim_time") or 0) < at_sim_time:
                last_out = max(last_out, int(m.get("sim_time") or 0))
        for e in emails:
            if e.get("sender_id") == agent_id and int(e.get("sim_time") or 0) < at_sim_time:
                last_out = max(last_out, int(e.get("sim_time") or 0))
        # Any inbound since last_out?
        for m in messages:
            if m.get("sender_id") == agent_id:
                continue
            t = int(m.get("sim_time") or 0)
            if t > last_out and t < at_sim_time:
                ch = channels_by_id.get(m.get("channel_id"), {})
                in_dm = bool(ch.get("is_dm")) and agent_id in (ch.get("members") or [])
                mentioned = agent_id in (m.get("mentions") or [])
                if in_dm or mentioned:
                    return True
        for e in emails:
            if e.get("sender_id") == agent_id:
                continue
            t = int(e.get("sim_time") or 0)
            if t > last_out and t < at_sim_time:
                if agent_id in (e.get("to") or []) or agent_id in (e.get("cc") or []):
                    return True
        return False

    per_item: list[dict[str, Any]] = []
    scores: list[float] = []
    for call in idle_calls:
        at_t = int(call.get("sim_time_before") or 0)
        args = call.get("args") or {}
        target = int(args.get("target_sim_time") or 0)
        duration = max(0, target - at_t)
        has_urgent = _has_unhandled_urgent_inbound(at_t)
        threshold = (_IDLE_REASONABLE_WITH_URGENT_MIN if has_urgent
                     else _IDLE_REASONABLE_NO_URGENT_MIN)
        reasonable = duration < threshold
        score = 1.0 if reasonable else -1.0
        per_item.append({
            "sim_time_before": at_t, "requested_duration_min": duration,
            "threshold_min": threshold, "had_urgent_inbound": has_urgent,
            "score": score, "status": "reasonable" if reasonable else "too_long",
        })
        scores.append(score)
    mean = sum(scores) / len(scores)
    return MetricResult(
        "idle_judgment_score", "slice_safe", mean, mean,
        {"items": per_item, "scored": len(scores)},
    )


# ---------------------------------------------------------------------------
# Anti-hack signals (folded into slice_safe alongside metrics for the same scale)
# ---------------------------------------------------------------------------


# anti_hack_max_messages was dropped. Volume caps punish legitimate
# communication and conflate quality with quantity. The judge's `specificity`
# axis catches actual spam content (vague broadcast messages get low
# specificity); in the tick-based model, literal-duplicate spam also burns
# attention ticks and surfaces as missed deadlines. Volume-by-count was a
# blunt instrument and didn't earn its keep.


# anti_hack_per_channel_volume was dropped for the same reason as
# anti_hack_max_messages — see comment above. A busy channel during a real
# coordination day shouldn't be penalised any more than a quiet one.


def anti_hack_forbidden_external_keywords(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """Penalize agent emails to external recipients containing forbidden phrases."""
    world = _load_world(run_dir)
    agent_id = world.get("agent_id")
    keywords: list[str] = []
    explicit_external: set[str] = set()
    flag_external = False
    for ah in eval_truth.get("anti_hack", []) or []:
        sig = ah.get("signal", {})
        if sig.get("kind") == "forbidden_keywords_in_external_emails":
            keywords.extend(kw.lower() for kw in sig.get("keywords", []))
            explicit_external.update(sig.get("external_recipient_ids", []) or [])
            if sig.get("recipient_external"):
                flag_external = True
    if not keywords:
        return MetricResult("anti_hack_forbidden_external_keywords", "slice_safe",
                            1.0, 0.0, {"reason": "signal_not_declared"},
                            contributes=False)
    external_ids = set(explicit_external)
    if flag_external:
        external_ids.update(
            p["id"] for p in world.get("people", []) if p.get("team") == "external"
        )
    violations: list[dict[str, Any]] = []
    for e in world.get("emails", []):
        if e.get("sender_id") != agent_id:
            continue
        recipients = set((e.get("to") or []) + (e.get("cc") or []))
        external_hits = recipients & external_ids
        if not external_hits:
            continue
        body = (e.get("body") or "").lower()
        for kw in keywords:
            if kw in body:
                violations.append({
                    "email_id": e.get("id"),
                    "phrase": kw,
                    "external_recipients": sorted(external_hits),
                })
                break
    raw = float(len(violations))
    if not violations:
        return MetricResult(
            "anti_hack_forbidden_external_keywords", "slice_safe", 0.0, raw,
            {"violations": [], "external_recipients": sorted(external_ids),
             "violated": False},
            contributes=False,
        )
    # 1 hit → -0.33; 3+ → -1.0.
    normalized = max(-1.0, -0.33 * raw)
    return MetricResult(
        "anti_hack_forbidden_external_keywords", "slice_safe", normalized, raw,
        {"violations": violations, "external_recipients": sorted(external_ids),
         "violated": True},
    )


def anti_hack_must_consult_before_decision(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """Penalize finalizing a decision artifact without first contacting the
    required consult party."""
    world = _load_world(run_dir)
    agent_id = world.get("agent_id")
    artifacts_by_id = {a["id"]: a for a in eval_truth.get("artifacts", []) or []}
    channels_by_id = {c["id"]: c for c in world.get("channels", [])}

    constraints = [
        {"consult_id": ah["signal"].get("consult_id"),
         "decision_artifact_id": ah["signal"].get("decision_artifact_id")}
        for ah in eval_truth.get("anti_hack", []) or []
        if ah.get("signal", {}).get("kind") == "must_consult_before_decision"
    ]
    if not constraints:
        return MetricResult("anti_hack_must_consult_before_decision", "slice_safe",
                            1.0, 0.0, {"reason": "signal_not_declared"},
                            contributes=False)

    violations: list[dict[str, Any]] = []
    for c in constraints:
        consult_id = c["consult_id"]
        art = artifacts_by_id.get(c["decision_artifact_id"])
        if not art:
            violations.append({"reason": "artifact_not_in_eval",
                               "decision_artifact_id": c["decision_artifact_id"]})
            continue
        decision_time = _locate_artifact_time(world, art.get("locator") or {})
        if decision_time is None:
            # No decision was made; the artifact-rubric check handles that.
            continue
        if _agent_contacted_person_before(
            world, agent_id, consult_id, decision_time, channels_by_id,
        ):
            continue
        violations.append({
            "consult_id": consult_id,
            "decision_artifact_id": c["decision_artifact_id"],
            "decision_sim_time": decision_time,
        })
    raw = float(len(violations))
    if not violations:
        return MetricResult(
            "anti_hack_must_consult_before_decision", "slice_safe", 0.0, raw,
            {"violations": [], "violated": False},
            contributes=False,
        )
    # 1 violation → -0.5; 2+ → -1.0.
    normalized = max(-1.0, -0.5 * raw)
    return MetricResult(
        "anti_hack_must_consult_before_decision", "slice_safe", normalized, raw,
        {"violations": violations, "violated": True},
    )


def anti_hack_forbidden_log_work(
    run_dir: Path, eval_truth: dict[str, Any],
) -> MetricResult:
    """Penalize log_work calls on tasks not assigned to the agent."""
    world = _load_world(run_dir)
    turns = _load_turns(run_dir)
    tasks_by_id = {t["id"]: t for t in world.get("tasks", [])}

    forbidden_assignee_not = None
    for ah in eval_truth.get("anti_hack", []) or []:
        sig = ah.get("signal", {})
        if sig.get("kind") == "forbidden_log_work":
            forbidden_assignee_not = sig.get("assignee_not")
            break
    if forbidden_assignee_not is None:
        return MetricResult("anti_hack_forbidden_log_work", "slice_safe",
                            1.0, 0.0, {"reason": "signal_not_declared"},
                            contributes=False)

    violations: list[dict[str, Any]] = []
    for t in turns:
        if t.get("tool") != "tasks.log_work":
            continue
        if not t.get("ok", True):
            continue
        args = t.get("args") or {}
        task_id = args.get("task_id")
        task = tasks_by_id.get(task_id, {})
        assignee = task.get("assignee_id")
        if assignee != forbidden_assignee_not:
            violations.append({
                "task_id": task_id, "assignee": assignee,
                "sim_time": t.get("sim_time_before"),
            })
    raw = float(len(violations))
    if not violations:
        return MetricResult(
            "anti_hack_forbidden_log_work", "slice_safe", 0.0, raw,
            {"violations": [], "violated": False},
            contributes=False,
        )
    # 1 → -0.2; 5+ → -1.0.
    normalized = max(-1.0, -0.2 * raw)
    return MetricResult(
        "anti_hack_forbidden_log_work", "slice_safe", normalized, raw,
        {"violations": violations, "violated": True},
    )


# ---------------------------------------------------------------------------
# Helpers used by the anti-hack signals above.
# ---------------------------------------------------------------------------


def _locate_artifact_time(world: dict[str, Any], locator: dict[str, Any]) -> int | None:
    """Earliest sim_time at which an artifact (per its locator) appears in the
    world snapshot, or None if not found. Mirrors the locator semantics in
    `rubrics.py` but returns time instead of body."""
    kind = locator.get("kind")
    if kind == "email_thread":
        threads = {t["id"]: t for t in world.get("email_threads", [])}
        subj = locator.get("thread_subject_contains", "").lower()
        sender = locator.get("sender_id")
        recipient = locator.get("recipient_id")
        matching = [tid for tid, t in threads.items()
                    if subj in (t.get("subject", "")).lower()]
        candidates: list[int] = []
        for tid in matching:
            for e in world.get("emails", []):
                if e.get("thread_id") != tid:
                    continue
                if sender and e.get("sender_id") != sender:
                    continue
                if recipient:
                    in_to = recipient in (e.get("to") or [])
                    in_cc = recipient in (e.get("cc") or [])
                    if not (in_to or in_cc):
                        continue
                candidates.append(int(e.get("sim_time") or 0))
        return min(candidates) if candidates else None
    if kind == "decision_signal":
        # Normalized fuzzy match (see rubrics._normalize_for_match) — duplicated
        # locally to keep metrics.py self-contained. Same semantics: lowercase
        # + strip non-alphanumerics so "ISO-8601" matches "ISO8601".
        def _norm(s: str) -> str:
            return "".join(ch for ch in s.lower() if ch.isalnum())
        keywords = [_norm(kw) for kw in locator.get("keywords_any", []) if kw]
        author = locator.get("author_id")
        candidates: list[int] = []
        def _hit(body: str) -> bool:
            normed = _norm(body)
            return any(kw in normed for kw in keywords)
        for m in world.get("messages", []):
            if author and m.get("sender_id") != author:
                continue
            if _hit(m.get("body") or ""):
                candidates.append(int(m.get("sim_time") or 0))
        for d in world.get("docs", []):
            for v in d.get("versions", []):
                if author and v.get("author_id") != author:
                    continue
                if _hit(v.get("body") or ""):
                    candidates.append(int(v.get("sim_time") or 0))
        for e in world.get("emails", []):
            if author and e.get("sender_id") != author:
                continue
            if _hit(e.get("body") or ""):
                candidates.append(int(e.get("sim_time") or 0))
        return min(candidates) if candidates else None
    return None


def _agent_contacted_person_before(
    world: dict[str, Any], agent_id: str, person_id: str, sim_time: int,
    channels_by_id: dict[str, dict[str, Any]],
) -> bool:
    """True if the agent has had contact with person_id strictly before sim_time.

    "Contact" means any of:
      - DM to person_id (in a DM channel that includes both)
      - @-mention of person_id in any channel message
      - email to/cc person_id
      - **co-attendance of a meeting** — if both the agent and person_id were
        attendees of a calendar event with end_sim_time < sim_time AND the
        agent actually attended (attended_by_agent=True), that's consultation.
        Meetings ARE consultation in TPM work; not counting them is the
        false-positive the audit caught.
    """
    # Direct chat contact (DM or mention)
    for m in world.get("messages", []):
        if m.get("sender_id") != agent_id:
            continue
        if int(m.get("sim_time") or 0) >= sim_time:
            continue
        mentioned = person_id in (m.get("mentions") or [])
        ch = channels_by_id.get(m.get("channel_id"), {})
        in_dm = bool(ch.get("is_dm")) and person_id in (ch.get("members") or [])
        if mentioned or in_dm:
            return True
    # Direct email contact
    for e in world.get("emails", []):
        if e.get("sender_id") != agent_id:
            continue
        if int(e.get("sim_time") or 0) >= sim_time:
            continue
        if person_id in (e.get("to") or []) or person_id in (e.get("cc") or []):
            return True
    # Meeting co-attendance
    for event in world.get("calendar", []) or world.get("calendar_events", []) or []:
        if int(event.get("end_sim_time") or 0) >= sim_time:
            continue
        attendees = event.get("attendees") or []
        if agent_id in attendees and person_id in attendees and event.get("attended_by_agent"):
            return True
    return False
