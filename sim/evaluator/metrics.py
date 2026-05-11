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


def turns_per_sim_hour(run_dir: Path) -> MetricResult:
    turns = _load_turns(run_dir)
    if not turns:
        return MetricResult("turns_per_sim_hour", "slice_safe", 0.0, 0.0, {"reason": "no_turns"})
    last = turns[-1]
    sim_time = last.get("sim_time_after", 0)
    if sim_time <= 0:
        return MetricResult("turns_per_sim_hour", "slice_safe", 0.0, float(len(turns)), {})
    raw = len(turns) * 60 / sim_time
    # 5-15 turns/sim-hour ideal → +1. 30+ → -1. <2 → -0.3 (too quiet).
    if 5 <= raw <= 15:
        normalized = 1.0
    elif raw < 2:
        normalized = -0.3
    elif raw <= 30:
        normalized = max(-1.0, 1.0 - (raw - 15) / 15)
    else:
        normalized = -1.0
    return MetricResult("turns_per_sim_hour", "slice_safe", normalized, raw,
                        {"turns": len(turns), "sim_time": sim_time})


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
            for m in messages:
                if m.get("sender_id") != world.get("agent_id"):
                    continue
                body = (m.get("body") or "").lower()
                if recipient and recipient not in (m.get("mentions") or []):
                    # Could also be a DM to recipient — check channel
                    # simplified: keyword + mention or DM
                    pass
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
    world = _load_world(run_dir)
    agent_id = world.get("agent_id")
    sent = sum(1 for m in world.get("messages", []) if m.get("sender_id") == agent_id)
    max_allowed = 120
    for ah in eval_truth.get("anti_hack", []) or []:
        sig = ah.get("signal", {})
        if sig.get("kind") in ("max_messages_total", "max_messages"):
            max_allowed = int(sig.get("max", max_allowed))
            break
    raw = sent / max_allowed if max_allowed else 0.0
    if sent <= max_allowed:
        normalized = 1.0 - (sent / max_allowed) * 0.4  # gentle taper to 0.6
    else:
        # Over the cap → linearly down to -1 at 2x the cap.
        over = sent - max_allowed
        normalized = max(-1.0, 0.6 - (over / max_allowed) * 1.6)
    return MetricResult("anti_hack_max_messages", "slice_safe", normalized, sent,
                        {"sent": sent, "limit": max_allowed})
