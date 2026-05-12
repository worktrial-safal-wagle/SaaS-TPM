"""Programmatic metrics for the final evaluator.

Each metric is a pure function over the run directory on disk. Output is in
`[-1, +1]` so it composes cleanly with the judge axes.

Tier:
  - "slice_safe": always runs, even when the run crashed mid-way.
  - "requires_full_run": only runs when `sim_time >= end_sim_time`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    # 0 errors → +1; ≥20% errors → -1; linear in between
    normalized = max(-1.0, min(1.0, 1 - 5 * raw))
    return MetricResult("error_rate", "slice_safe", normalized, raw,
                        {"errors": errors, "total": len(turns)})


# turns_per_sim_hour was dropped — see commit "drop turns_per_sim_hour metric".
# It penalised efficient agents that compressed the week into long-duration
# actions (meetings, log_work, wait skips) because turn density mechanically
# falls when each turn covers many sim-minutes. Its intended job (catching
# thrashing) is already covered by tight_loop_rate, repeat_read_rate, and the
# anti_hack volume signals. Keep the slot in the file as a placeholder comment
# so anyone grep-ing the history knows where it used to live.


TIGHT_LOOP_THRESHOLD = 3
# Tools where identical consecutive calls reflect chunked work, not stuck
# behaviour. `tasks.log_work` is the load-bearing case: an agent logging 5
# hours of work on a task does five identical-args log_work calls, but
# that's 5 hours of real productive output, not 5 turns of spinning.
# Excluded tools still BREAK other streaks (the agent did something else)
# — they just don't form streaks themselves.
TIGHT_LOOP_EXCLUDED_TOOLS = {"tasks.log_work"}


def tight_loop_rate(run_dir: Path) -> MetricResult:
    """Flag agents that get stuck calling the same tool with the same args.

    A "tight loop" is 3+ consecutive turns with identical (tool, args) —
    regardless of success/failure. Two failure modes this catches:

      - Failed loops: the agent keeps retrying a broken call (e.g., 136
        consecutive failed meetings.attend on an already-ended meeting).
      - Successful spam: the agent keeps emitting the same write call
        (e.g., 100 consecutive chat.send with identical body).

    Whether each call succeeds doesn't matter — the pattern is "agent isn't
    varying its behaviour, isn't making progress." Failed loops already cost
    sim-time so they self-terminate; this metric ensures they also show up
    on the scorecard rather than being silently absorbed by the (saturated)
    error_rate metric. Successful spam is already partly caught by
    anti_hack_max_messages but only for the specific case of chat volume.

    Total loop-turns / total turns. 0% → +1; ≥20% → -1.
    """
    turns = _load_turns(run_dir)
    if not turns:
        return MetricResult("tight_loop_rate", "slice_safe", 1.0, 0.0,
                            {"reason": "no_turns"})

    def args_sig(args: Any) -> str:
        try:
            return json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            return str(args)

    loop_turns = 0
    longest_loop = 0
    longest_loop_key: tuple[str, str] | None = None
    current_key: tuple[str, str] | None = None
    current_count = 0

    def _close_streak() -> None:
        nonlocal loop_turns, longest_loop, longest_loop_key
        if current_count >= TIGHT_LOOP_THRESHOLD:
            loop_turns += current_count
            if current_count > longest_loop:
                longest_loop = current_count
                longest_loop_key = current_key

    for t in turns:
        tool = t.get("tool", "")
        if tool in TIGHT_LOOP_EXCLUDED_TOOLS:
            # Chunked-work tool — close any existing streak (the agent did
            # something else) but don't form a new streak from these calls.
            _close_streak()
            current_key = None
            current_count = 0
            continue
        key = (tool, args_sig(t.get("args")))
        if key == current_key:
            current_count += 1
        else:
            _close_streak()
            current_key = key
            current_count = 1
    if current_count >= TIGHT_LOOP_THRESHOLD:
        loop_turns += current_count
        if current_count > longest_loop:
            longest_loop = current_count
            longest_loop_key = current_key

    raw = loop_turns / len(turns)
    # 0% loop turns → +1.0; 20% → 0.0; 40%+ → -1.0.
    normalized = max(-1.0, min(1.0, 1.0 - raw / 0.2))
    detail: dict[str, Any] = {
        "loop_turns": loop_turns,
        "total_turns": len(turns),
        "threshold": TIGHT_LOOP_THRESHOLD,
    }
    if longest_loop_key is not None:
        detail["longest_loop"] = {
            "tool": longest_loop_key[0],
            "args_sig": longest_loop_key[1][:160],
            "length": longest_loop,
        }
    return MetricResult("tight_loop_rate", "slice_safe", normalized, raw, detail)


def repeat_read_rate(run_dir: Path) -> MetricResult:
    """Penalize reading the same artifact ≥3× without acting between reads."""
    turns = _load_turns(run_dir)
    read_keys: dict[str, int] = defaultdict(int)
    bad = 0
    total_reads = 0
    READ_TOOLS = {"chat.read", "email.read", "docs.read", "tasks.get", "calendar.get",
                  "directory.get", "meetings.get_transcript"}
    for t in turns:
        if t.get("tool") not in READ_TOOLS:
            continue
        total_reads += 1
        args = t.get("args") or {}
        key_id = (
            args.get("doc_id") or args.get("thread_id") or args.get("event_id")
            or args.get("task_id") or args.get("channel_id") or args.get("person_id")
            or ""
        )
        if not key_id:
            continue
        compound = f"{t['tool']}:{key_id}"
        read_keys[compound] += 1
        if read_keys[compound] >= 3:
            bad += 1
    raw = bad / total_reads if total_reads else 0.0
    normalized = max(-1.0, min(1.0, 1 - 4 * raw))
    return MetricResult("repeat_read_rate", "slice_safe", normalized, raw,
                        {"bad_reads": bad, "total_reads": total_reads})


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


# ---------------------------------------------------------------------------
# Anti-hack signals (folded into slice_safe alongside metrics for the same scale)
# ---------------------------------------------------------------------------


def anti_hack_max_messages(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    """Tripwire: contributes a negative score ONLY when the agent's message
    volume exceeds the eval.yaml cap. Below the cap is the baseline
    expectation — the agent doesn't earn a positive score for not spamming."""
    world = _load_world(run_dir)
    agent_id = world.get("agent_id")
    sent = sum(1 for m in world.get("messages", []) if m.get("sender_id") == agent_id)
    max_allowed: int | None = None
    for ah in eval_truth.get("anti_hack", []) or []:
        sig = ah.get("signal", {})
        if sig.get("kind") in ("max_messages_total", "max_messages"):
            max_allowed = int(sig.get("max", 120))
            break
    if max_allowed is None:
        return MetricResult("anti_hack_max_messages", "slice_safe",
                            0.0, sent, {"reason": "signal_not_declared", "sent": sent},
                            contributes=False)
    if sent <= max_allowed:
        return MetricResult("anti_hack_max_messages", "slice_safe",
                            0.0, sent,
                            {"sent": sent, "limit": max_allowed, "violated": False},
                            contributes=False)
    # Over the cap → linearly down to -1 at 2x the cap.
    over = sent - max_allowed
    normalized = max(-1.0, -(over / max_allowed))
    return MetricResult("anti_hack_max_messages", "slice_safe", normalized, sent,
                        {"sent": sent, "limit": max_allowed, "violated": True})


