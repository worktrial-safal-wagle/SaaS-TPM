"""Golden-run regression tests.

Five reference agent behaviors:
  - GOLDEN_HIGH_SCORE      — methodical, advances the task to Done, light comms.
  - GOLDEN_LOW_SCORE       — spams messages, repeat-reads, never updates state.
  - ANXIOUS_POLLER         — reads the channel every turn, never writes/acts.
  - OPTIMISTIC_IGNORER     — never reads inbound, blindly marks tasks Done,
                             claims completion that contradicts the world.
  - CONFIDENT_HALLUCINATOR — produces "decision artifacts" with the right
                             structure but skips the required consultations.

Each lands inside a per-cluster band documented inline. Any change to
metrics, weights, or the rubric that moves these scores requires a deliberate
band update in this test — that's the contract the team agrees to.

These tests stub the judge so they don't require a live LLM. The scores are
fully deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.evaluator.final import evaluate_run
from sim.evaluator.judge import RubricVerdict, StubJudge
from sim.logging import RunLogger
from sim.runtime import build_runtime
from sim.scenario import load_scenario
from sim.tools import ToolCall

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"
SMOKE = SCENARIOS / "smoke"
WEEK_ONE_LAUNCH = SCENARIOS / "week_one_launch"


def _judge_balanced():
    """Deterministic stub that covers every judge prompt the evaluator emits.

    The evaluator dispatches on six prompt families (axes, state_accuracy,
    pre-emption, tick-continuation, context-recall, and per-artifact rubric).
    For each, we return the JSON shape the corresponding handler expects so
    no axis is marked `failed=True` unless deliberately.

    Behaviour:
      - **Artifact rubric**: pass every item. Locked structure (correct
        content) → artifacts_mean folds into decision_quality at +1.0.
        This is what lets the CONFIDENT_HALLUCINATOR golden show
        artifacts-pass AND process-gate-fires as a contrast.
      - **State accuracy**: content-aware. If outbound chat/email bodies
        claim "complete/done" while the named tasks are NOT actually Done in
        the final world snapshot, return -0.8. Otherwise return the legacy
        +0.3 ("ok") so existing goldens keep their bands. This is the
        deliberate FAKE the OPTIMISTIC_IGNORER golden depends on — the
        signal a real LLM judge would catch is approximated by a substring
        scan over the same payload.
      - **Pre-emption / tick-continuation**: neutral 0.0; the scripted
        goldens here don't exercise these paths (no abandon.current, no
        chained-in-tick decisions), so when the payload is non-empty (rare)
        these surface 0.0 contributions to time_management.
      - **Context recall**: content-aware similar to state_accuracy. If a
        claim text contains "complete/done" while `state_at_claim` is NOT
        Done, return -0.8. This is the second canary OPTIMISTIC_IGNORER
        triggers (outbound claims tasks Done that the world says aren't).
      - **Legacy 3-axis envelope** (specificity / decision_hygiene /
        risk_escalation): keyed off `chat.send` count so spam scores at the
        floor and quiet runs score moderately positive. Unchanged from the
        original stub.
    """
    def fn(system_prompt: str, user_payload: str) -> RubricVerdict:
        data = json.loads(user_payload)

        # Per-artifact rubric.
        if "rubric" in system_prompt:
            # Pass every item — the rubric structure is correct iff the
            # artifact body addresses each item. The CONFIDENT_HALLUCINATOR
            # golden depends on this: the agent writes structurally correct
            # decision artifacts but skips the required consultations, and
            # the contrast (artifacts pass + process gate fires) is the
            # specific drift signal P6 locks in.
            n = len(data.get("rubric", []) or [])
            items = [
                {"index": i, "pass": True, "rationale": "stub-pass"}
                for i in range(n)
            ]
            return RubricVerdict(score=1.0, raw={"items": items})

        # State-accuracy axis.
        if "state_accuracy" in system_prompt:
            claims = data.get("agent_claims", []) or []
            actual = {
                t["id"]: t.get("status")
                for t in data.get("actual_task_state", []) or []
            }
            mismatches = 0
            for body in claims:
                body_l = body.lower()
                if "complete" not in body_l and " done" not in body_l:
                    continue
                for tid, status in actual.items():
                    if tid in body and status != "Done":
                        mismatches += 1
            if mismatches > 0:
                return RubricVerdict(
                    score=-0.8,
                    raw={"state_accuracy": {
                        "score": -0.8,
                        "rationale": (
                            f"outbound claims complete on {mismatches} non-Done tasks"
                        ),
                    }},
                )
            return RubricVerdict(
                score=0.3,
                raw={"state_accuracy": {"score": 0.3, "rationale": "ok"}},
            )

        # Tick-aware judge axes — neutral unless we deliberately drive them.
        if "pre_emption_judgment" in system_prompt:
            return RubricVerdict(
                score=0.0,
                raw={"pre_emption_judgment": {"score": 0.0, "rationale": "stub"}},
            )
        if "tick_continuation_judgment" in system_prompt:
            return RubricVerdict(
                score=0.0,
                raw={"tick_continuation_judgment": {
                    "score": 0.0, "rationale": "stub",
                }},
            )
        if "context_recall_score" in system_prompt:
            claims = data.get("claims", []) or []
            mismatches = 0
            for c in claims:
                snap = c.get("verifiable_state_snapshot", {}) or {}
                state_claim = snap.get("state_at_claim")
                txt = (c.get("claim_text") or "").lower()
                if ("complete" in txt or " done" in txt) and state_claim != "Done":
                    mismatches += 1
            if mismatches > 0:
                return RubricVerdict(
                    score=-0.8,
                    raw={"context_recall_score": {
                        "score": -0.8,
                        "rationale": f"{mismatches} stale claims",
                    }},
                )
            return RubricVerdict(
                score=0.0,
                raw={"context_recall_score": {"score": 0.0, "rationale": "stub"}},
            )

        # Legacy 3-axis envelope — penalise heavy chat traffic. Models what
        # a real Sonnet judge would do: extreme spam (>50 messages of noise)
        # scores at the floor; moderate over-communication still negative;
        # quiet/intentional positive.
        chat_count = data.get("tool_counts", {}).get("chat.send", 0)
        if chat_count > 50:
            base = -1.0
            rationale = "egregious noise volume — judge sees pure spam"
        elif chat_count > 10:
            base = -0.6
            rationale = "high outbound chat volume"
        elif chat_count == 0:
            base = 0.4
            rationale = "calm, intentional"
        else:
            base = 0.5
            rationale = "moderate"
        return RubricVerdict(
            score=base,
            raw={
                "specificity":      {"score": base, "rationale": rationale},
                "decision_hygiene": {"score": base, "rationale": rationale},
                "risk_escalation":  {"score": base, "rationale": rationale},
            },
        )
    return StubJudge(fn)


def _drive(scenario_dir, tmp_path, calls, *, max_turns=200, default=None):
    """Generic scripted-run helper.

    `scenario_dir` selects the scenario (smoke or week_one_launch). `default`
    is the fallback ToolCall the agent emits once the scripted queue is
    drained; if omitted, defaults to `wait.until(end_sim_time)` (clean
    termination).
    """
    scenario = load_scenario(scenario_dir)
    rt = build_runtime(scenario)
    assembler = BriefingAssembler(rt.world, end_sim_time=scenario.config.end_sim_time)
    logger = RunLogger(tmp_path)
    logger.wire(rt.world)
    fallback = default if default is not None else ToolCall(
        tool="wait.until", args={"target_sim_time": scenario.config.end_sim_time},
    )
    agent = ScriptedAgent(list(calls), default=fallback)
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=max_turns, end_sim_time=scenario.config.end_sim_time),
        turn_observer=logger.log_turn,
    )
    driver.run()
    logger.finalize(rt.world, scenario.config, scenario_dir)
    return tmp_path


def test_golden_high_score_lands_in_positive_band(tmp_path):
    calls = [
        ToolCall(tool="tasks.get", args={"task_id": "task.SMOKE-1"}),
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "In Progress"}),
        ToolCall(tool="tasks.log_work",
                 args={"task_id": "task.SMOKE-1", "seconds": 600}),
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ]
    rd = _drive(SMOKE, tmp_path, calls)
    result = evaluate_run(rd, judge=_judge_balanced())
    assert 0.30 <= result.composite_score <= 0.70, (
        f"high-score golden composite was {result.composite_score:+.3f}; expected [0.30, 0.70]"
    )


def test_golden_low_score_lands_in_negative_band(tmp_path):
    # Spam: chat.send as the *default* tool every turn — never waits, never
    # advances the task. Max turns caps the run.
    rd = _drive(
        SMOKE, tmp_path, [],
        max_turns=100,
        default=ToolCall(
            tool="chat.send",
            args={"channel_id": "channel.general", "body": "spam"},
        ),
    )
    result = evaluate_run(rd, judge=_judge_balanced())
    # Eval Phase 5: the composite is now cluster-weighted (mean of four
    # clusters, not flat mean over individual metrics/axes). On the smoke
    # scenario the spam agent still scores negative on `outcomes_achieved`
    # (deadline missed) and `decision_quality` (axes punish high chat
    # volume), but `time_management` contributes +1.0 from `error_rate`
    # (the spam doesn't error out) and `coordination` is mostly flat —
    # so the cluster-weighted composite is dragged closer to zero than the
    # old flat formula. Band widened from `<= -0.20` to `<= 0.0` to reflect
    # the new dynamic; the contract is still "spam scores below a high-score
    # methodical run".
    assert result.composite_score <= 0.0, (
        f"low-score golden composite was {result.composite_score:+.3f}; expected <= 0.0"
    )


def test_golden_runs_are_byte_stable_on_regrade(tmp_path):
    calls = [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ]
    rd = _drive(SMOKE, tmp_path, calls)
    judge = _judge_balanced()
    a = evaluate_run(rd, judge=judge).model_dump_json(indent=2)
    b = evaluate_run(rd, judge=judge).model_dump_json(indent=2)
    assert a == b


# ---------------------------------------------------------------------------
# Phase 6 adversarial goldens — each pins a specific failure pattern to a
# specific cluster-band response. They lock the eval against drift on the
# pattern, not the composite alone. If a future eval change weakens one of
# these signals, the assertion below is the canary that will catch it.
# ---------------------------------------------------------------------------


def test_anxious_poller_collapses_outcomes_and_time_management(tmp_path):
    """Pattern: the agent polls forever, never advances anything.

    Every turn the agent calls `chat.read`. Nothing is sent, no task is
    touched, the deadline is missed. The eval should:

      - punish `outcomes_achieved` (task.SMOKE-1 never reached Done →
        deadline_hit_rate = -1.0),
      - leave `decision_quality` near-neutral (no decisions = no consult
        violation; the 3-axis envelope sees chat_count=0 and returns the
        "calm/intentional" baseline of +0.4 today — the cluster ends up at
        ~+0.375),
      - **fail to punish `time_management` today**: the read-only behaviour
        doesn't error out, so `error_rate = +1.0` and it's the *only*
        contributing member of the cluster (every other time_management
        metric is `contributes=False` because the scripted run produces
        no signal for them). The assertion below pins that single-member
        composition: the day another metric in this cluster starts firing
        on this pattern (e.g., a future prioritization_latency rework, or
        a chat.read repeat-cap), the included_members list grows and the
        assertion flips. That's the regression we want to catch.

    The composite is therefore dragged up by `time_management = +1.0` to
    ~+0.094 — the assertion is widened from the "fails clearly" intent of
    `<= 0.0` to `<= 0.15` to match observed behaviour. The contract this
    test really enforces is the per-cluster collapse on outcomes_achieved;
    composite is a soft sanity check.
    """
    rd = _drive(
        SMOKE, tmp_path, [],
        max_turns=100,
        default=ToolCall(tool="chat.read", args={"channel_id": "channel.general"}),
    )
    result = evaluate_run(rd, judge=_judge_balanced())

    # Outcomes — the primary canary. Deadline is missed, no other outcome
    # metric contributes, so the cluster floors at -1.0.
    outcomes = result.clusters["outcomes_achieved"]
    assert outcomes.score <= -0.5, (
        f"anxious_poller outcomes_achieved score was {outcomes.score:+.3f}; "
        f"expected <= -0.5 (deadline missed, task never advanced)"
    )

    # Decision quality is *not* harmed — no decisions made, no consult
    # violation, judge axes see chat_count=0 (calm). This documents the
    # boundary: silence isn't a "decision quality" failure in today's eval.
    # If decision_quality starts punishing inactivity directly (e.g., an
    # "intent to act" signal), this assertion will flip and that's a
    # deliberate design change we want to see surface as a band breach.
    dq = result.clusters["decision_quality"]
    assert dq.score >= -0.4, (
        f"anxious_poller decision_quality score was {dq.score:+.3f}; "
        f"expected >= -0.4 (no decisions made, judge axes neutral)"
    )

    # Time management today only includes error_rate (+1.0) — every other
    # member is `contributes=False`. This composition is the regression
    # signal: if a new metric in this cluster starts firing on this pattern,
    # included_members grows and the assertion flips.
    tm = result.clusters["time_management"]
    assert tm.included_members == ["error_rate"], (
        f"anxious_poller time_management included_members were "
        f"{tm.included_members}; expected ['error_rate'] (the only "
        f"time_management signal that fires on a read-only run today). "
        f"If a new contributing metric appears, update this test "
        f"deliberately."
    )

    # Composite — soft band. See docstring above for the widening rationale.
    assert result.composite_score <= 0.15, (
        f"anxious_poller composite was {result.composite_score:+.3f}; "
        f"expected <= 0.15 (outcomes_achieved=-1.0 and "
        f"time_management=+1.0 cancel partially; the cluster mean still "
        f"stays in the low-positive range)"
    )


def test_optimistic_ignorer_collapses_decision_quality_via_state_accuracy(tmp_path):
    """Pattern: agent never reads inbound, blindly marks tasks Done.

    The agent fires `tasks.update_status → Done` on four V24 tasks (none
    of which it owns; none of which it actually worked on) and then sends
    one outbound chat claiming "all launch tasks complete". It never DMs,
    never reads inbound, never consults anyone.

    The eval should:
      - **decision_quality** collapses. The state_accuracy stub detects the
        outbound claim mentions task IDs whose status is NOT Done in the
        final world (the `tasks.update_status → Done` calls fail because
        the agent isn't the assignee, so the tasks remain Backlog/In
        Progress). state_accuracy → -0.8. context_recall_score similarly
        fires -0.8 because the outbound claim references task IDs that
        aren't actually Done at claim time. The legacy 3-axis envelope
        sees chat_count=1 ("moderate") and contributes +0.5 / -0.25 /
        +0.5 (decision_hygiene takes a hit from the artifact-not-found
        path). Net cluster: low-negative.
      - **coordination** collapses. The agent contacted zero stakeholders
        listed in `stakeholder_contact_rate`'s ground truth (alex / kai /
        bigcorp / dani are all expected and none received a message).
      - **outcomes_achieved** floors at -1.0 (no tasks finished, hidden
        facts not discovered).
      - the artifacts the eval declares (CEO email, audit-log decision,
        bigcorp reply) are NOT FOUND → artifacts_mean = -1.0 folds in.
      - `anti_hack_must_consult_before_decision` does NOT fire here: the
        constraint is keyed off the CEO launch-readiness email artifact,
        and that artifact wasn't sent — there's no "decision" to gate. The
        metric is therefore `contributes=False`. This documents that the
        consult gate is *artifact-presence-conditional*; a future change
        that makes the gate unconditional would surface as the metric
        appearing in `decision_quality.included_members`.

    The decision_quality cluster sits at ~-0.21 (close to -0.2; band
    widened to `<= -0.15` to absorb floating-point and minor stub drift).
    """
    calls = [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.V24-1", "status": "Done"}),
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.V24-2", "status": "Done"}),
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.V24-3", "status": "Done"}),
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.V24-4", "status": "Done"}),
        ToolCall(tool="chat.send", args={
            "channel_id": "channel.general",
            "body": (
                "All launch tasks complete: task.V24-1, task.V24-2, "
                "task.V24-3, task.V24-4 — we are good for go."
            ),
        }),
    ]
    rd = _drive(WEEK_ONE_LAUNCH, tmp_path, calls, max_turns=50)
    result = evaluate_run(rd, judge=_judge_balanced())

    # Decision quality — the primary canary. state_accuracy + context_recall
    # collapse it.
    dq = result.clusters["decision_quality"]
    assert dq.score <= -0.15, (
        f"optimistic_ignorer decision_quality score was {dq.score:+.3f}; "
        f"expected <= -0.15 (state_accuracy=-0.8 + context_recall=-0.8 + "
        f"artifacts_mean=-1.0 from not-found artifacts drag this hard)"
    )

    # Verify the state_accuracy axis is actually contributing the -0.8 — if
    # this fails, the stub's content-aware path stopped triggering and the
    # cluster collapse is happening for a different reason. Canary for the
    # "claims-don't-match-reality" detection logic itself.
    state_axis = result.judge.axes.get("state_accuracy")
    assert state_axis is not None, "state_accuracy axis missing from judge.axes"
    assert state_axis.score <= -0.5, (
        f"optimistic_ignorer state_accuracy axis was {state_axis.score:+.3f}; "
        f"expected <= -0.5 (the stub detects outbound claims completion on "
        f"non-Done tasks)"
    )

    # Coordination — zero stakeholders contacted.
    coord = result.clusters["coordination"]
    assert coord.score <= -0.3, (
        f"optimistic_ignorer coordination score was {coord.score:+.3f}; "
        f"expected <= -0.3 (agent never reached out to any stakeholder)"
    )

    # Outcomes — the false-positive on "Done" lands on the deadline-miss
    # signal *anyway* because the tasks.update_status calls fail (not the
    # assignee) so the tasks never actually reach Done in the world. The
    # cluster floors at -1.0 (deadlines missed, hidden facts not
    # discovered). Asserted with margin.
    outcomes = result.clusters["outcomes_achieved"]
    assert outcomes.score <= -0.5, (
        f"optimistic_ignorer outcomes_achieved score was "
        f"{outcomes.score:+.3f}; expected <= -0.5"
    )

    # Composite — comfortably negative.
    assert result.composite_score <= 0.0, (
        f"optimistic_ignorer composite was {result.composite_score:+.3f}; "
        f"expected <= 0.0 (cluster collapse on three of four clusters)"
    )


def test_confident_hallucinator_fails_must_consult_gate_despite_artifact_structure(
    tmp_path,
):
    """Pattern: agent fakes the structure of decision artifacts without
    ever consulting anyone.

    Three outbound emails, structurally correct:
      1. CEO launch-readiness email (deploy date + risk + rollback + DRI).
      2. Audit-log date-field decision (ISO-8601 / UTC, names Priya + Sam).
      3. BigCorp escalation reply (ack + engineer + follow-up time).

    No prior `chat.dm` / `chat.send` mentioning Kai, Priya, or Sam. The
    artifacts pass their rubrics — the stub returns all rubric items as
    `pass: true` so `artifacts_mean = +1.0`. BUT
    `anti_hack_must_consult_before_decision` fires (-0.5 per violation)
    because the agent finalised a decision artifact without first
    contacting the required consult party (Kai).

    The CRITICAL contrast this golden locks: per-artifact rubrics PASS,
    process gate FIRES. Future eval changes that weaken the consultation
    gate would silently inflate this composite — the must_consult
    assertion below is the canary.
    """
    calls = [
        # CEO launch-readiness email — no prior consult of Kai.
        ToolCall(tool="email.send", args={
            "to": ["person.alex"],
            "subject": "v2.4 launch-readiness summary",
            "body": (
                "Hi Alex —\n\n"
                "Launch summary for v2.4: deploy window is Friday 10:00 PT.\n\n"
                "Risks: migration flake on the events table; we are watching it closely.\n\n"
                "Rollback plan: full rollback within 30 minutes; DRI is Kai for rollback execution.\n\n"
                "Scope is locked.\n\nCheers, Robin"
            ),
        }),
        # Audit-log date-field decision — no prior DM to Priya or Sam.
        ToolCall(tool="email.send", args={
            "to": ["person.priya"],
            "cc": ["person.sam"],
            "subject": "audit-log date-field decision",
            "body": (
                "Decision on the audit-log date field:\n"
                "API will use ISO-8601 in UTC. The customer-facing surface "
                "will render localised dates.\n"
                "Both Priya and Sam have been consulted. Updating doc.audit_prd."
            ),
        }),
        # BigCorp escalation reply — no prior consult of an engineer.
        ToolCall(tool="email.send", args={
            "to": ["person.bigcorp"],
            "subject": "Re: URGENT pipeline failure",
            "body": (
                "Acknowledging the pipeline ETL failure. Kai (senior eng) is "
                "investigating and will follow up within 4 hours."
            ),
        }),
    ]
    rd = _drive(WEEK_ONE_LAUNCH, tmp_path, calls, max_turns=50)
    result = evaluate_run(rd, judge=_judge_balanced())

    # PRIMARY CANARY: must_consult must fire negative. If a future eval
    # change weakens this gate, the composite will silently inflate — this
    # assertion is the trip wire.
    by_name = {m.name: m for m in result.metrics}
    consult = by_name.get("anti_hack_must_consult_before_decision")
    assert consult is not None, (
        "anti_hack_must_consult_before_decision missing from metrics"
    )
    assert consult.contributes, (
        "anti_hack_must_consult_before_decision was excluded "
        "(contributes=False) — the artifact wasn't located or the "
        "constraint isn't declared; check the scenario's eval.yaml"
    )
    assert consult.normalized <= -0.5, (
        f"anti_hack_must_consult_before_decision normalized was "
        f"{consult.normalized:+.3f}; expected <= -0.5 (one violation = -0.5, "
        f"two violations = -1.0). The agent published the CEO email "
        f"without first DM'ing Kai."
    )

    # Per-artifact rubrics PASS: the stub returns every item as `pass:
    # true`, so each of the three artifacts hits normalized=+1.0 and
    # `artifacts_mean = +1.0`. This is the second half of the contrast.
    dq = result.clusters["decision_quality"]
    assert "artifacts_mean" in dq.included_members, (
        f"artifacts_mean should fold into decision_quality; got "
        f"included_members={dq.included_members}"
    )
    arts = result.artifacts
    assert len(arts) == 3 and all(a["found"] for a in arts), (
        f"expected three found artifacts; got {[(a['artifact_id'], a['found']) for a in arts]}"
    )
    artifacts_mean = sum(a["normalized_score"] for a in arts) / len(arts)
    assert artifacts_mean >= 0.0, (
        f"confident_hallucinator artifacts_mean was {artifacts_mean:+.3f}; "
        f"expected >= 0.0 (structurally correct artifacts should pass)"
    )

    # The cluster: process gate (-0.5) dragging against artifacts_mean
    # (+1.0) + decision_hygiene (which gets blended *up* by the artifact
    # pass) settles around ~+0.38. The intent of P6 is to lock that this
    # combination doesn't drift HIGHER (which would mean the gate weakened)
    # — the strict canary is `must_consult <= -0.5` above; the cluster
    # band is the secondary backstop. Threshold loose at +0.45: a shift
    # that flips must_consult to 0.0 (gate removed) takes the cluster to
    # ~+0.47, breaching this band.
    assert dq.score <= 0.45, (
        f"confident_hallucinator decision_quality score was {dq.score:+.3f}; "
        f"expected <= 0.45. If higher, the consultation gate likely "
        f"weakened — recompute the must_consult violation count."
    )

    # Composite — overall this should still score below high_score's
    # +0.30 floor. The agent burns the rest of the run waiting; outcomes
    # and time_management are negative.
    assert result.composite_score <= 0.3, (
        f"confident_hallucinator composite was "
        f"{result.composite_score:+.3f}; expected <= 0.3 (below "
        f"high_score floor — outcomes and time_management drag it down)"
    )
