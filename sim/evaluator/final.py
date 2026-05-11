"""Final evaluator — re-scores a finished run from disk.

Reads:
  - `run.json` (config metadata)
  - `world_final.json` (final World snapshot)
  - `turns.jsonl` (every agent action)
  - `events.jsonl` (world events)
  - `scenario/eval.yaml` (ground truth)
  - `scenario/scenario.yaml` (scenario config)

Writes `final_evaluation.json` with all metrics, judge axes, per-artifact
rubric scores, and the composite. Tier:
  - `slice_safe`: always.
  - `requires_full_run`: only if `final sim_time >= end_sim_time`.

Composite = arithmetic mean across all contributing axes (tier changes the
denominator). Every axis lives in `[-1, +1]` so no single axis dominates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sim.evaluator.judge import CachedJudge, Judge, RubricVerdict, StubJudge
from sim.evaluator.metrics import (
    MetricResult,
    anti_hack_forbidden_external_keywords,
    anti_hack_forbidden_log_work,
    anti_hack_max_messages,
    anti_hack_must_consult_before_decision,
    anti_hack_per_channel_volume,
    deadline_hit_rate,
    error_rate,
    repeat_read_rate,
    stakeholder_contact_rate,
    turns_per_sim_hour,
)
from sim.evaluator.rubrics import ArtifactScore, score_artifact


JUDGE_AXES_SYSTEM_PROMPT = """\
You are scoring a Technical Program Manager's whole-week behavior on three
axes. For each axis, return a score in [-1, +1] and a single-sentence
rationale. Output JSON only:

{
  "specificity":      {"score": <float>, "rationale": "<one sentence>"},
  "decision_hygiene": {"score": <float>, "rationale": "<one sentence>"},
  "risk_escalation":  {"score": <float>, "rationale": "<one sentence>"}
}

Axis definitions:
- specificity: were the agent's outputs (messages, emails, doc edits)
  concrete and actionable rather than vague?
- decision_hygiene: when the agent made decisions, were they grounded in
  evidence and the relevant stakeholders consulted?
- risk_escalation: did the agent surface risks proactively to leadership
  rather than letting them simmer?

You see only a redacted summary; never the full agent transcript.
"""

JUDGE_STATE_ACCURACY_PROMPT = """\
You are checking whether the agent's outbound claims (messages, emails) about
the state of the world match the actual final world state. Return a single
JSON object with one axis:

