"""Final evaluator — re-scores a finished run from disk.

Reads:
  - `run.json` (config metadata)
  - `world_final.json` (final World snapshot)
  - `turns.jsonl` (every agent action)
  - `events.jsonl` (world events)
  - `scenario/eval.yaml` (ground truth)
  - `scenario/scenario.yaml` (scenario config)

Writes `final_evaluation.json` with all metrics, judge axes, per-artifact
rubric scores, the per-cluster summary, and the composite. Tier:
  - `slice_safe`: always.
  - `requires_full_run`: only if `final sim_time >= end_sim_time`.

Composite = arithmetic mean over the four **skill clusters** that have at
least one contributing member (Eval Phase 5). Each cluster's own score is
the arithmetic mean of its contributing members (metrics + judge axes; the
`decision_quality` cluster also folds artifact rubric scores in as a single
synthetic `artifacts_mean` member). Cluster weighting prevents the larger
buckets (e.g., `decision_quality` with 7+ members) from dominating the
smaller ones (e.g., `outcomes_achieved` with 2). Every member lives in
`[-1, +1]`; the composite inherits that range.
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
    anti_hack_must_consult_before_decision,
    appropriate_abandonments_rate,
    bad_timing_starts,
    deadline_hit_rate,
    error_rate,
    follow_up_rate,
    hidden_fact_discovery_rate,
    idle_judgment_score,
    opportunity_cost_score,
    prioritization_latency,
    stakeholder_contact_rate,
)
from sim.evaluator.rubrics import ArtifactScore, score_artifact


# ---------------------------------------------------------------------------
# Cluster definitions (Eval Phase 5)
# ---------------------------------------------------------------------------
#
# The scorecard groups every contributing signal into one of four skill
# clusters. The composite is the arithmetic mean of the cluster scores (each
# cluster equally weighted) — a larger bucket no longer dominates by virtue
# of having more members. Per-cluster, the score is the arithmetic mean of
# its contributing members (failed / not_applicable / contributes=False are
# excluded). The `decision_quality` cluster folds the artifact rubric scores
# in as a single synthetic `artifacts_mean` member so artifacts collapse to
# one cluster-level vote regardless of how many were declared.
#
# Mapping is by **member name** (the string name on `MetricResult` /
# `JudgeAxis`). If a metric name appears in the run output but is not listed
# in any cluster, that's a configuration bug — it's logged to
# `FinalEvaluation.errors[]` but does not crash the grader.

CLUSTERS: dict[str, dict[str, Any]] = {
    "outcomes_achieved": {
        "metrics": ["deadline_hit_rate", "hidden_fact_discovery_rate"],
        "axes": [],
        "artifacts": False,
    },
    "decision_quality": {
        "metrics": [
            "anti_hack_must_consult_before_decision",
            "anti_hack_forbidden_external_keywords",
            "anti_hack_forbidden_log_work",
        ],
        "axes": [
            "decision_hygiene",
            "specificity",
            "risk_escalation",
            "state_accuracy",
        ],
        "artifacts": True,   # per-artifact rubric mean folds in as one member
    },
    "coordination": {
        "metrics": ["stakeholder_contact_rate", "follow_up_rate"],
        "axes": [],
        "artifacts": False,
    },
    "time_management": {
        "metrics": [
            "error_rate",
            "prioritization_latency",
            "opportunity_cost_score",
            "appropriate_abandonments_rate",
            "bad_timing_starts",
            "idle_judgment_score",
        ],
        "axes": [
            "pre_emption_judgment",
            "tick_continuation_judgment",
            "context_recall_score",
        ],
        "artifacts": False,
    },
}


JUDGE_AXES_SYSTEM_PROMPT = """\
You are scoring a Technical Program Manager's whole-week behavior on three
axes. The user payload is a redacted JSON object containing what the agent
SAW (inbound chats and emails — DMs to the agent, mentions of the agent,
and emails to/cc the agent) and what the agent DID (outbound chats,
outbound emails, tool-call counts, final task board). You do NOT see the
agent's internal briefing, reasoning, or self-narration.

Output JSON ONLY. The first character of your response must be `{`. Do
not add prose, markdown fences, or commentary. Return exactly this shape
with ALL THREE axis keys present. If you cannot score an axis confidently,
return score 0.0 and explain in the rationale:

{
  "specificity":      {"score": <float in [-1, 1]>, "rationale": "<one short sentence>"},
  "decision_hygiene": {"score": <float in [-1, 1]>, "rationale": "<one short sentence>"},
  "risk_escalation":  {"score": <float in [-1, 1]>, "rationale": "<one short sentence>"}
}

Axis definitions:
- specificity: were the agent's outbound messages, emails, and doc edits
  concrete and actionable rather than vague?