def anti_hack_per_channel_volume(run_dir: Path, eval_truth: dict[str, Any]) -> MetricResult:
    """Penalize sending too many messages to one channel in one sim-day."""
    world = _load_world(run_dir)
    agent_id = world.get("agent_id")
    max_per_day = None
    for ah in eval_truth.get("anti_hack", []) or []:
        sig = ah.get("signal", {})
        if sig.get("kind") == "max_messages_per_channel_per_day":
            max_per_day = int(sig.get("max", 25))
            break
    if max_per_day is None:
        return MetricResult("anti_hack_per_channel_volume", "slice_safe",
                            1.0, 0.0, {"reason": "signal_not_declared"},
                            contributes=False)
    buckets: dict[tuple[str, int], int] = defaultdict(int)
    for m in world.get("messages", []):
        if m.get("sender_id") != agent_id:
            continue
        day = int(m.get("sim_time", 0)) // (24 * 60)
        buckets[(m.get("channel_id") or "", day)] += 1
    violations = [
        {"channel_id": ch, "day": day, "count": cnt}
        for (ch, day), cnt in buckets.items() if cnt > max_per_day
    ]
    raw = float(len(violations))
    if not violations:
        # Tripwire didn't fire — not a positive signal, just baseline.
        return MetricResult(
            "anti_hack_per_channel_volume", "slice_safe", 0.0, raw,
            {"violations": [], "limit": max_per_day, "violated": False},
            contributes=False,
        )
    # 1 violation → -0.2; 5+ → -1.0.
    normalized = max(-1.0, -0.2 * raw)
    return MetricResult(
        "anti_hack_per_channel_volume", "slice_safe", normalized, raw,
        {"violations": violations, "limit": max_per_day, "violated": True},
    )


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