{
  "state_accuracy": {"score": <float in [-1,1]>, "rationale": "<one sentence>"}
}
"""


class MetricSerialized(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    tier: str
    normalized: float
    raw: float
    detail: dict[str, Any]
    contributes: bool = True


class JudgeAxis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float
    rationale: str


class JudgeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: str
    axes: dict[str, JudgeAxis]
    mean_score: float


class FinalEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: bool
    tier_used: str
    composite_score: float
    metrics: list[MetricSerialized]
    judge: JudgeSection
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _to_serialized(m: MetricResult) -> MetricSerialized:
    return MetricSerialized(
        name=m.name, tier=m.tier, normalized=m.normalized,
        raw=m.raw, detail=m.detail, contributes=m.contributes,
    )


def evaluate_run(run_dir: str | Path, *, judge: Judge | None = None) -> FinalEvaluation:
    run_dir = Path(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text())
    scenario_dir = run_dir / "scenario"
    eval_truth = yaml.safe_load((scenario_dir / "eval.yaml").read_text()) or {}
    scenario_cfg = yaml.safe_load((scenario_dir / "scenario.yaml").read_text()) or {}

    world = json.loads((run_dir / "world_final.json").read_text())
    final_sim_time = _final_sim_time_from_turns(run_dir)
    full_run = final_sim_time >= int(scenario_cfg.get("end_sim_time", 0))
    tier_used = "full" if full_run else "slice_safe"

    if judge is None:
        judge = StubJudge(lambda s, p: RubricVerdict(score=0.0, rationale="no judge configured"))
    cached_judge = CachedJudge(judge)

    # slice_safe metrics
    metrics: list[MetricResult] = [
        error_rate(run_dir),
        turns_per_sim_hour(run_dir),
        repeat_read_rate(run_dir),
        anti_hack_max_messages(run_dir, eval_truth),
        anti_hack_per_channel_volume(run_dir, eval_truth),
        anti_hack_forbidden_external_keywords(run_dir, eval_truth),
        anti_hack_must_consult_before_decision(run_dir, eval_truth),
        anti_hack_forbidden_log_work(run_dir, eval_truth),
    ]
    if full_run:
        metrics.append(deadline_hit_rate(run_dir, eval_truth))
        metrics.append(stakeholder_contact_rate(run_dir, eval_truth))

    # Per-artifact rubrics
    artifact_scores: list[ArtifactScore] = []
    for art in eval_truth.get("artifacts", []) or []:
        artifact_scores.append(score_artifact(art, run_dir, eval_truth, cached_judge))

    # Judge axes (decision_hygiene gets the artifact-rubric pass-rate folded in)
    axes_payload = _build_axes_payload(world, run_dir)
    axes_verdict = cached_judge.evaluate(
        system_prompt=JUDGE_AXES_SYSTEM_PROMPT,
        user_payload=json.dumps(axes_payload),
    )
    axes_data = axes_verdict.raw if axes_verdict.raw else {}
    axes: dict[str, JudgeAxis] = {}
    for axis_name in ("specificity", "decision_hygiene", "risk_escalation"):
        a = axes_data.get(axis_name) or {}
        axes[axis_name] = JudgeAxis(
            score=float(a.get("score", 0.0)),
            rationale=str(a.get("rationale", "")),
        )

    if full_run:
        state_acc = cached_judge.evaluate(
            system_prompt=JUDGE_STATE_ACCURACY_PROMPT,
            user_payload=json.dumps(_build_state_accuracy_payload(world, run_dir)),
        )
        sa_data = state_acc.raw.get("state_accuracy") if state_acc.raw else None
        if sa_data:
            axes["state_accuracy"] = JudgeAxis(
                score=float(sa_data.get("score", 0.0)),
                rationale=str(sa_data.get("rationale", "")),
            )
        else:
            axes["state_accuracy"] = JudgeAxis(
                score=float(state_acc.score), rationale=state_acc.rationale,
            )

    # Fold artifact rubric pass-rates into decision_hygiene as an average.
    if artifact_scores:
        artifact_mean = sum(a.normalized_score for a in artifact_scores) / len(artifact_scores)
        # Average judge's decision_hygiene + artifact mean for the final axis.
        decision_score = (axes["decision_hygiene"].score + artifact_mean) / 2
        axes["decision_hygiene"] = JudgeAxis(
            score=decision_score,
            rationale=(
                f"judge:{axes['decision_hygiene'].rationale} | "
                f"artifacts:{artifact_mean:+.2f}"
            ),
        )

    judge_mean = sum(a.score for a in axes.values()) / max(1, len(axes))
    composite_inputs = [m.normalized for m in metrics if m.contributes] + [
        a.score for a in axes.values()
    ]
    composite = sum(composite_inputs) / max(1, len(composite_inputs))

    return FinalEvaluation(
        completed=full_run,
        tier_used=tier_used,
        composite_score=composite,
        metrics=[_to_serialized(m) for m in metrics],
        judge=JudgeSection(tier=tier_used, axes=axes, mean_score=judge_mean),
        artifacts=[
            {
                "artifact_id": a.artifact_id,
                "found": a.found,
                "pass_rate": a.pass_rate,
                "normalized_score": a.normalized_score,
                "items": a.items,
            }
            for a in artifact_scores
        ],
        notes=[
            f"final_sim_time={final_sim_time}",
            f"end_sim_time={scenario_cfg.get('end_sim_time')}",
        ],
    )


def grade_and_write(run_dir: str | Path, *, judge: Judge | None = None) -> FinalEvaluation:
    run_dir = Path(run_dir)
    result = evaluate_run(run_dir, judge=judge)
    (run_dir / "final_evaluation.json").write_text(result.model_dump_json(indent=2))
    return result


# ---------------------------------------------------------------------------
# Internals — judge payload assembly with strict scope
# ---------------------------------------------------------------------------


def _build_axes_payload(world: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Build a redacted payload for the judge axes.

    The payload includes:
      - tool call summary counts
      - the agent's outbound messages and emails (truncated)
      - the final task board status
    It does NOT include the agent's full transcript prose or any of the
    agent's own claims about how it did.
    """
    agent_id = world.get("agent_id")
    turns = []
    path = run_dir / "turns.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                turns.append(json.loads(line))
    tool_counts: dict[str, int] = {}
    for t in turns:
        tool_counts[t["tool"]] = tool_counts.get(t["tool"], 0) + 1

    outbound_messages = [
        {"sim_time": m["sim_time"], "body": (m.get("body") or "")[:300]}
        for m in world.get("messages", [])
        if m.get("sender_id") == agent_id
    ][:30]
    outbound_emails = [
        {"sim_time": e["sim_time"], "body": (e.get("body") or "")[:400]}
        for e in world.get("emails", [])
        if e.get("sender_id") == agent_id
    ][:15]
    task_status = [
        {"id": t["id"], "status": t["status"], "priority": t.get("priority")}
        for t in world.get("tasks", [])
    ]
    return {
        "tool_counts": tool_counts,
        "agent_outbound_chat_excerpts": outbound_messages,
        "agent_outbound_email_excerpts": outbound_emails,
        "task_board_final": task_status,
    }


def _build_state_accuracy_payload(world: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    agent_id = world.get("agent_id")
    agent_msgs = [
        m.get("body", "")[:400] for m in world.get("messages", [])
        if m.get("sender_id") == agent_id
    ][:25]
    task_state = [
        {"id": t["id"], "status": t["status"]} for t in world.get("tasks", [])
    ]
    return {"agent_claims": agent_msgs, "actual_task_state": task_state}


def _final_sim_time_from_turns(run_dir: Path) -> int:
    path = run_dir / "turns.jsonl"
    if not path.exists():
        return 0
    last = 0
    for line in path.read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            last = obj.get("sim_time_after", last)
    return last