- decision_hygiene: COMPARE inbound to outbound. Did the agent respond to
  what was actually asked? Before committing to decisions, did they contact
  the people who surfaced the relevant information in the inbound?
- risk_escalation: did the agent surface risks proactively to leadership
  (look for outbound that names risks raised in the inbound) rather than
  letting them simmer?
"""

JUDGE_STATE_ACCURACY_PROMPT = """\
You are checking whether the agent's outbound claims (messages, emails)
about the state of the world match the actual final world state shown
in the payload.

Output JSON ONLY. The first character of your response must be `{`. Do
not add prose, markdown fences, or commentary. Return exactly this shape:

{
  "state_accuracy": {"score": <float in [-1, 1]>, "rationale": "<one short sentence>"}
}
"""


JUDGE_PRE_EMPTION_PROMPT = """\
You are scoring whether the agent's decisions to PRE-EMPT (abandon) a
long-running action were justified given the context that prompted each
abandonment.

The payload is a redacted JSON object listing every `abandon.current` call
the agent made. For each event you see:
  - `sim_time`: when the abandon happened
  - `what_was_abandoned`: the in-flight tool call (name + cost_minutes)
  - `recent_inbound`: up to 5 inbound DMs/emails that arrived in the
     ~30 sim-min before the abandon (these are what may have triggered it)
  - `next_action_after_abandon`: the next tool call within 15 sim-min
     after the abandon (or null if none)

Output JSON ONLY. The first character of your response must be `{`. Do
not add prose, markdown fences, or commentary. Return exactly this shape:

{
  "pre_emption_judgment": {
    "score": <float in [-1, 1] — mean across events>,
    "rationale": "<one short sentence>",
    "per_event": [
      {"sim_time": <int>, "score": <float in [-1, 1]>,
       "rationale": "<one short sentence>"}
    ]
  }
}

Score per event:
  - +1: the recent_inbound contains a clearly urgent signal AND the
        next_action_after_abandon addresses it (a write/reply/DM).
  - 0:  recent_inbound has some plausible signal but the follow-up is
        unclear or weak.
  - -1: no plausible justification — abandoning was wasted motion.

Aggregate `score` is the mean across events.
"""


JUDGE_TICK_CONTINUATION_PROMPT = """\
You are scoring whether the agent's CHAINED decisions within a single tick
were productive (acting on what was just learned) or disguised polling
(stacking reads with no follow-up action).

The payload lists tick-continuation events. Each event is a sequence of
2+ tool calls the agent made back-to-back inside one tick. For each event
you see:
  - `tick_start_sim_time`: when the tick fired
  - `decisions_in_tick`: ordered list of tool calls (name + truncated args)
  - `had_write_or_action`: bool — did any non-read tool fire in this group?

Output JSON ONLY. The first character of your response must be `{`. Do
not add prose, markdown fences, or commentary. Return exactly this shape:

{
  "tick_continuation_judgment": {
    "score": <float in [-1, 1] — mean across events>,
    "rationale": "<one short sentence>",
    "per_event": [
      {"tick_start_sim_time": <int>, "score": <float in [-1, 1]>,
       "rationale": "<one short sentence>"}
    ]
  }
}

Score per event:
  - +1: the chain ends in (or contains) a write/action that's clearly a
        response to what was read earlier in the chain.
  - 0:  mixed — reads + write but the write looks unrelated to the reads.
  - -1: pure reads with no follow-up write — disguised polling.

Aggregate `score` is the mean across events.
"""


JUDGE_CONTEXT_RECALL_PROMPT = """\
You are scoring whether the agent's outbound claims about world state
(task statuses, doc states, deadlines, action items) referenced state
that was CURRENT at the time the claim was made — or STALE state that
had already changed.

The payload lists agent claims with verifiable state references. Each:
  - `claim_sim_time`: when the claim was made
  - `claim_text`: truncated outbound chat/email body
  - `verifiable_state_snapshot`: relevant entity state as of claim_sim_time
     and as of the agent's most-recent read of that entity

Output JSON ONLY. The first character of your response must be `{`. Do
not add prose, markdown fences, or commentary. Return exactly this shape:

{
  "context_recall_score": {
    "score": <float in [-1, 1] — mean across claims>,
    "rationale": "<one short sentence>",
    "per_claim": [
      {"claim_sim_time": <int>, "score": <float in [-1, 1]>,
       "rationale": "<one short sentence>"}
    ]
  }
}

Score per claim:
  - +1: the claim aligns with the entity state at claim_sim_time.
  - 0:  ambiguous — claim is vague or partially aligns.
  - -1: the claim is stale (matches the agent's last-read snapshot but
        not the current state — delta-briefing missed the change).

Aggregate `score` is the mean across claims.
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
    """A single judge-graded axis.

    `failed`: judge couldn't produce a verdict (API error or malformed JSON
    after retries). Excluded from the composite mean — reported for visibility
    but neither rewarded nor penalized.

    `not_applicable`: the run produced no signal for this axis (e.g. agent
    made zero abandonments, never chained continuations, made no verifiable
    state claims). Excluded from the composite mean for the same reason —
    "no signal" should not be conflated with a real 0.0 score. Mirrors the
    `contributes=False` flag on programmatic metrics.
    """

    model_config = ConfigDict(extra="forbid")
    score: float
    rationale: str
    failed: bool = False
    not_applicable: bool = False


class JudgeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: str
    axes: dict[str, JudgeAxis]
    mean_score: float


class Cluster(BaseModel):
    """One skill cluster — a group of metrics / axes / (optionally) artifacts.

    Members come from `CLUSTERS` (mapping by name). At grading time each
    member is checked for whether it contributes:
      - metrics: `MetricResult.contributes=True`
      - axes: `JudgeAxis.failed=False AND JudgeAxis.not_applicable=False`
      - artifacts (decision_quality only): non-failed artifacts roll into a
        single synthetic member named `artifacts_mean`.

    `score` is the arithmetic mean of contributing members' values
    (`[-1, +1]`-normalised). When every member is excluded, `score` is 0.0
    and `contributes_to_composite=False` — the cluster is skipped when the
    composite is computed.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    score: float
    included_members: list[str]
    excluded_members: list[str]
    member_count: int
    contributes_to_composite: bool


class FinalEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: bool
    tier_used: str
    composite_score: float
    metrics: list[MetricSerialized]
    judge: JudgeSection
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    # Eval Phase 5: per-skill-cluster summary. Keys are the four cluster names
    # in `CLUSTERS`. Raw `metrics[]` and `judge.axes` remain in the JSON for
    # detailed inspection; `clusters` is the top-level summary the composite
    # is computed from.
    clusters: dict[str, Cluster] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    # Judge calls that couldn't produce a verdict (API error after retries,
    # malformed JSON, or missing axis), plus any configuration warnings
    # (e.g. an orphan metric name not present in any CLUSTERS bucket). These
    # are reported here for visibility and excluded from the composite mean —
    # neither rewarded nor penalized.
    errors: list[str] = Field(default_factory=list)


def _to_serialized(m: MetricResult) -> MetricSerialized:
    return MetricSerialized(
        name=m.name, tier=m.tier, normalized=m.normalized,
        raw=m.raw, detail=m.detail, contributes=m.contributes,
    )


def evaluate_run(run_dir: str | Path, *, judge: Judge | None = None) -> FinalEvaluation:
    """Re-score a finished run from disk and return the scorecard.

    Composite formula (Eval Phase 5): the scorecard groups every contributing
    signal into one of four skill clusters (`outcomes_achieved`,
    `decision_quality`, `coordination`, `time_management`). Per cluster, the
    score is the **arithmetic mean of its contributing members** — every
    member equally weighted within a cluster. The top-level
    `composite_score` is the **arithmetic mean of the cluster scores** —
    every cluster equally weighted, regardless of how many members each
    contains. Clusters with zero contributing members are skipped.

    The `decision_quality` cluster also rolls per-artifact rubric scores into
    a single synthetic `artifacts_mean` member, so artifacts collapse to one
    cluster-level vote regardless of how many were declared.
    """
    run_dir = Path(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text())
    scenario_dir = run_dir / "scenario"
    eval_truth = yaml.safe_load((scenario_dir / "eval.yaml").read_text()) or {}
    scenario_cfg = yaml.safe_load((scenario_dir / "scenario.yaml").read_text()) or {}
    # Propagate tick_size_minutes to metrics so they can apply the tick-floor
    # (prioritization_latency) and other tick-aware logic. Default 15 matches
    # the scenario schema default.
    eval_truth["tick_size_minutes"] = int(scenario_cfg.get("tick_size_minutes", 15))

    world = json.loads((run_dir / "world_final.json").read_text())
    # Prefer run.json's `final_sim_time` (written by logger.finalize with the
    # scheduler's clock at termination). Fall back to scanning turns.jsonl
    # for older runs that pre-date that field. The two can disagree when the
    # driver advances sim_time post-last-turn (graceful idle-out to end).
    final_sim_time = int(run_meta.get("final_sim_time") or 0)
    if not final_sim_time:
        final_sim_time = _final_sim_time_from_turns(run_dir)
    full_run = final_sim_time >= int(scenario_cfg.get("end_sim_time", 0))
    tier_used = "full" if full_run else "slice_safe"

    if judge is None:
        judge = StubJudge(lambda s, p: RubricVerdict(score=0.0, rationale="no judge configured"))
    cached_judge = CachedJudge(judge)

    # slice_safe metrics
    metrics: list[MetricResult] = [
        error_rate(run_dir),
        prioritization_latency(run_dir, eval_truth),
        hidden_fact_discovery_rate(run_dir, eval_truth),
        follow_up_rate(run_dir, eval_truth),
        opportunity_cost_score(run_dir, eval_truth),
        appropriate_abandonments_rate(run_dir, eval_truth),
        bad_timing_starts(run_dir, eval_truth),
        idle_judgment_score(run_dir, eval_truth),
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

    errors: list[str] = []

    # Judge axes (decision_hygiene gets the artifact-rubric pass-rate folded in)
    axes_payload = _build_axes_payload(world, run_dir)
    axes_verdict = cached_judge.evaluate(
        system_prompt=JUDGE_AXES_SYSTEM_PROMPT,
        user_payload=json.dumps(axes_payload),
    )
    axes_data = axes_verdict.raw if axes_verdict.raw else {}
    axes: dict[str, JudgeAxis] = {}
    for axis_name in ("specificity", "decision_hygiene", "risk_escalation"):
        a = axes_data.get(axis_name)
        if axes_verdict.failed or a is None:
            axes[axis_name] = JudgeAxis(
                score=0.0,
                rationale=axes_verdict.rationale if axes_verdict.failed
                          else "axis_missing_from_judge_response",
                failed=True,
            )
            errors.append(
                f"axes.{axis_name}: " +
                ("judge_failed" if axes_verdict.failed else "axis_missing")
            )
        else:
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
        if state_acc.failed:
            axes["state_accuracy"] = JudgeAxis(
                score=0.0, rationale=state_acc.rationale, failed=True,
            )
            errors.append(f"axes.state_accuracy: judge_failed")
        elif sa_data:
            axes["state_accuracy"] = JudgeAxis(
                score=float(sa_data.get("score", 0.0)),
                rationale=str(sa_data.get("rationale", "")),
            )
        else:
            axes["state_accuracy"] = JudgeAxis(
                score=float(state_acc.score), rationale=state_acc.rationale,
            )

    # Surface artifact-judge failures as errors, exclude them from the
    # artifact mean (don't fabricate a half-pass score).
    for a in artifact_scores:
        if a.failed:
            errors.append(f"artifact.{a.artifact_id}: {a.failure_reason or 'judge_failed'}")
    scored_artifacts = [a for a in artifact_scores if not a.failed]

    # Fold artifact rubric pass-rates into decision_hygiene. If the axis judge
    # failed but the artifact judges succeeded, the artifacts are the only
    # signal we have for that axis — use them to recover the score rather than
    # dropping the axis entirely.
    if scored_artifacts:
        artifact_mean = sum(a.normalized_score for a in scored_artifacts) / len(scored_artifacts)
        if axes["decision_hygiene"].failed:
            axes["decision_hygiene"] = JudgeAxis(
                score=artifact_mean,
                rationale=f"axes_judge_failed; using artifact_mean={artifact_mean:+.2f}",
            )
        else:
            decision_score = (axes["decision_hygiene"].score + artifact_mean) / 2
            axes["decision_hygiene"] = JudgeAxis(
                score=decision_score,
                rationale=(
                    f"judge:{axes['decision_hygiene'].rationale} | "
                    f"artifacts:{artifact_mean:+.2f}"
                ),
            )

    # ------------------------------------------------------------------
    # Tick-based judge axes (Phase 4)
    # ------------------------------------------------------------------
    # Each axis follows the same shape:
    #   1) Build a payload (pure function on world / run_dir / eval_truth).
    #   2) If the payload has no signal → not_applicable=True, excluded
    #      from composite + mean.
    #   3) Else call the cached judge with the dedicated prompt. Failure /
    #      missing key → failed=True (also excluded).
    _add_axis_from_judge(
        axes, errors,
        axis_name="pre_emption_judgment",
        prompt=JUDGE_PRE_EMPTION_PROMPT,
        payload=_build_pre_emption_payload(world, run_dir, eval_truth),
        applicable_key="events",
        not_applicable_rationale="no abandonments in this run",
        cached_judge=cached_judge,
    )
    _add_axis_from_judge(
        axes, errors,
        axis_name="tick_continuation_judgment",
        prompt=JUDGE_TICK_CONTINUATION_PROMPT,
        payload=_build_tick_continuation_payload(world, run_dir, eval_truth),
        applicable_key="events",
        not_applicable_rationale="no tick continuations in this run",
        cached_judge=cached_judge,
    )
    _add_axis_from_judge(
        axes, errors,
        axis_name="context_recall_score",
        prompt=JUDGE_CONTEXT_RECALL_PROMPT,
        payload=_build_context_recall_payload(world, run_dir, eval_truth),
        applicable_key="claims",
        not_applicable_rationale="no verifiable state claims in this run",
        cached_judge=cached_judge,
    )

    def _axis_contributes(a: JudgeAxis) -> bool:
        return not a.failed and not a.not_applicable

    judge_mean = sum(a.score for a in axes.values() if _axis_contributes(a)) / max(
        1, sum(1 for a in axes.values() if _axis_contributes(a))
    )

    # ------------------------------------------------------------------
    # Eval Phase 5: build per-cluster summary + cluster-weighted composite.
    # ------------------------------------------------------------------
    clusters = _build_clusters(metrics, axes, scored_artifacts, errors)
    contributing_cluster_scores = [
        c.score for c in clusters.values() if c.contributes_to_composite
    ]
    composite = (
        sum(contributing_cluster_scores) / len(contributing_cluster_scores)
        if contributing_cluster_scores else 0.0
    )

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
                "failed": a.failed,
                "failure_reason": a.failure_reason,
            }
            for a in artifact_scores
        ],
        clusters=clusters,
        notes=[
            f"final_sim_time={final_sim_time}",
            f"end_sim_time={scenario_cfg.get('end_sim_time')}",
        ],
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Cluster builder (Eval Phase 5)
# ---------------------------------------------------------------------------


def _build_clusters(
    metrics: list[MetricResult],
    axes: dict[str, JudgeAxis],
    scored_artifacts: list[ArtifactScore],
    errors: list[str],
) -> dict[str, Cluster]:
    """Group metrics / axes / (optionally) artifacts into skill clusters.

    Per cluster: `score = mean(contributing members)`. Members that are
    present in the run but excluded (metric.contributes=False, axis.failed,
    axis.not_applicable, failed artifacts) land in `excluded_members` so the
    JSON shows them without folding them into the cluster mean.

    Artifacts fold into `decision_quality` only — and as a single synthetic
    member named `artifacts_mean` (mean of non-failed artifact normalized
    scores). This prevents a scenario that declares many artifacts from
    swamping the cluster.

    Names referenced by `CLUSTERS` that don't exist in this run are simply
    absent from both `included_members` and `excluded_members` (they
    represent metric/axis slots that didn't fire this scenario). Conversely,
    any metric name in the run output that isn't in *any* cluster's bucket
    list is an orphan: logged to `errors[]` and dropped from cluster scoring.
    """
    metrics_by_name = {m.name: m for m in metrics}

    # Compute the synthetic artifacts_mean once, shared by any cluster that
    # opts in via `artifacts=True` (only `decision_quality` does today).
    artifacts_mean_value: float | None = None
    if scored_artifacts:
        artifacts_mean_value = (
            sum(a.normalized_score for a in scored_artifacts) / len(scored_artifacts)
        )

    # Membership lookup: which cluster does each metric name belong to?
    # Used to detect orphans below.
    metric_owner: dict[str, str] = {}
    for cluster_name, spec in CLUSTERS.items():
        for metric_name in spec["metrics"]:
            metric_owner[metric_name] = cluster_name

    # Orphan check: any metric in the run that no cluster claims is a
    # configuration bug. We log it but don't crash — composite is still
    # computed from declared clusters.
    for m in metrics:
        if m.name not in metric_owner:
            errors.append(
                f"cluster_config: metric '{m.name}' is not assigned to any "
                f"cluster in CLUSTERS — it will be excluded from the composite"
            )

    clusters: dict[str, Cluster] = {}
    for cluster_name, spec in CLUSTERS.items():
        included: list[str] = []
        excluded: list[str] = []
        included_values: list[float] = []

        for metric_name in spec["metrics"]:
            m = metrics_by_name.get(metric_name)
            if m is None:
                # Metric wasn't computed this run (e.g., a full-run metric in
                # a slice_safe run). Not present in the cluster at all.
                continue
            if m.contributes:
                included.append(m.name)
                included_values.append(m.normalized)
            else:
                excluded.append(m.name)

        for axis_name in spec["axes"]:
            a = axes.get(axis_name)
            if a is None:
                continue
            if a.failed or a.not_applicable:
                excluded.append(axis_name)
            else:
                included.append(axis_name)
                included_values.append(a.score)

        if spec.get("artifacts"):
            if artifacts_mean_value is not None:
                included.append("artifacts_mean")
                included_values.append(artifacts_mean_value)
            # If artifacts were declared but every one failed, we'd ideally
            # surface "artifacts_mean" as excluded — but we only do so when
            # the eval payload declared artifacts AND scored_artifacts is
            # empty (all failed) or empty by design. The simpler rule:
            # nothing to add either way → no entry. Failed artifacts are
            # already surfaced individually in `FinalEvaluation.errors`.

        score = (
            sum(included_values) / len(included_values)
            if included_values else 0.0
        )
        clusters[cluster_name] = Cluster(
            name=cluster_name,
            score=score,
            included_members=included,
            excluded_members=excluded,
            member_count=len(included) + len(excluded),
            contributes_to_composite=bool(included),
        )
    return clusters


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

    The payload includes BOTH sides of the agent's surface:
      - tool call summary counts
      - INBOUND chats the agent could see (DMs to the agent, mentions of
        the agent in channels) and INBOUND emails (to/cc the agent)
      - OUTBOUND chats and emails the agent sent
      - final task board status

    It does NOT include the agent's briefing, reasoning prose, or any
    self-narration — that's the Phase 1d redaction invariant. Inbound is
    safe to expose: the agent itself had visibility on it.
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

    channels_by_id = {c["id"]: c for c in world.get("channels", [])}

    def _is_inbound_chat(m: dict[str, Any]) -> bool:
        if m.get("sender_id") == agent_id:
            return False
        if agent_id in (m.get("mentions") or []):
            return True
        ch = channels_by_id.get(m.get("channel_id"), {})
        return bool(ch.get("is_dm")) and agent_id in (ch.get("members") or [])

    inbound_messages = [
        {"sim_time": m["sim_time"], "sender": m.get("sender_id"),
         "body": (m.get("body") or "")[:200]}
        for m in world.get("messages", [])
        if _is_inbound_chat(m)
    ][:30]
    outbound_messages = [
        {"sim_time": m["sim_time"], "body": (m.get("body") or "")[:300]}
        for m in world.get("messages", [])
        if m.get("sender_id") == agent_id
    ][:30]
    inbound_emails = [
        {"sim_time": e["sim_time"], "sender": e.get("sender_id"),
         "body": (e.get("body") or "")[:300]}
        for e in world.get("emails", [])
        if e.get("sender_id") != agent_id and (
            agent_id in (e.get("to") or []) or agent_id in (e.get("cc") or [])
        )
    ][:15]
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
        "agent_inbound_chat_excerpts": inbound_messages,
        "agent_outbound_chat_excerpts": outbound_messages,
        "agent_inbound_email_excerpts": inbound_emails,
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


# ---------------------------------------------------------------------------
# Phase 4: tick-aware judge axis plumbing
# ---------------------------------------------------------------------------


def _add_axis_from_judge(
    axes: dict[str, JudgeAxis],
    errors: list[str],
    *,
    axis_name: str,
    prompt: str,
    payload: dict[str, Any],
    applicable_key: str,
    not_applicable_rationale: str,
    cached_judge: CachedJudge,
) -> None:
    """Call a judge for one axis and write the result into `axes`.

    If `payload[applicable_key]` is empty/missing, the axis is marked
    `not_applicable=True` and excluded from the composite mean (mirrors the
    `contributes=False` flag on programmatic metrics). The judge is NOT
    called in this case — saving a cache slot and an API roundtrip.
    """
    items = payload.get(applicable_key) or []
    if not items:
        axes[axis_name] = JudgeAxis(
            score=0.0,
            rationale=not_applicable_rationale,
            not_applicable=True,
        )
        return

    verdict = cached_judge.evaluate(
        system_prompt=prompt, user_payload=json.dumps(payload),
    )
    axis_data = verdict.raw.get(axis_name) if verdict.raw else None
    if verdict.failed:
        axes[axis_name] = JudgeAxis(
            score=0.0, rationale=verdict.rationale, failed=True,
        )
        errors.append(f"axes.{axis_name}: judge_failed")
        return
    if axis_data is None:
        axes[axis_name] = JudgeAxis(
            score=0.0,
            rationale="axis_missing_from_judge_response",
            failed=True,
        )
        errors.append(f"axes.{axis_name}: axis_missing")
        return
    axes[axis_name] = JudgeAxis(
        score=float(axis_data.get("score", 0.0)),
        rationale=str(axis_data.get("rationale", "")),
    )


# ---------------------------------------------------------------------------
# Payload builders for the three new axes (pure functions, test-friendly)
# ---------------------------------------------------------------------------


_READ_TOOLS = frozenset({
    "chat.read", "chat.list_channels", "chat.list_recent",
    "email.list", "email.read", "email.list_threads",
    "docs.list", "docs.read", "tasks.list", "tasks.get",
    "calendar.list", "directory.list",
    "presence.get", "presence.list",
})


def _load_turns_for_eval(run_dir: Path) -> list[dict[str, Any]]:
    """Local loader so payload builders stay pure (no external dep on metrics)."""
    path = run_dir / "turns.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _build_pre_emption_payload(
    world: dict[str, Any], run_dir: Path, eval_truth: dict[str, Any],
) -> dict[str, Any]:
    """Build a payload listing every abandon.current event with context.

    For each abandon:
      - sim_time
      - what_was_abandoned: previous turn's tool name + cost_minutes
      - recent_inbound: up to 5 inbound DMs/emails in the ~30 sim-min
        window before the abandon
      - next_action_after_abandon: the next non-read tool within 15
        sim-min of the abandon (or null)

    Redaction: no briefings, no reasoning prose. Inbound is OK to expose
    (the agent saw it). Tool names + truncated args only.
    """
    agent_id = world.get("agent_id")
    turns = _load_turns_for_eval(run_dir)
    channels_by_id = {c["id"]: c for c in world.get("channels", [])}

    def _is_inbound_chat(m: dict[str, Any]) -> bool:
        if m.get("sender_id") == agent_id:
            return False
        if agent_id in (m.get("mentions") or []):
            return True
        ch = channels_by_id.get(m.get("channel_id"), {})
        return bool(ch.get("is_dm")) and agent_id in (ch.get("members") or [])

    inbound_chats = sorted(
        ({"kind": "chat", "sim_time": int(m.get("sim_time") or 0),
          "sender": m.get("sender_id"),
          "body": (m.get("body") or "")[:200]}
         for m in world.get("messages", []) if _is_inbound_chat(m)),
        key=lambda x: x["sim_time"],
    )
    inbound_emails = sorted(
        ({"kind": "email", "sim_time": int(e.get("sim_time") or 0),
          "sender": e.get("sender_id"),
          "body": (e.get("body") or "")[:200]}
         for e in world.get("emails", [])
         if e.get("sender_id") != agent_id and (
             agent_id in (e.get("to") or []) or agent_id in (e.get("cc") or [])
         )),
        key=lambda x: x["sim_time"],
    )
    all_inbound = sorted(inbound_chats + inbound_emails, key=lambda x: x["sim_time"])

    events: list[dict[str, Any]] = []
    for i, t in enumerate(turns):
        if t.get("tool") != "abandon.current":
            continue
        sim_time = int(t.get("sim_time_before") or 0)

        # The "what was abandoned" — previous turn (the in-flight call).
        prev = turns[i - 1] if i > 0 else None
        what_was_abandoned = None
        if prev is not None:
            what_was_abandoned = {
                "tool": prev.get("tool"),
                "cost_minutes": prev.get("cost_minutes"),
                "sim_time_before": prev.get("sim_time_before"),
                "sim_time_after": prev.get("sim_time_after"),
            }

        # Recent inbound: up to 5 items in [sim_time-30, sim_time].
        recent_inbound = [
            x for x in all_inbound
            if sim_time - 30 <= x["sim_time"] <= sim_time
        ][-5:]

        # Next action after the abandon, looking only at non-read tools
        # within 15 sim-min of the abandon.
        next_action_after_abandon = None
        abandon_after = int(t.get("sim_time_after") or sim_time)
        for nxt in turns[i + 1:]:
            nxt_before = int(nxt.get("sim_time_before") or 0)
            if nxt_before > abandon_after + 15:
                break
            if nxt.get("tool") in _READ_TOOLS:
                continue
            args = nxt.get("args") or {}
            args_repr = json.dumps(args, default=str)[:200]
            next_action_after_abandon = {
                "tool": nxt.get("tool"),
                "args_excerpt": args_repr,
                "sim_time": nxt_before,
            }
            break

        events.append({
            "sim_time": sim_time,
            "what_was_abandoned": what_was_abandoned,
            "recent_inbound": recent_inbound,
            "next_action_after_abandon": next_action_after_abandon,
        })

    return {"events": events}


def _build_tick_continuation_payload(
    world: dict[str, Any], run_dir: Path, eval_truth: dict[str, Any],
) -> dict[str, Any]:
    """Group consecutive turns that occurred in the SAME tick.

    Detection heuristic (documented):

    The agent driver only invokes the next decision in the same tick when
    `ToolCall.continue_in_tick=True`. After a chained call, the registry
    advances `sim_time` by the tool's cost — so the NEXT turn's
    `sim_time_before` equals the PREVIOUS turn's `sim_time_after`, and no
    `actor_poll` event fires in between. This is the cleanest signal we
    have: `turns[i].sim_time_before == turns[i-1].sim_time_after`.

    Across tick boundaries the driver waits for the next `actor_poll`
    event (which fires at a multiple of `tick_size_minutes` after the
    previous tick's start). The wait advances `sim_time` to the poll's
    fire_at, so `turns[i].sim_time_before` is strictly greater than the
    previous `sim_time_after`.

    We yield a "tick continuation event" for any group of 2+ consecutive
    turns where every pair satisfies the equality above. Single-decision
    ticks aren't returned (the agent didn't continue — there's nothing to
    score).
    """
    turns = _load_turns_for_eval(run_dir)
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for idx, t in enumerate(turns):
        if not current:
            current = [t]
            continue
        prev = current[-1]
        if int(t.get("sim_time_before") or 0) == int(prev.get("sim_time_after") or 0):
            current.append(t)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [t]
    if len(current) >= 2:
        groups.append(current)

    events: list[dict[str, Any]] = []
    for group in groups:
        tick_start = int(group[0].get("sim_time_before") or 0)
        had_write_or_action = any(
            t.get("tool") not in _READ_TOOLS for t in group
        )
        decisions_in_tick = []
        for t in group:
            args = t.get("args") or {}
            args_repr = json.dumps(args, default=str)[:120]
            decisions_in_tick.append({
                "tool": t.get("tool"),
                "args_excerpt": args_repr,
                "sim_time_before": int(t.get("sim_time_before") or 0),
                "sim_time_after": int(t.get("sim_time_after") or 0),
            })
        events.append({
            "tick_start_sim_time": tick_start,
            "decisions_in_tick": decisions_in_tick,
            "had_write_or_action": had_write_or_action,
        })

    return {"events": events}


def _build_context_recall_payload(
    world: dict[str, Any], run_dir: Path, eval_truth: dict[str, Any],
) -> dict[str, Any]:
    """List agent outbound claims that reference task state, plus snapshots.

    For each agent outbound chat or email whose body mentions a task id
    found in `world.tasks`, we capture:
      - claim_sim_time: when the outbound was sent
      - claim_text: truncated body
      - verifiable_state_snapshot:
        - task_id: the referenced task
        - state_at_claim: world status of that task as-of claim_sim_time
          (i.e. the most recent task_status_changed in events.jsonl with
          `sim_time <= claim_sim_time`)
        - state_at_last_read: world status of the task as-of the agent's
          last read of the task (last `tasks.list` or `tasks.get` turn
          before the claim that referenced this task)
        - final_state: the task's status in the final world snapshot

    Redaction: agent outbound + final world state only. No briefings, no
    reasoning prose. The "last read" comes from turns.jsonl which is
    already a record of the agent's own actions — fair game.
    """
    agent_id = world.get("agent_id")
    tasks_by_id = {t.get("id"): t for t in world.get("tasks", []) if t.get("id")}
    turns = _load_turns_for_eval(run_dir)
    events_path = run_dir / "events.jsonl"
    task_status_history: list[tuple[int, str, str]] = []  # (sim_time, task_id, status)
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("kind") == "task_status_changed":
                p = obj.get("payload") or {}
                tid = p.get("task_id") or p.get("id")
                st = p.get("status") or p.get("new_status")
                stime = int(p.get("sim_time") or 0)
                if tid is not None and st is not None:
                    task_status_history.append((stime, str(tid), str(st)))
    task_status_history.sort(key=lambda x: x[0])

    def _status_at(task_id: str, at_sim_time: int) -> str | None:
        # Find the latest task_status_changed entry with sim_time <= at_sim_time.
        latest: str | None = None
        for stime, tid, st in task_status_history:
            if tid != task_id:
                continue
            if stime > at_sim_time:
                break
            latest = st
        return latest

    # For each task id, find the latest turn (before each claim) that reads
    # tasks (tasks.list / tasks.get). We'll resolve per-claim below.
    read_turn_times: list[int] = []
    for t in turns:
        if t.get("tool") in {"tasks.list", "tasks.get"}:
            read_turn_times.append(int(t.get("sim_time_before") or 0))
    read_turn_times.sort()

    def _last_read_before(at_sim_time: int) -> int | None:
        last: int | None = None
        for rt in read_turn_times:
            if rt > at_sim_time:
                break
            last = rt
        return last

    # Build outbound claims. A claim is an agent outbound chat/email that
    # mentions any known task id in the body.
    outbounds: list[dict[str, Any]] = []
    for m in world.get("messages", []):
        if m.get("sender_id") != agent_id:
            continue
        outbounds.append({
            "kind": "chat",
            "sim_time": int(m.get("sim_time") or 0),
            "body": m.get("body") or "",
        })
    for e in world.get("emails", []):
        if e.get("sender_id") != agent_id:
            continue
        outbounds.append({
            "kind": "email",
            "sim_time": int(e.get("sim_time") or 0),
            "body": e.get("body") or "",
        })
    outbounds.sort(key=lambda x: x["sim_time"])

    claims: list[dict[str, Any]] = []
    for ob in outbounds:
        body = ob["body"]
        referenced = [tid for tid in tasks_by_id if tid and tid in body]
        if not referenced:
            continue
        for task_id in referenced:
            last_read = _last_read_before(ob["sim_time"])
            state_at_claim = _status_at(task_id, ob["sim_time"])
            state_at_last_read = (
                _status_at(task_id, last_read) if last_read is not None else None
            )
            final_state = (tasks_by_id.get(task_id) or {}).get("status")
            claims.append({
                "claim_sim_time": ob["sim_time"],
                "claim_text": body[:300],
                "verifiable_state_snapshot": {
                    "task_id": task_id,
                    "state_at_claim": state_at_claim,
                    "state_at_last_read": state_at_last_read,
                    "last_read_sim_time": last_read,
                    "final_state": final_state,
                },
            })

    return {"claims": claims}
