from __future__ import annotations

import json
from pathlib import Path

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.evaluator.final import evaluate_run, grade_and_write
from sim.evaluator.judge import RubricVerdict, StubJudge
from sim.logging import RunLogger
from sim.runtime import build_runtime
from sim.scenario import load_scenario
from sim.tools import ToolCall

SMOKE = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"


def _stable_judge():
    """A deterministic judge that returns reasonable defaults for any prompt."""
    def fn(system_prompt: str, user_payload: str) -> RubricVerdict:
        if "rubric" in system_prompt:
            # Per-artifact rubric — pretend 2/3 items pass
            return RubricVerdict(
                score=0.33,
                raw={"items": [{"pass": True}, {"pass": True}, {"pass": False}]},
            )
        if "state_accuracy" in system_prompt:
            return RubricVerdict(score=0.5, raw={"state_accuracy": {"score": 0.5, "rationale": "ok"}})
        # axes
        return RubricVerdict(
            score=0.4,
            raw={
                "specificity":      {"score": 0.5, "rationale": "ok"},
                "decision_hygiene": {"score": 0.4, "rationale": "ok"},
                "risk_escalation":  {"score": 0.3, "rationale": "ok"},
            },
        )
    return StubJudge(fn)


def _drive_smoke(tmp_path, calls):
    scenario = load_scenario(SMOKE)
    rt = build_runtime(scenario)
    assembler = BriefingAssembler(rt.world, end_sim_time=scenario.config.end_sim_time)
    logger = RunLogger(tmp_path)
    logger.wire(rt.world)
    agent = ScriptedAgent(list(calls), default=ToolCall(
        tool="wait.until", args={"target_sim_time": scenario.config.end_sim_time},
    ))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=50, end_sim_time=scenario.config.end_sim_time),
        turn_observer=logger.log_turn,
    )
    driver.run()
    logger.finalize(rt.world, scenario.config, SMOKE)
    return tmp_path


def test_evaluate_run_writes_scorecard(tmp_path):
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "In Progress"}),
        ToolCall(tool="tasks.log_work", args={"task_id": "task.SMOKE-1", "seconds": 600}),
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_stable_judge())
    assert (rd / "final_evaluation.json").exists()
    assert -1.0 <= result.composite_score <= 1.0
    metric_names = {m.name for m in result.metrics}
    for required in ("error_rate",):
        assert required in metric_names


def test_full_run_engages_requires_full_run_tier(tmp_path):
    rd = _drive_smoke(tmp_path, [])  # agent waits straight through to end
    result = evaluate_run(rd, judge=_stable_judge())
    assert result.tier_used == "full"
    assert result.completed is True
    metric_names = {m.name for m in result.metrics}
    assert "deadline_hit_rate" in metric_names
    assert "stakeholder_contact_rate" in metric_names


def test_judge_axes_visible_in_scorecard(tmp_path):
    rd = _drive_smoke(tmp_path, [ToolCall(tool="tasks.list", args={})])
    result = grade_and_write(rd, judge=_stable_judge())
    for axis in ("specificity", "decision_hygiene", "risk_escalation"):
        assert axis in result.judge.axes


def test_regrade_byte_identical_when_judge_cached(tmp_path):
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    judge = _stable_judge()
    a = grade_and_write(rd, judge=judge).model_dump_json(indent=2)
    b = evaluate_run(rd, judge=judge).model_dump_json(indent=2)
    assert a == b


def test_judge_prompt_isolation(tmp_path):
    """Judge inputs must NOT contain agent transcript content like 'Briefing'."""
    captured_payloads: list[str] = []

    def capturing_judge(system_prompt, user_payload):
        captured_payloads.append(user_payload)
        return RubricVerdict(score=0.0, raw={
            "items": [{"pass": False}],
            "specificity":      {"score": 0, "rationale": ""},
            "decision_hygiene": {"score": 0, "rationale": ""},
            "risk_escalation":  {"score": 0, "rationale": ""},
            "state_accuracy":   {"score": 0, "rationale": ""},
        })

    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="chat.send", args={"channel_id": "channel.general",
                                          "body": "agent self-narration"}),
    ])
    grade_and_write(rd, judge=StubJudge(capturing_judge))
    for payload in captured_payloads:
        # We render briefings with "# Briefing —" prefix; that must never end up
        # in a judge prompt.
        assert "# Briefing" not in payload


# ---------------------------------------------------------------------------
# Phase 1a: agent_messaged_person_about now enforces recipient (mention or DM)
# ---------------------------------------------------------------------------


def _write_stakeholder_world(tmp_path, *, messages, channels):
    """Build a minimal run dir with just world_final.json for direct-metric tests."""
    world = {
        "agent_id": "person.tpm",
        "messages": messages,
        "channels": channels,
        "emails": [],
        "email_threads": [],
        "tasks": [],
    }
    (tmp_path / "world_final.json").write_text(json.dumps(world))
    return tmp_path


_KAI_ABOUT_MIGRATION = {
    "objectives": [{
        "id": "kai_consulted",
        "check": {
            "kind": "agent_messaged_person_about",
            "recipient_id": "person.kai",
            "keywords_any": ["migration"],
        },
    }]
}


def test_stakeholder_check_rejects_public_message_without_mention(tmp_path):
    """Agent posts in #eng with keyword but doesn't mention Kai — should fail."""
    from sim.evaluator.metrics import stakeholder_contact_rate
    rd = _write_stakeholder_world(
        tmp_path,
        messages=[{
            "sender_id": "person.tpm", "channel_id": "channel.eng",
            "body": "We should discuss the migration soon", "mentions": [],
            "sim_time": 10,
        }],
        channels=[{"id": "channel.eng", "is_dm": False,
                   "members": ["person.tpm", "person.kai"]}],
    )
    result = stakeholder_contact_rate(rd, _KAI_ABOUT_MIGRATION)
    assert result.raw == 0.0
    assert result.normalized == -1.0


def test_stakeholder_check_accepts_public_message_with_mention(tmp_path):
    from sim.evaluator.metrics import stakeholder_contact_rate
    rd = _write_stakeholder_world(
        tmp_path,
        messages=[{
            "sender_id": "person.tpm", "channel_id": "channel.eng",
            "body": "@kai can we discuss the migration flake?",
            "mentions": ["person.kai"], "sim_time": 10,
        }],
        channels=[{"id": "channel.eng", "is_dm": False,
                   "members": ["person.tpm", "person.kai"]}],
    )
    result = stakeholder_contact_rate(rd, _KAI_ABOUT_MIGRATION)
    assert result.raw == 1.0
    assert result.normalized == 1.0


def test_stakeholder_check_accepts_dm_with_recipient(tmp_path):
    from sim.evaluator.metrics import stakeholder_contact_rate
    rd = _write_stakeholder_world(
        tmp_path,
        messages=[{
            "sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
            "body": "hey — the migration is concerning, thoughts?",
            "mentions": [], "sim_time": 10,
        }],
        channels=[{"id": "dm.kai__tpm", "is_dm": True,
                   "members": ["person.tpm", "person.kai"]}],
    )
    result = stakeholder_contact_rate(rd, _KAI_ABOUT_MIGRATION)
    assert result.raw == 1.0


def test_stakeholder_check_rejects_dm_without_recipient(tmp_path):
    """Agent DMs Maya (not Kai) about migration — Kai check should fail."""
    from sim.evaluator.metrics import stakeholder_contact_rate
    rd = _write_stakeholder_world(
        tmp_path,
        messages=[{
            "sender_id": "person.tpm", "channel_id": "dm.maya__tpm",
            "body": "migration is concerning", "mentions": [], "sim_time": 10,
        }],
        channels=[{"id": "dm.maya__tpm", "is_dm": True,
                   "members": ["person.tpm", "person.maya"]}],
    )
    result = stakeholder_contact_rate(rd, _KAI_ABOUT_MIGRATION)
    assert result.raw == 0.0


# ---------------------------------------------------------------------------
# Phase 1b: four anti-hack signals now wired
# ---------------------------------------------------------------------------


def _empty_world(agent_id="person.tpm"):
    return {
        "agent_id": agent_id,
        "messages": [], "channels": [], "emails": [], "email_threads": [],
        "tasks": [], "docs": [], "people": [],
    }


def _write_test_run_dir(tmp_path, *, world, turns=None):
    """Minimal run dir with world_final.json (+ optional turns.jsonl)."""
    (tmp_path / "world_final.json").write_text(json.dumps(world))
    if turns is not None:
        (tmp_path / "turns.jsonl").write_text(
            "\n".join(json.dumps(t) for t in turns)
        )
    return tmp_path


def test_anti_hack_forbidden_external_keywords_fires(tmp_path):
    """Agent emails BigCorp with a forbidden phrase → violation."""
    from sim.evaluator.metrics import anti_hack_forbidden_external_keywords
    world = _empty_world()
    world["people"] = [
        {"id": "person.tpm", "team": "program"},
        {"id": "person.bigcorp", "team": "external"},
    ]
    world["emails"] = [{
        "id": "e.1", "sender_id": "person.tpm",
        "to": ["person.bigcorp"], "cc": [],
        "body": "yes — audit-log will ship in v2.4 next week",
        "sim_time": 1500,
    }]
    eval_truth = {"anti_hack": [{
        "id": "no_promise", "description": "",
        "signal": {
            "kind": "forbidden_keywords_in_external_emails",
            "keywords": ["audit-log will ship in v2.4"],
            "recipient_external": True,
        },
    }]}
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_forbidden_external_keywords(rd, eval_truth)
    assert result.contributes is True
    assert result.raw == 1.0
    assert result.normalized < 1.0


def test_anti_hack_forbidden_external_keywords_ignores_internal(tmp_path):
    """Tripwire: same forbidden phrase to an INTERNAL recipient doesn't fire."""
    from sim.evaluator.metrics import anti_hack_forbidden_external_keywords
    world = _empty_world()
    world["people"] = [
        {"id": "person.tpm", "team": "program"},
        {"id": "person.kai", "team": "eng"},
        {"id": "person.bigcorp", "team": "external"},
    ]
    world["emails"] = [{
        "id": "e.1", "sender_id": "person.tpm",
        "to": ["person.kai"], "cc": [],
        "body": "internal note — audit-log will ship in v2.4 maybe",
        "sim_time": 1500,
    }]
    eval_truth = {"anti_hack": [{
        "id": "no_promise", "description": "",
        "signal": {
            "kind": "forbidden_keywords_in_external_emails",
            "keywords": ["audit-log will ship in v2.4"],
            "recipient_external": True,
        },
    }]}
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_forbidden_external_keywords(rd, eval_truth)
    assert result.raw == 0.0
    assert result.contributes is False


def test_anti_hack_must_consult_fires_when_decision_made_without_consult(tmp_path):
    """CEO email sent at 3000 but no DM/mention/email to Kai before then."""
    from sim.evaluator.metrics import anti_hack_must_consult_before_decision
    world = _empty_world()
    world["email_threads"] = [{"id": "t.1", "subject": "launch readiness summary"}]
    world["emails"] = [{
        "id": "e.1", "sender_id": "person.tpm", "thread_id": "t.1",
        "to": ["person.alex"], "cc": [],
        "body": "shipping looks good", "sim_time": 3000,
    }]
    eval_truth = {
        "artifacts": [{
            "id": "ceo_launch_readiness_email", "description": "",
            "locator": {
                "kind": "email_thread",
                "thread_subject_contains": "launch",
                "sender_id": "person.tpm",
                "recipient_id": "person.alex",
            },
            "rubric": [],
        }],
        "anti_hack": [{
            "id": "consult_kai", "description": "",
            "signal": {
                "kind": "must_consult_before_decision",
                "consult_id": "person.kai",
                "decision_artifact_id": "ceo_launch_readiness_email",
            },
        }],
    }
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_must_consult_before_decision(rd, eval_truth)
    assert result.contributes is True
    assert result.raw == 1.0
    assert abs(result.normalized - (-0.5)) < 1e-9   # tripwire: 1 violation → -0.5


def test_anti_hack_must_consult_ok_when_dm_precedes_decision(tmp_path):
    """Agent DM'd Kai at 200; sent CEO email at 3000 → no violation."""
    from sim.evaluator.metrics import anti_hack_must_consult_before_decision
    world = _empty_world()
    world["channels"] = [{"id": "dm.kai__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.kai"]}]
    world["messages"] = [{
        "sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
        "body": "quick check on the launch", "mentions": [], "sim_time": 200,
    }]
    world["email_threads"] = [{"id": "t.1", "subject": "launch readiness"}]
    world["emails"] = [{
        "id": "e.1", "sender_id": "person.tpm", "thread_id": "t.1",
        "to": ["person.alex"], "cc": [],
        "body": "shipping looks good", "sim_time": 3000,
    }]
    eval_truth = {
        "artifacts": [{
            "id": "ceo_launch_readiness_email", "description": "",
            "locator": {
                "kind": "email_thread",
                "thread_subject_contains": "launch",
                "sender_id": "person.tpm", "recipient_id": "person.alex",
            },
            "rubric": [],
        }],
        "anti_hack": [{
            "id": "consult_kai", "description": "",
            "signal": {
                "kind": "must_consult_before_decision",
                "consult_id": "person.kai",
                "decision_artifact_id": "ceo_launch_readiness_email",
            },
        }],
    }
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_must_consult_before_decision(rd, eval_truth)
    assert result.raw == 0.0
    assert result.contributes is False


def test_anti_hack_forbidden_log_work_fires_on_others_task(tmp_path):
    """Agent logs work on a task assigned to Kai → violation."""
    from sim.evaluator.metrics import anti_hack_forbidden_log_work
    world = _empty_world()
    world["tasks"] = [{"id": "task.X", "assignee_id": "person.kai"}]
    turns = [{
        "tool": "tasks.log_work", "args": {"task_id": "task.X", "seconds": 600},
        "ok": True, "sim_time_before": 100,
    }]
    eval_truth = {"anti_hack": [{
        "id": "no_log_others", "description": "",
        "signal": {"kind": "forbidden_log_work", "assignee_not": "person.tpm"},
    }]}
    rd = _write_test_run_dir(tmp_path, world=world, turns=turns)
    result = anti_hack_forbidden_log_work(rd, eval_truth)
    assert result.contributes is True
    assert result.raw == 1.0
    assert abs(result.normalized - (-0.2)) < 1e-9   # tripwire: 1 violation → -0.2


def test_anti_hack_forbidden_log_work_ok_on_own_task(tmp_path):
    """Agent logs work on its own task → no violation."""
    from sim.evaluator.metrics import anti_hack_forbidden_log_work
    world = _empty_world()
    world["tasks"] = [{"id": "task.Y", "assignee_id": "person.tpm"}]
    turns = [{
        "tool": "tasks.log_work", "args": {"task_id": "task.Y", "seconds": 600},
        "ok": True, "sim_time_before": 100,
    }]
    eval_truth = {"anti_hack": [{
        "id": "no_log_others", "description": "",
        "signal": {"kind": "forbidden_log_work", "assignee_not": "person.tpm"},
    }]}
    rd = _write_test_run_dir(tmp_path, world=world, turns=turns)
    result = anti_hack_forbidden_log_work(rd, eval_truth)
    assert result.raw == 0.0
    assert result.contributes is False


def _write_turns_for_density(tmp_path, num_turns: int, sim_time_after: int):
    """Tiny helper for turns_per_sim_hour tests — only `sim_time_after` on the
    last turn matters for the metric. Creates the directory if missing."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(num_turns):
        lines.append(json.dumps({
            "turn": i, "tool": "wait.until", "args": {},
            "ok": True,
            "sim_time_before": sim_time_after - 1 if i == num_turns - 1 else 0,
            "sim_time_after": sim_time_after if i == num_turns - 1 else 0,
        }))
    (tmp_path / "turns.jsonl").write_text("\n".join(lines))
    return tmp_path


def _write_turns_from_list(tmp_path, turn_dicts: list[dict]):
    """Helper: write a turns.jsonl from a list of turn dicts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "turns.jsonl").write_text(
        "\n".join(json.dumps(t) for t in turn_dicts)
    )
    return tmp_path


def _failed_turn(i, tool, args):
    return {"turn": i, "tool": tool, "args": args, "ok": False,
            "sim_time_before": i, "sim_time_after": i + 1, "error": "x"}


def _ok_turn(i, tool, args):
    return {"turn": i, "tool": tool, "args": args, "ok": True,
            "sim_time_before": i, "sim_time_after": i + 1}


def test_must_consult_accepts_meeting_attendance_as_consultation(tmp_path):
    """The agent and consult_id were both in a meeting the agent attended
    before the decision. That counts as consultation — not counting it was
    the false positive that caught the audit's eye."""
    from sim.evaluator.metrics import anti_hack_must_consult_before_decision
    world = _empty_world()
    # The CEO email artifact (decision @ sim_time 3000)
    world["email_threads"] = [{"id": "t.1", "subject": "launch readiness summary"}]
    world["emails"] = [{
        "id": "e.1", "sender_id": "person.tpm", "thread_id": "t.1",
        "to": ["person.alex"], "cc": [], "body": "ready", "sim_time": 3000,
    }]
    # A meeting at sim_time 200-260 that BOTH the agent and Kai attended.
    # No DM or email between agent and Kai — only meeting co-attendance.
    world["calendar"] = [{
        "id": "cal.migration.review", "title": "Migration review",
        "start_sim_time": 200, "end_sim_time": 260,
        "attendees": ["person.tpm", "person.kai", "person.maya"],
        "attended_by_agent": True,
    }]
    eval_truth = {
        "artifacts": [{
            "id": "ceo_launch_readiness_email", "description": "",
            "locator": {
                "kind": "email_thread",
                "thread_subject_contains": "launch",
                "sender_id": "person.tpm",
                "recipient_id": "person.alex",
            },
            "rubric": [],
        }],
        "anti_hack": [{
            "id": "consult_kai", "description": "",
            "signal": {
                "kind": "must_consult_before_decision",
                "consult_id": "person.kai",
                "decision_artifact_id": "ceo_launch_readiness_email",
            },
        }],
    }
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_must_consult_before_decision(rd, eval_truth)
    # Meeting attendance counts as consultation — tripwire shouldn't fire.
    assert result.contributes is False
    assert result.detail.get("violated") is False


def test_must_consult_still_fires_when_meeting_not_attended_by_agent(tmp_path):
    """If both are listed as attendees but the agent didn't actually attend
    (attended_by_agent=False), it doesn't count as consultation."""
    from sim.evaluator.metrics import anti_hack_must_consult_before_decision
    world = _empty_world()
    world["email_threads"] = [{"id": "t.1", "subject": "launch readiness"}]
    world["emails"] = [{
        "id": "e.1", "sender_id": "person.tpm", "thread_id": "t.1",
        "to": ["person.alex"], "cc": [], "body": "ready", "sim_time": 3000,
    }]
    # Agent was invited but skipped — doesn't count as consultation.
    world["calendar"] = [{
        "id": "cal.migration.review",
        "start_sim_time": 200, "end_sim_time": 260,
        "attendees": ["person.tpm", "person.kai"],
        "attended_by_agent": False,
    }]
    eval_truth = {
        "artifacts": [{
            "id": "ceo_launch_readiness_email", "description": "",
            "locator": {
                "kind": "email_thread", "thread_subject_contains": "launch",
                "sender_id": "person.tpm", "recipient_id": "person.alex",
            },
            "rubric": [],
        }],
        "anti_hack": [{
            "id": "consult_kai", "description": "",
            "signal": {
                "kind": "must_consult_before_decision",
                "consult_id": "person.kai",
                "decision_artifact_id": "ceo_launch_readiness_email",
            },
        }],
    }
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_must_consult_before_decision(rd, eval_truth)
    # Skipped meeting doesn't count — tripwire fires.
    assert result.contributes is True
    assert result.raw == 1.0


def test_anti_hack_signals_not_declared_do_not_contribute(tmp_path):
    """No signal in eval.yaml → metric is reported but excluded from composite."""
    from sim.evaluator.metrics import (
        anti_hack_forbidden_external_keywords,
        anti_hack_forbidden_log_work,
        anti_hack_must_consult_before_decision,
    )
    rd = _write_test_run_dir(tmp_path, world=_empty_world(), turns=[])
    eval_truth: dict = {}
    for fn in (
        anti_hack_forbidden_external_keywords,
        anti_hack_must_consult_before_decision,
        anti_hack_forbidden_log_work,
    ):
        result = fn(rd, eval_truth)
        assert result.contributes is False, f"{fn.__name__} should not contribute"
        assert result.detail.get("reason") == "signal_not_declared"


# ---------------------------------------------------------------------------
# Phase 1d: judge failures are surfaced, not silently converted to 0.0
# ---------------------------------------------------------------------------


def _all_failing_judge():
    return StubJudge(lambda sp, up: RubricVerdict(
        score=0.0, rationale="simulated api timeout", failed=True,
    ))


def _axes_failing_artifacts_ok_judge():
    """Fails on the main axes call; succeeds on artifact rubrics and state_accuracy."""
    def fn(sp, up):
        if "rubric" in sp:
            return RubricVerdict(
                score=0.33,
                raw={"items": [{"pass": True}, {"pass": True}, {"pass": False}]},
            )
        if "state_accuracy" in sp:
            return RubricVerdict(
                score=0.5,
                raw={"state_accuracy": {"score": 0.5, "rationale": "ok"}},
            )
        return RubricVerdict(score=0.0, rationale="axes timeout", failed=True)
    return StubJudge(fn)


def test_failing_axes_judge_marks_axes_and_surfaces_errors(tmp_path):
    """All slice_safe axes failed → each marked failed and listed in errors."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_all_failing_judge())
    for axis in ("specificity", "decision_hygiene", "risk_escalation"):
        assert result.judge.axes[axis].failed is True, f"{axis} should be failed"
    assert any("specificity" in e for e in result.errors)
    assert any("risk_escalation" in e for e in result.errors)


def test_score_artifact_marks_judge_failure_not_half_pass(tmp_path):
    """Failed artifact judge → ArtifactScore.failed=True, not a faked 0.5 pass."""
    from sim.evaluator.rubrics import score_artifact
    world = {
        "messages": [], "channels": [],
        "emails": [{
            "id": "e.1", "thread_id": "t.1", "sender_id": "person.tpm",
            "to": ["person.alex"], "cc": [],
            "body": "the launch is ready", "sim_time": 3000,
        }],
        "email_threads": [{"id": "t.1", "subject": "launch readiness"}],
        "tasks": [], "docs": [], "people": [],
    }
    (tmp_path / "world_final.json").write_text(json.dumps(world))
    artifact_yaml = {
        "id": "fake_artifact", "description": "test",
        "locator": {
            "kind": "email_thread",
            "thread_subject_contains": "launch",
            "sender_id": "person.tpm",
            "recipient_id": "person.alex",
        },
        "rubric": ["concrete date is named"],
    }
    failing_judge = StubJudge(
        lambda sp, up: RubricVerdict(
            score=0.0, rationale="simulated timeout", failed=True,
        )
    )
    score = score_artifact(artifact_yaml, tmp_path, {}, failing_judge)
    assert score.found is True
    assert score.failed is True
    assert score.failure_reason == "simulated timeout"
    assert score.pass_rate == 0.0


def test_decision_hygiene_recovered_from_artifacts_when_axes_judge_fails(tmp_path):
    """Axes judge fails but artifact judge succeeds → decision_hygiene takes
    artifact_mean as its score rather than being dropped."""
    import yaml
    rd = _drive_smoke(tmp_path, [])
    # Inject an artifact-bearing email into the world snapshot and an artifact
    # definition into the run's eval.yaml so the artifact path runs.
    world_path = rd / "world_final.json"
    world = json.loads(world_path.read_text())
    world.setdefault("email_threads", []).append({
        "id": "t.fake", "subject": "launch readiness"
    })
    world.setdefault("emails", []).append({
        "id": "e.fake", "thread_id": "t.fake", "sender_id": "person.tpm",
        "to": ["person.tpm"], "cc": [],
        "body": "ready to ship Wednesday", "sim_time": 100,
    })
    world_path.write_text(json.dumps(world))

    eval_path = rd / "scenario" / "eval.yaml"
    existing = yaml.safe_load(eval_path.read_text()) or {}
    existing.setdefault("artifacts", []).append({
        "id": "test_artifact",
        "description": "test artifact",
        "locator": {
            "kind": "email_thread",
            "thread_subject_contains": "launch",
            "sender_id": "person.tpm",
            "recipient_id": "person.tpm",
        },
        "rubric": ["item one", "item two", "item three"],
    })
    eval_path.write_text(yaml.safe_dump(existing))

    result = grade_and_write(rd, judge=_axes_failing_artifacts_ok_judge())
    assert result.judge.axes["decision_hygiene"].failed is False
    assert "artifact_mean" in result.judge.axes["decision_hygiene"].rationale
    assert result.judge.axes["specificity"].failed is True
    assert result.judge.axes["risk_escalation"].failed is True


def test_missing_axis_in_judge_response_marked_failed(tmp_path):
    """Judge succeeds overall but omits an axis → that axis is marked failed."""
    def fn(sp, up):
        if "rubric" in sp:
            return RubricVerdict(
                score=0.33,
                raw={"items": [{"pass": True}, {"pass": False}]},
            )
        if "state_accuracy" in sp:
            return RubricVerdict(
                score=0.5,
                raw={"state_accuracy": {"score": 0.5, "rationale": "ok"}},
            )
        # Deliberately omit risk_escalation
        return RubricVerdict(score=0.4, raw={
            "specificity":      {"score": 0.5, "rationale": "ok"},
            "decision_hygiene": {"score": 0.4, "rationale": "ok"},
        })
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=StubJudge(fn))
    assert result.judge.axes["risk_escalation"].failed is True
    assert result.judge.axes["specificity"].failed is False
    assert result.judge.axes["decision_hygiene"].failed is False
    assert any("risk_escalation" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Phase 2: judge axes payload includes inbound + outbound (both sides)
# ---------------------------------------------------------------------------


def test_axes_payload_includes_inbound_dms_and_mentions(tmp_path):
    """Inbound chats are DMs to the agent OR channel messages mentioning the
    agent. Outbound chats are the agent's sends. Non-addressed channel chatter
    is excluded so the judge isn't drowned in irrelevant context."""
    from sim.evaluator.final import _build_axes_payload
    world = {
        "agent_id": "person.tpm",
        "messages": [
            {"sender_id": "person.tpm", "channel_id": "channel.eng",
             "body": "my outbound post", "sim_time": 100, "mentions": []},
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "Maya DM to me about migration", "sim_time": 50,
             "mentions": []},
            {"sender_id": "person.kai", "channel_id": "channel.eng",
             "body": "@tpm please review the spec", "sim_time": 60,
             "mentions": ["person.tpm"]},
            {"sender_id": "person.sam", "channel_id": "channel.eng",
             "body": "general chatter not addressed to me", "sim_time": 70,
             "mentions": []},
        ],
        "channels": [
            {"id": "channel.eng", "is_dm": False,
             "members": ["person.tpm", "person.kai", "person.sam"]},
            {"id": "dm.maya__tpm", "is_dm": True,
             "members": ["person.tpm", "person.maya"]},
        ],
        "emails": [
            {"sender_id": "person.tpm", "to": ["person.alex"], "cc": [],
             "body": "outbound", "sim_time": 200},
            {"sender_id": "person.bigcorp", "to": ["person.tpm"], "cc": [],
             "body": "URGENT escalation", "sim_time": 150},
            {"sender_id": "person.alex", "to": ["person.dani"],
             "cc": ["person.tpm"], "body": "agent is cc'd", "sim_time": 160},
            {"sender_id": "person.alex", "to": ["person.dani"], "cc": [],
             "body": "agent not on this thread", "sim_time": 170},
        ],
        "tasks": [],
        "email_threads": [],
        "docs": [],
        "people": [],
    }
    (tmp_path / "world_final.json").write_text(json.dumps(world))
    (tmp_path / "turns.jsonl").write_text("")
    payload = _build_axes_payload(world, tmp_path)

    inbound_chats = [m["body"] for m in payload["agent_inbound_chat_excerpts"]]
    assert "Maya DM to me about migration" in inbound_chats   # DM hit
    assert "@tpm please review the spec" in inbound_chats     # mention hit
    assert "general chatter not addressed to me" not in inbound_chats
    assert "my outbound post" not in inbound_chats

    outbound_chats = [m["body"] for m in payload["agent_outbound_chat_excerpts"]]
    assert "my outbound post" in outbound_chats

    inbound_emails = [e["body"] for e in payload["agent_inbound_email_excerpts"]]
    assert "URGENT escalation" in inbound_emails               # to: agent
    assert "agent is cc'd" in inbound_emails                   # cc: agent
    assert "agent not on this thread" not in inbound_emails
    assert "outbound" not in inbound_emails


# ---------------------------------------------------------------------------
# prioritization_latency — continuous score on urgent-inbound-to-ack latency
# ---------------------------------------------------------------------------


def _write_prio_world(tmp_path, *, messages=None, emails=None, threads=None,
                      channels=None, final_sim_time=7200):
    world = {
        "agent_id": "person.tpm",
        "messages": messages or [],
        "emails": emails or [],
        "email_threads": threads or [],
        "channels": channels or [],
        "tasks": [],
    }
    (tmp_path / "world_final.json").write_text(json.dumps(world))
    (tmp_path / "run.json").write_text(json.dumps({"final_sim_time": final_sim_time}))
    (tmp_path / "turns.jsonl").write_text("")
    return tmp_path


_MAYA_DM_PRIO = {
    "objectives": [{
        "id": "maya_acked_fast",
        "check": {
            "kind": "urgent_ack_latency",
            "trigger": {
                "kind": "dm_received",
                "sender_id": "person.maya",
                "after_sim_time": 0,
                "body_contains_any": ["migration"],
            },
            "ack": {
                "kind": "agent_outbound_with_keywords",
                "to": ["person.maya", "person.kai"],
                "keywords_any": ["migration"],
            },
            "target_latency_minutes": 240,
        },
    }]
}


def test_prioritization_fast_ack_scores_positive(tmp_path):
    """Trigger at t=120, ack 60min later (1/4 of target) → score ≈ +0.75."""
    from sim.evaluator.metrics import prioritization_latency
    rd = _write_prio_world(
        tmp_path,
        channels=[
            {"id": "dm.maya__tpm", "is_dm": True,
             "members": ["person.tpm", "person.maya"]},
            {"id": "dm.kai__tpm", "is_dm": True,
             "members": ["person.tpm", "person.kai"]},
        ],
        messages=[
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "the migration is flaky", "sim_time": 120, "mentions": []},
            {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
             "body": "Kai — Maya flagged migration trouble. Look?",
             "sim_time": 180, "mentions": []},
        ],
    )
    result = prioritization_latency(rd, _MAYA_DM_PRIO)
    assert result.contributes is True
    assert 0.70 <= result.normalized <= 0.80
    item = result.detail["items"][0]
    assert item["status"] == "acked"
    assert item["latency_minutes"] == 60


def test_prioritization_no_ack_scores_minus_one(tmp_path):
    from sim.evaluator.metrics import prioritization_latency
    rd = _write_prio_world(
        tmp_path,
        channels=[{"id": "dm.maya__tpm", "is_dm": True,
                   "members": ["person.tpm", "person.maya"]}],
        messages=[
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "the migration is flaky", "sim_time": 120, "mentions": []},
        ],
    )
    result = prioritization_latency(rd, _MAYA_DM_PRIO)
    assert result.normalized == -1.0
    assert result.detail["items"][0]["status"] == "no_ack"


def test_prioritization_trigger_not_fired_excluded(tmp_path):
    """If the trigger inbound never arrived, this item shouldn't drag the score."""
    from sim.evaluator.metrics import prioritization_latency
    rd = _write_prio_world(tmp_path)  # empty world
    result = prioritization_latency(rd, _MAYA_DM_PRIO)
    assert result.contributes is False
    assert result.detail["reason"] == "no_triggers_fired"


def test_prioritization_late_ack_scores_negative(tmp_path):
    """Ack at 1.5× target latency → score = 1 - 1.5 = -0.5."""
    from sim.evaluator.metrics import prioritization_latency
    rd = _write_prio_world(
        tmp_path,
        channels=[
            {"id": "dm.maya__tpm", "is_dm": True,
             "members": ["person.tpm", "person.maya"]},
            {"id": "dm.kai__tpm", "is_dm": True,
             "members": ["person.tpm", "person.kai"]},
        ],
        messages=[
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "the migration is flaky", "sim_time": 100, "mentions": []},
            {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
             "body": "Kai — migration?", "sim_time": 100 + 360, "mentions": []},
        ],
    )
    result = prioritization_latency(rd, _MAYA_DM_PRIO)
    assert -0.55 <= result.normalized <= -0.45


def test_prioritization_email_reply_latency(tmp_path):
    """Email-reply-to-sender ack: BigCorp URGENT thread, agent replies 30min later
    against a 120-min target → score = 1 - 0.25 = +0.75."""
    from sim.evaluator.metrics import prioritization_latency
    objectives = {"objectives": [{
        "id": "bigcorp_fast",
        "check": {
            "kind": "urgent_ack_latency",
            "trigger": {
                "kind": "email_received",
                "sender_id": "person.bigcorp",
                "subject_contains": "URGENT",
            },
            "ack": {
                "kind": "email_reply_to_sender",
                "thread_subject_contains": "URGENT",
            },
            "target_latency_minutes": 120,
        },
    }]}
    threads = [{"id": "thread.bc", "subject": "URGENT — pipeline failures"}]
    emails = [
        {"id": "email.1", "thread_id": "thread.bc", "sender_id": "person.bigcorp",
         "to": ["person.tpm"], "cc": [], "body": "outage", "sim_time": 1500},
        {"id": "email.2", "thread_id": "thread.bc", "sender_id": "person.tpm",
         "to": ["person.bigcorp"], "cc": [], "body": "acking",
         "sim_time": 1530},
    ]
    rd = _write_prio_world(tmp_path, emails=emails, threads=threads)
    result = prioritization_latency(rd, objectives)
    assert 0.70 <= result.normalized <= 0.80
    item = result.detail["items"][0]
    assert item["latency_minutes"] == 30


def test_prioritization_metric_not_declared_does_not_contribute(tmp_path):
    """Scenario with no urgent_ack_latency objectives → metric excluded from composite."""
    from sim.evaluator.metrics import prioritization_latency
    rd = _write_prio_world(tmp_path)
    result = prioritization_latency(rd, {"objectives": []})
    assert result.contributes is False


# ---------------------------------------------------------------------------
# Eval Phase 2 — tick-aware tweaks to prioritization_latency + error_rate
# ---------------------------------------------------------------------------


def test_prioritization_within_tick_floor_scores_plus_one(tmp_path):
    """Latency strictly less than tick_size_minutes → tick floor → +1."""
    from sim.evaluator.metrics import prioritization_latency
    # Default tick = 15. Trigger at t=120, ack at t=130 → latency=10 < 15.
    rd = _write_prio_world(
        tmp_path,
        channels=[
            {"id": "dm.maya__tpm", "is_dm": True,
             "members": ["person.tpm", "person.maya"]},
            {"id": "dm.kai__tpm", "is_dm": True,
             "members": ["person.tpm", "person.kai"]},
        ],
        messages=[
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "the migration is flaky", "sim_time": 120, "mentions": []},
            {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
             "body": "Kai — migration?", "sim_time": 130, "mentions": []},
        ],
    )
    eval_truth = dict(_MAYA_DM_PRIO)
    eval_truth["tick_size_minutes"] = 15
    result = prioritization_latency(rd, eval_truth)
    assert result.normalized == 1.0
    item = result.detail["items"][0]
    assert item["latency_minutes"] == 10
    assert item["within_tick_floor"] is True
    assert item["score"] == 1.0


def test_prioritization_floor_respects_configured_tick_size(tmp_path):
    """A larger tick_size_minutes pushes the floor higher (e.g., 30 → latency
    20 is within-floor)."""
    from sim.evaluator.metrics import prioritization_latency
    rd = _write_prio_world(
        tmp_path,
        channels=[
            {"id": "dm.maya__tpm", "is_dm": True,
             "members": ["person.tpm", "person.maya"]},
            {"id": "dm.kai__tpm", "is_dm": True,
             "members": ["person.tpm", "person.kai"]},
        ],
        messages=[
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "the migration is flaky", "sim_time": 100, "mentions": []},
            {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
             "body": "migration?", "sim_time": 120, "mentions": []},
        ],
    )
    eval_truth = dict(_MAYA_DM_PRIO)
    eval_truth["tick_size_minutes"] = 30
    result = prioritization_latency(rd, eval_truth)
    assert result.normalized == 1.0
    assert result.detail["items"][0]["within_tick_floor"] is True


def test_prioritization_above_tick_scores_normally(tmp_path):
    """Latency >= tick_size_minutes → standard linear formula applies."""
    from sim.evaluator.metrics import prioritization_latency
    # Default tick=15. Trigger t=100, ack t=160 → latency=60 against target 240.
    rd = _write_prio_world(
        tmp_path,
        channels=[
            {"id": "dm.maya__tpm", "is_dm": True,
             "members": ["person.tpm", "person.maya"]},
            {"id": "dm.kai__tpm", "is_dm": True,
             "members": ["person.tpm", "person.kai"]},
        ],
        messages=[
            {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
             "body": "the migration is flaky", "sim_time": 100, "mentions": []},
            {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
             "body": "migration?", "sim_time": 160, "mentions": []},
        ],
    )
    eval_truth = dict(_MAYA_DM_PRIO)
    eval_truth["tick_size_minutes"] = 15
    result = prioritization_latency(rd, eval_truth)
    assert 0.70 <= result.normalized <= 0.80   # 1 - 60/240 = 0.75
    assert result.detail["items"][0]["within_tick_floor"] is False


def test_error_rate_reports_errors_per_tick_with_tick_ids(tmp_path):
    """When turns.jsonl carries tick_id, errors_per_tick reflects distinct ticks."""
    from sim.evaluator.metrics import error_rate
    turns = [
        # tick 0 — 3 turns, 1 error
        _ok_turn(0, "chat.read", {}) | {"tick_id": 0},
        _failed_turn(1, "chat.send", {}) | {"tick_id": 0},
        _ok_turn(2, "tasks.list", {}) | {"tick_id": 0},
        # tick 1 — 1 turn, 1 error
        _failed_turn(3, "chat.send", {}) | {"tick_id": 1},
    ]
    rd = _write_turns_from_list(tmp_path, turns)
    result = error_rate(rd)
    assert result.detail["errors"] == 2
    assert result.detail["total"] == 4
    assert result.detail["total_ticks"] == 2
    assert result.detail["errors_per_tick"] == 1.0   # 2 errors / 2 ticks


def test_error_rate_reports_none_when_tick_id_missing(tmp_path):
    """tick_id missing from every turn → errors_per_tick stays None."""
    from sim.evaluator.metrics import error_rate
    turns = [
        _ok_turn(0, "chat.read", {}),
        _failed_turn(1, "chat.send", {}),
    ]
    rd = _write_turns_from_list(tmp_path, turns)
    result = error_rate(rd)
    assert result.detail["errors_per_tick"] is None
    assert result.detail["total_ticks"] is None
    # Existing turn-based formula unchanged: 0.5 error rate → clipped at -1.0.
    assert result.raw == 0.5
    assert result.normalized == -1.0


def test_evaluate_run_injects_tick_size_into_eval_truth(tmp_path):
    """The final evaluator should inject scenario.tick_size_minutes so that
    metrics like prioritization_latency apply the tick floor consistently.

    We patch the scenario.yaml inside the run dir to set tick_size_minutes=30
    and an urgent_ack_latency objective with a 25-min latency. Without the
    injection the score would be ~0.875 (1 - 25/200); with the injection
    and a 30-min tick floor, the score should be exactly +1.0.
    """
    import yaml
    rd = _drive_smoke(tmp_path, [])
    # Patch scenario.yaml: bump tick_size_minutes to 30.
    scenario_path = rd / "scenario" / "scenario.yaml"
    scen = yaml.safe_load(scenario_path.read_text())
    scen["tick_size_minutes"] = 30
    scenario_path.write_text(yaml.safe_dump(scen))
    # Inject an urgent objective with latency 25 min (< 30 → tick floor).
    eval_path = rd / "scenario" / "eval.yaml"
    eval_data = yaml.safe_load(eval_path.read_text()) or {}
    eval_data.setdefault("objectives", []).append({
        "id": "fake_urgent", "description": "tick floor probe",
        "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["foo"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": [], "keywords_any": ["foo"]},
            "target_latency_minutes": 200,
        },
    })
    eval_path.write_text(yaml.safe_dump(eval_data))
    # Patch world to add the trigger and a fast ack.
    world_path = rd / "world_final.json"
    world = json.loads(world_path.read_text())
    world.setdefault("channels", []).append({
        "id": "dm.maya__tpm", "is_dm": True,
        "members": ["person.tpm", "person.maya"],
    })
    world.setdefault("messages", []).extend([
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "foo", "sim_time": 100, "mentions": []},
        {"sender_id": "person.tpm", "channel_id": "dm.maya__tpm",
         "body": "ack foo", "sim_time": 125, "mentions": []},
    ])
    world_path.write_text(json.dumps(world))

    result = evaluate_run(rd, judge=_stable_judge())
    prio = next(m for m in result.metrics if m.name == "prioritization_latency")
    # tick_size_minutes=30 was injected → latency=25 < 30 → tick floor → +1.
    assert prio.normalized == 1.0
    assert prio.detail["items"][0]["within_tick_floor"] is True


# ---------------------------------------------------------------------------
# Eval Phase 3 — six new programmatic metrics
# ---------------------------------------------------------------------------


def _write_run_dir(tmp_path, *, world=None, turns=None, run_meta=None):
    """Compose a run directory from arbitrary world / turns / run.json pieces."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if world is not None:
        (tmp_path / "world_final.json").write_text(json.dumps(world))
    else:
        (tmp_path / "world_final.json").write_text(json.dumps(_empty_world()))
    if turns is not None:
        (tmp_path / "turns.jsonl").write_text(
            "\n".join(json.dumps(t) for t in turns)
        )
    else:
        (tmp_path / "turns.jsonl").write_text("")
    (tmp_path / "run.json").write_text(json.dumps(run_meta or {"final_sim_time": 7200}))
    return tmp_path


# ----- B1. hidden_fact_discovery_rate ---------------------------------------


def test_hidden_fact_discovery_rate_read_and_referenced(tmp_path):
    """Agent reads Maya's DM, then references 'migration' in an outbound to Kai → +1."""
    from sim.evaluator.metrics import hidden_fact_discovery_rate
    world = _empty_world()
    world["channels"] = [
        {"id": "dm.maya__tpm", "is_dm": True,
         "members": ["person.tpm", "person.maya"]},
        {"id": "dm.kai__tpm", "is_dm": True,
         "members": ["person.tpm", "person.kai"]},
    ]
    # Agent's outbound to Kai contains the keyword "migration"
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
         "body": "Kai — Maya said migration is flaky", "sim_time": 200,
         "mentions": []},
    ]
    turns = [{
        "tool": "chat.read", "args": {"channel_id": "dm.maya__tpm"},
        "ok": True, "sim_time_before": 100, "sim_time_after": 101,
        "result": {"channel_id": "dm.maya__tpm", "messages": [{
            "sender_id": "person.maya",
            "body": "the migration on events table keeps failing",
        }]},
    }]
    eval_truth = {"hidden_facts": [{
        "id": "maya_migration_dm",
        "description": "Maya DM about migration",
        "source_locator": {
            "kind": "dm", "sender_id": "person.maya",
            "keywords_any": ["migration", "events table"],
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = hidden_fact_discovery_rate(rd, eval_truth)
    assert result.contributes is True
    assert result.normalized == 1.0
    assert result.detail["items"][0]["status"] == "read_and_referenced"


def test_hidden_fact_discovery_rate_read_but_not_referenced(tmp_path):
    """Agent reads the DM but never references the keyword in any outbound → 0."""
    from sim.evaluator.metrics import hidden_fact_discovery_rate
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    turns = [{
        "tool": "chat.read", "args": {"channel_id": "dm.maya__tpm"},
        "ok": True, "sim_time_before": 100, "sim_time_after": 101,
        "result": {"channel_id": "dm.maya__tpm", "messages": [{
            "sender_id": "person.maya",
            "body": "the migration on events table is flaky",
        }]},
    }]
    eval_truth = {"hidden_facts": [{
        "id": "maya_migration_dm",
        "description": "Maya DM about migration",
        "source_locator": {
            "kind": "dm", "sender_id": "person.maya",
            "keywords_any": ["migration"],
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = hidden_fact_discovery_rate(rd, eval_truth)
    assert result.normalized == 0.0
    assert result.detail["items"][0]["status"] == "read_only"


def test_hidden_fact_discovery_rate_not_discovered(tmp_path):
    """Agent never reads the source artifact → -1."""
    from sim.evaluator.metrics import hidden_fact_discovery_rate
    eval_truth = {"hidden_facts": [{
        "id": "maya_migration_dm",
        "description": "Maya DM about migration",
        "source_locator": {
            "kind": "dm", "sender_id": "person.maya",
            "keywords_any": ["migration"],
        },
    }]}
    rd = _write_run_dir(tmp_path)
    result = hidden_fact_discovery_rate(rd, eval_truth)
    assert result.normalized == -1.0
    assert result.detail["items"][0]["status"] == "not_discovered"


def test_hidden_fact_discovery_rate_not_declared(tmp_path):
    """No hidden_facts in eval.yaml → contributes=False."""
    from sim.evaluator.metrics import hidden_fact_discovery_rate
    rd = _write_run_dir(tmp_path)
    result = hidden_fact_discovery_rate(rd, {})
    assert result.contributes is False
    assert result.detail.get("reason") == "no_hidden_facts"


def test_hidden_fact_discovery_rate_doc_conflict_locator(tmp_path):
    """doc_conflict locator: reading either listed doc counts as discovery."""
    from sim.evaluator.metrics import hidden_fact_discovery_rate
    world = _empty_world()
    world["docs"] = [
        {"id": "doc.audit_prd", "versions": []},
        {"id": "doc.audit_eng_spec", "versions": []},
    ]
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "channel.eng",
         "body": "we need ISO-8601 on the API and localised display",
         "sim_time": 200, "mentions": []},
    ]
    world["channels"] = [{"id": "channel.eng", "is_dm": False,
                          "members": ["person.tpm"]}]
    turns = [{
        "tool": "docs.read", "args": {"doc_id": "doc.audit_prd"},
        "ok": True, "sim_time_before": 100, "sim_time_after": 105,
        "result": {"id": "doc.audit_prd", "body": "localised dates ..."},
    }]
    eval_truth = {"hidden_facts": [{
        "id": "date_field_conflict_in_docs",
        "description": "PRD vs eng spec disagree on date format",
        "source_locator": {
            "kind": "doc_conflict",
            "docs": ["doc.audit_prd", "doc.audit_eng_spec"],
            "keywords_any": ["ISO-8601", "localised"],
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = hidden_fact_discovery_rate(rd, eval_truth)
    # Read the PRD; outbound contains "iso-8601" and "localised" → +1.
    assert result.normalized == 1.0


# ----- B2. follow_up_rate ---------------------------------------------------


def test_follow_up_rate_replied(tmp_path):
    """Agent DMs Kai; Kai replies in-window → +1."""
    from sim.evaluator.metrics import follow_up_rate
    world = _empty_world()
    world["channels"] = [{"id": "dm.kai__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.kai"]}]
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
         "body": "kai any thoughts on migration?", "sim_time": 100,
         "mentions": []},
        {"sender_id": "person.kai", "channel_id": "dm.kai__tpm",
         "body": "yeah looking now", "sim_time": 200, "mentions": []},
    ]
    eval_truth = {"follow_up_targets": [{
        "id": "kai_migration", "npc_id": "person.kai",
        "target_minutes": 240, "keywords_any": ["migration"],
    }]}
    rd = _write_run_dir(tmp_path, world=world)
    result = follow_up_rate(rd, eval_truth)
    assert result.contributes is True
    assert result.normalized == 1.0
    assert result.detail["items"][0]["status"] == "replied"


def test_follow_up_rate_followed_up(tmp_path):
    """NPC doesn't reply within window but agent re-pings about same topic → +1."""
    from sim.evaluator.metrics import follow_up_rate
    world = _empty_world()
    world["channels"] = [{"id": "dm.kai__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.kai"]}]
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
         "body": "kai any thoughts on migration?", "sim_time": 100,
         "mentions": []},
        # No reply from Kai in window (window = 60 min → deadline 160).
        {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
         "body": "ping on the migration question", "sim_time": 200,
         "mentions": []},
    ]
    eval_truth = {"follow_up_targets": [{
        "id": "kai_migration", "npc_id": "person.kai",
        "target_minutes": 60, "keywords_any": ["migration"],
    }]}
    rd = _write_run_dir(tmp_path, world=world)
    result = follow_up_rate(rd, eval_truth)
    assert result.normalized == 1.0
    assert result.detail["items"][0]["status"] == "followed_up"


def test_follow_up_rate_dropped(tmp_path):
    """NPC doesn't reply; agent never follows up → -1."""
    from sim.evaluator.metrics import follow_up_rate
    world = _empty_world()
    world["channels"] = [{"id": "dm.kai__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.kai"]}]
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
         "body": "kai migration question", "sim_time": 100, "mentions": []},
    ]
    eval_truth = {"follow_up_targets": [{
        "id": "kai_migration", "npc_id": "person.kai",
        "target_minutes": 60, "keywords_any": ["migration"],
    }]}
    rd = _write_run_dir(tmp_path, world=world)
    result = follow_up_rate(rd, eval_truth)
    assert result.normalized == -1.0
    assert result.detail["items"][0]["status"] == "dropped"


def test_follow_up_rate_not_declared(tmp_path):
    """No follow_up_targets → contributes=False."""
    from sim.evaluator.metrics import follow_up_rate
    rd = _write_run_dir(tmp_path)
    result = follow_up_rate(rd, {})
    assert result.contributes is False


def test_follow_up_rate_default_window(tmp_path):
    """Omitting target_minutes uses the 240-min default."""
    from sim.evaluator.metrics import follow_up_rate
    world = _empty_world()
    world["channels"] = [{"id": "dm.kai__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.kai"]}]
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "dm.kai__tpm",
         "body": "kai migration?", "sim_time": 100, "mentions": []},
        # Kai replies at 200 — under the 240-min default → +1.
        {"sender_id": "person.kai", "channel_id": "dm.kai__tpm",
         "body": "thinking", "sim_time": 200, "mentions": []},
    ]
    eval_truth = {"follow_up_targets": [{
        "id": "kai", "npc_id": "person.kai",
        "keywords_any": ["migration"],
    }]}
    rd = _write_run_dir(tmp_path, world=world)
    result = follow_up_rate(rd, eval_truth)
    assert result.normalized == 1.0
    assert result.detail["items"][0]["deadline_sim_time"] == 100 + 240


# ----- B3. opportunity_cost_score -------------------------------------------


def test_opportunity_cost_acked_in_window(tmp_path):
    """One of the next-3 non-read calls is a chat.send to the right recipient
    with the right keyword → +1."""
    from sim.evaluator.metrics import opportunity_cost_score
    world = _empty_world()
    world["channels"] = [
        {"id": "dm.maya__tpm", "is_dm": True,
         "members": ["person.tpm", "person.maya"]},
    ]
    world["messages"] = [
        # The trigger — Maya DM at t=120.
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 120, "mentions": []},
    ]
    turns = [
        # Read first (doesn't count) and then a chat.dm with "migration" to Kai.
        {"tool": "chat.read", "args": {"channel_id": "dm.maya__tpm"},
         "ok": True, "sim_time_before": 121, "sim_time_after": 122, "result": {}},
        {"tool": "chat.dm", "args": {"recipient_id": "person.kai",
                                       "body": "Kai — migration?"},
         "ok": True, "sim_time_before": 122, "sim_time_after": 123, "result": {}},
        {"tool": "tasks.update_status",
         "args": {"task_id": "task.x", "status": "Done"},
         "ok": True, "sim_time_before": 123, "sim_time_after": 124, "result": {}},
    ]
    eval_truth = {"objectives": [{
        "id": "maya_acked", "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["migration"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": ["person.kai"], "keywords_any": ["migration"]},
            "target_latency_minutes": 240,
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = opportunity_cost_score(rd, eval_truth)
    assert result.contributes is True
    assert result.normalized == 1.0
    assert result.detail["items"][0]["status"] == "acked_in_window"


def test_opportunity_cost_lost(tmp_path):
    """Agent does 3 unrelated non-read actions after the trigger → -1."""
    from sim.evaluator.metrics import opportunity_cost_score
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 120, "mentions": []},
    ]
    turns = [
        {"tool": "tasks.update_status",
         "args": {"task_id": "task.x", "status": "In Progress"},
         "ok": True, "sim_time_before": 130, "sim_time_after": 131, "result": {}},
        {"tool": "tasks.log_work", "args": {"task_id": "task.x", "seconds": 60},
         "ok": True, "sim_time_before": 131, "sim_time_after": 132, "result": {}},
        {"tool": "tasks.update_status",
         "args": {"task_id": "task.y", "status": "Done"},
         "ok": True, "sim_time_before": 132, "sim_time_after": 133, "result": {}},
    ]
    eval_truth = {"objectives": [{
        "id": "maya_acked", "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["migration"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": ["person.kai"], "keywords_any": ["migration"]},
            "target_latency_minutes": 240,
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = opportunity_cost_score(rd, eval_truth)
    assert result.normalized == -1.0
    assert result.detail["items"][0]["status"] == "opportunity_lost"


def test_opportunity_cost_reads_dont_count(tmp_path):
    """Even with 3 reads in a row, the lookahead skips them and looks at the
    first non-read call."""
    from sim.evaluator.metrics import opportunity_cost_score
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 120, "mentions": []},
    ]
    turns = [
        # Five reads, then a chat.dm that ACKs.
        {"tool": "chat.read", "args": {}, "ok": True,
         "sim_time_before": 121, "sim_time_after": 122, "result": {}},
        {"tool": "email.list", "args": {}, "ok": True,
         "sim_time_before": 122, "sim_time_after": 123, "result": {}},
        {"tool": "docs.list", "args": {}, "ok": True,
         "sim_time_before": 123, "sim_time_after": 124, "result": {}},
        {"tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 124, "sim_time_after": 125, "result": {}},
        {"tool": "chat.dm", "args": {"recipient_id": "person.kai",
                                       "body": "migration look?"},
         "ok": True, "sim_time_before": 125, "sim_time_after": 126, "result": {}},
    ]
    eval_truth = {"objectives": [{
        "id": "maya_acked", "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["migration"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": ["person.kai"], "keywords_any": ["migration"]},
            "target_latency_minutes": 240,
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = opportunity_cost_score(rd, eval_truth)
    assert result.normalized == 1.0
    assert "chat.read" not in result.detail["items"][0]["evaluated_calls"]


def test_opportunity_cost_not_declared(tmp_path):
    """No urgent_ack_latency → contributes=False."""
    from sim.evaluator.metrics import opportunity_cost_score
    rd = _write_run_dir(tmp_path)
    result = opportunity_cost_score(rd, {"objectives": []})
    assert result.contributes is False


# ----- B4. appropriate_abandonments_rate ------------------------------------


def test_appropriate_abandon_with_trigger(tmp_path):
    """Agent abandons during the event window AND an urgent trigger fires
    inside that window → +1."""
    from sim.evaluator.metrics import appropriate_abandonments_rate
    world = _empty_world()
    world["calendar"] = [{
        "id": "cal.review", "title": "Review",
        "start_sim_time": 100, "end_sim_time": 200,
        "attendees": ["person.tpm"], "attended_by_agent": True,
    }]
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 150, "mentions": []},
    ]
    turns = [{
        "tool": "abandon.current", "args": {}, "ok": True,
        "sim_time_before": 160, "sim_time_after": 162, "result": {"abandoned": True},
    }]
    eval_truth = {
        "appropriate_abandonments": [{
            "id": "abandon_review_for_maya", "during_event": "cal.review",
            "abandon_triggered_by": "urgent_dm_from_maya",
        }],
        "objectives": [{
            "id": "maya_ack", "check": {
                "kind": "urgent_ack_latency",
                "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                            "body_contains_any": ["migration"]},
                "ack": {"kind": "agent_outbound_with_keywords",
                        "to": ["person.kai"], "keywords_any": ["migration"]},
                "target_latency_minutes": 60,
            },
        }],
    }
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = appropriate_abandonments_rate(rd, eval_truth)
    assert result.contributes is True
    assert result.normalized == 1.0
    assert result.detail["items"][0]["status"] == "abandoned_with_trigger"


def test_appropriate_abandon_false_positive(tmp_path):
    """Agent abandons but no urgent trigger present → +0.5."""
    from sim.evaluator.metrics import appropriate_abandonments_rate
    world = _empty_world()
    world["calendar"] = [{
        "id": "cal.review", "start_sim_time": 100, "end_sim_time": 200,
    }]
    turns = [{
        "tool": "abandon.current", "args": {}, "ok": True,
        "sim_time_before": 160, "sim_time_after": 162, "result": {},
    }]
    eval_truth = {
        "appropriate_abandonments": [{
            "id": "abandon_review", "during_event": "cal.review",
            "abandon_triggered_by": "urgent",
        }],
        "objectives": [],
    }
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = appropriate_abandonments_rate(rd, eval_truth)
    assert abs(result.normalized - 0.5) < 1e-9
    assert result.detail["items"][0]["status"] == "false_positive_abandon"


def test_appropriate_abandon_missed(tmp_path):
    """Trigger arrives during event window; agent didn't abandon → -1."""
    from sim.evaluator.metrics import appropriate_abandonments_rate
    world = _empty_world()
    world["calendar"] = [{
        "id": "cal.review", "start_sim_time": 100, "end_sim_time": 200,
    }]
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 150, "mentions": []},
    ]
    eval_truth = {
        "appropriate_abandonments": [{
            "id": "abandon_review", "during_event": "cal.review",
        }],
        "objectives": [{
            "id": "maya_ack", "check": {
                "kind": "urgent_ack_latency",
                "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                            "body_contains_any": ["migration"]},
                "ack": {"kind": "agent_outbound_with_keywords",
                        "to": ["person.kai"], "keywords_any": ["migration"]},
                "target_latency_minutes": 60,
            },
        }],
    }
    rd = _write_run_dir(tmp_path, world=world, turns=[])
    result = appropriate_abandonments_rate(rd, eval_truth)
    assert result.normalized == -1.0
    assert result.detail["items"][0]["status"] == "missed_abandon"


def test_appropriate_abandon_not_declared(tmp_path):
    """No appropriate_abandonments → contributes=False."""
    from sim.evaluator.metrics import appropriate_abandonments_rate
    rd = _write_run_dir(tmp_path)
    result = appropriate_abandonments_rate(rd, {})
    assert result.contributes is False


# ----- B5. bad_timing_starts ------------------------------------------------


def test_bad_timing_starts_zero_violations_scores_plus_one(tmp_path):
    """No long actions overlap with any urgent trigger → +1."""
    from sim.evaluator.metrics import bad_timing_starts
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 100, "mentions": []},
    ]
    # Long action starts well AFTER the urgent trigger.
    turns = [{
        "tool": "docs.create", "args": {}, "ok": True,
        "sim_time_before": 500, "sim_time_after": 540, "cost_minutes": 40,
    }]
    eval_truth = {"objectives": [{
        "id": "maya_ack", "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["migration"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": ["person.kai"], "keywords_any": ["migration"]},
            "target_latency_minutes": 60,
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = bad_timing_starts(rd, eval_truth)
    assert result.contributes is True
    assert result.normalized == 1.0
    assert result.detail["count"] == 0


def test_bad_timing_starts_one_violation_scores_partial(tmp_path):
    """One long action that swallowed an urgent trigger → score < 1.0."""
    from sim.evaluator.metrics import bad_timing_starts
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        # Urgent trigger lands at t=150, while the agent was busy on docs.create.
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 150, "mentions": []},
    ]
    turns = [{
        "tool": "docs.create", "args": {}, "ok": True,
        "sim_time_before": 100, "sim_time_after": 200, "cost_minutes": 100,
    }]
    eval_truth = {"objectives": [{
        "id": "maya_ack", "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["migration"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": ["person.kai"], "keywords_any": ["migration"]},
            "target_latency_minutes": 60,
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = bad_timing_starts(rd, eval_truth)
    # 1 violation → 1 - 2/3 ≈ 0.333.
    assert 0.30 <= result.normalized <= 0.40
    assert result.detail["count"] == 1


def test_bad_timing_starts_abandon_recovers(tmp_path):
    """If the agent abandons during the long action window, it's not a
    bad-timing violation."""
    from sim.evaluator.metrics import bad_timing_starts
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 150, "mentions": []},
    ]
    turns = [
        {"tool": "docs.create", "args": {}, "ok": True,
         "sim_time_before": 100, "sim_time_after": 200, "cost_minutes": 100},
        {"tool": "abandon.current", "args": {}, "ok": True,
         "sim_time_before": 160, "sim_time_after": 162, "cost_minutes": 2},
    ]
    eval_truth = {"objectives": [{
        "id": "maya_ack", "check": {
            "kind": "urgent_ack_latency",
            "trigger": {"kind": "dm_received", "sender_id": "person.maya",
                        "body_contains_any": ["migration"]},
            "ack": {"kind": "agent_outbound_with_keywords",
                    "to": ["person.kai"], "keywords_any": ["migration"]},
            "target_latency_minutes": 60,
        },
    }]}
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = bad_timing_starts(rd, eval_truth)
    assert result.normalized == 1.0
    assert result.detail["count"] == 0


def test_bad_timing_starts_not_declared(tmp_path):
    """No urgent_ack_latency → contributes=False."""
    from sim.evaluator.metrics import bad_timing_starts
    rd = _write_run_dir(tmp_path)
    result = bad_timing_starts(rd, {"objectives": []})
    assert result.contributes is False


# ----- B6. idle_judgment_score ----------------------------------------------


def test_idle_judgment_reasonable_short_idle_no_urgent(tmp_path):
    """Empty inbox, short idle (< 120) → reasonable, +1."""
    from sim.evaluator.metrics import idle_judgment_score
    turns = [{
        "tool": "idle.until",
        "args": {"target_sim_time": 130},
        "ok": True, "sim_time_before": 100, "sim_time_after": 101,
    }]
    rd = _write_run_dir(tmp_path, turns=turns)
    result = idle_judgment_score(rd, {})
    assert result.contributes is True
    assert result.normalized == 1.0


def test_idle_judgment_too_long_with_urgent(tmp_path):
    """Inbox has unread urgent inbound; agent idles 60 min (>= 30) → -1."""
    from sim.evaluator.metrics import idle_judgment_score
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    # Maya DMs the agent at t=50 — unread urgent inbound.
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is flaky", "sim_time": 50, "mentions": []},
    ]
    turns = [{
        "tool": "idle.until",
        "args": {"target_sim_time": 160},
        "ok": True, "sim_time_before": 100, "sim_time_after": 101,
    }]
    rd = _write_run_dir(tmp_path, world=world, turns=turns)
    result = idle_judgment_score(rd, {})
    assert result.normalized == -1.0
    item = result.detail["items"][0]
    assert item["had_urgent_inbound"] is True
    assert item["threshold_min"] == 30


def test_idle_judgment_too_long_no_urgent(tmp_path):
    """Empty inbox, long idle (>= 120) → -1."""
    from sim.evaluator.metrics import idle_judgment_score
    turns = [{
        "tool": "idle.until",
        "args": {"target_sim_time": 250},
        "ok": True, "sim_time_before": 100, "sim_time_after": 101,
    }]
    rd = _write_run_dir(tmp_path, turns=turns)
    result = idle_judgment_score(rd, {})
    assert result.normalized == -1.0
    assert result.detail["items"][0]["threshold_min"] == 120


def test_idle_judgment_no_calls_does_not_contribute(tmp_path):
    """No idle.until calls in the run → contributes=False (no signal)."""
    from sim.evaluator.metrics import idle_judgment_score
    rd = _write_run_dir(tmp_path, turns=[
        _ok_turn(0, "chat.read", {}),
    ])
    result = idle_judgment_score(rd, {})
    assert result.contributes is False
    assert result.detail.get("reason") == "no_idle_calls"


# ---------------------------------------------------------------------------
# Wiring: evaluate_run reports all 6 new metrics
# ---------------------------------------------------------------------------


def test_evaluate_run_includes_new_metrics(tmp_path):
    """The 6 Phase-3 metrics should appear in the scorecard alongside existing
    ones, even when their signals aren't declared (they're listed with
    contributes=False)."""
    rd = _drive_smoke(tmp_path, [])
    result = evaluate_run(rd, judge=_stable_judge())
    metric_names = {m.name for m in result.metrics}
    for required in (
        "hidden_fact_discovery_rate", "follow_up_rate",
        "opportunity_cost_score", "appropriate_abandonments_rate",
        "bad_timing_starts", "idle_judgment_score",
    ):
        assert required in metric_names, f"{required} missing from scorecard"


# ---------------------------------------------------------------------------
# Eval Phase 4 — three new LLM-judged axes
# ---------------------------------------------------------------------------


def _write_run_dir_with_scenario(
    tmp_path, *, world=None, turns=None, events=None,
    eval_truth=None, scenario_cfg=None, run_meta=None,
):
    """Compose a fully-formed run directory including scenario YAMLs so
    `evaluate_run` (which loads them off disk) is happy. Mirrors the layout
    that the run logger writes."""
    import yaml
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "world_final.json").write_text(json.dumps(world or _empty_world()))
    (tmp_path / "turns.jsonl").write_text(
        "\n".join(json.dumps(t) for t in (turns or []))
    )
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in (events or []))
    )
    (tmp_path / "run.json").write_text(json.dumps(run_meta or {
        "scenario_id": "phase4_probe", "seed": 0, "end_sim_time": 7200,
        "agent_id": "person.tpm", "status": "completed",
    }))
    scenario_dir = tmp_path / "scenario"
    scenario_dir.mkdir(exist_ok=True)
    (scenario_dir / "eval.yaml").write_text(yaml.safe_dump(eval_truth or {}))
    (scenario_dir / "scenario.yaml").write_text(yaml.safe_dump(scenario_cfg or {
        "id": "phase4_probe", "seed": 0, "agent_id": "person.tpm",
        "start": {"weekday": 0, "time": "09:00"},
        "end_sim_time": 7200, "tick_size_minutes": 15,
    }))
    return tmp_path


def _phase4_axes_judge(*, axis_responses=None, fail_on_axis=None):
    """A stub judge that returns deterministic verdicts for ALL judge axes.

    `axis_responses` overrides the per-axis raw payload for the three new
    Phase-4 axes (key by axis_name). Missing axes fall through to a default.

    `fail_on_axis`: if set to an axis_name (or list of names), return
    failed=True when that axis's prompt is invoked.
    """
    fail_set = set()
    if isinstance(fail_on_axis, str):
        fail_set.add(fail_on_axis)
    elif fail_on_axis:
        fail_set.update(fail_on_axis)
    axis_responses = axis_responses or {}

    def fn(system_prompt: str, user_payload: str) -> RubricVerdict:
        # Per-artifact rubrics keep the existing path.
        if "rubric" in system_prompt:
            return RubricVerdict(
                score=0.33,
                raw={"items": [{"pass": True}, {"pass": True}, {"pass": False}]},
            )
        if "state_accuracy" in system_prompt:
            return RubricVerdict(
                score=0.5,
                raw={"state_accuracy": {"score": 0.5, "rationale": "ok"}},
            )
        # Identify the new Phase-4 axes by a unique string in each prompt.
        for axis_name, marker in (
            ("pre_emption_judgment", "pre_emption_judgment"),
            ("tick_continuation_judgment", "tick_continuation_judgment"),
            ("context_recall_score", "context_recall_score"),
        ):
            if marker in system_prompt:
                if axis_name in fail_set:
                    return RubricVerdict(
                        score=0.0, rationale="simulated phase4 axis failure",
                        failed=True,
                    )
                response = axis_responses.get(axis_name, {
                    "score": 0.5, "rationale": "ok",
                })
                return RubricVerdict(score=float(response["score"]), raw={
                    axis_name: response,
                })
        # The legacy three-axis prompt.
        return RubricVerdict(
            score=0.4,
            raw={
                "specificity":      {"score": 0.5, "rationale": "ok"},
                "decision_hygiene": {"score": 0.4, "rationale": "ok"},
                "risk_escalation":  {"score": 0.3, "rationale": "ok"},
            },
        )
    return StubJudge(fn)


# ----- Axis 1: pre_emption_judgment -----------------------------------------


def _abandon_run_dir(tmp_path):
    """Build a run with one abandon.current call sandwiched between a long
    in-flight action (docs.create) and a follow-up DM."""
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        # Inbound DM lands ~10 sim-min before the abandon.
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "the migration is on fire — need eyes now",
         "sim_time": 150, "mentions": []},
    ]
    turns = [
        # Long in-flight action (sim_time advances by cost).
        {"turn": 0, "tool": "docs.create",
         "args": {"title": "Q3 plan"},
         "ok": True, "sim_time_before": 100, "sim_time_after": 160,
         "cost_minutes": 60},
        # Abandon mid-stream.
        {"turn": 1, "tool": "abandon.current", "args": {}, "ok": True,
         "sim_time_before": 160, "sim_time_after": 162, "cost_minutes": 2},
        # Productive follow-up: DM Kai about migration.
        {"turn": 2, "tool": "chat.dm",
         "args": {"recipient_id": "person.kai",
                   "body": "Maya flagged migration urgency"},
         "ok": True, "sim_time_before": 162, "sim_time_after": 163,
         "cost_minutes": 1},
    ]
    return _write_run_dir_with_scenario(tmp_path, world=world, turns=turns)


def test_pre_emption_judgment_happy_path(tmp_path):
    """Run has an abandon; judge stub returns +1 → axis lands in scorecard."""
    rd = _abandon_run_dir(tmp_path)
    judge = _phase4_axes_judge(axis_responses={
        "pre_emption_judgment": {"score": 0.9, "rationale": "well justified"},
    })
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["pre_emption_judgment"]
    assert axis.failed is False
    assert axis.not_applicable is False
    assert abs(axis.score - 0.9) < 1e-9
    assert "justified" in axis.rationale


def test_pre_emption_judgment_failure_excluded_from_composite(tmp_path):
    """Judge fails on pre_emption_judgment → axis is failed=True; composite
    is computed from remaining inputs only."""
    rd = _abandon_run_dir(tmp_path)
    judge = _phase4_axes_judge(fail_on_axis="pre_emption_judgment")
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["pre_emption_judgment"]
    assert axis.failed is True
    assert any("pre_emption_judgment" in e for e in result.errors)
    # When excluded, it doesn't contribute to the composite mean.
    contributing = [
        a.score for n, a in result.judge.axes.items()
        if not a.failed and not a.not_applicable
    ]
    assert axis.score not in contributing or axis.score == 0.0  # excluded


def test_pre_emption_judgment_not_applicable_when_no_abandonments(tmp_path):
    """A run with zero abandon.current calls → axis is not_applicable."""
    rd = _write_run_dir_with_scenario(tmp_path, turns=[
        {"turn": 0, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 0, "sim_time_after": 1, "cost_minutes": 1},
    ])
    judge = _phase4_axes_judge()
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["pre_emption_judgment"]
    assert axis.not_applicable is True
    assert axis.failed is False
    assert "no abandonments" in axis.rationale


def test_pre_emption_judgment_payload_isolation(tmp_path):
    """The judge payload for pre_emption_judgment must not contain briefing
    text or agent reasoning prose."""
    captured: list[tuple[str, str]] = []

    def capturing(system_prompt, user_payload):
        captured.append((system_prompt, user_payload))
        return RubricVerdict(score=0.0, raw={
            "items": [{"pass": False}],
            "specificity":      {"score": 0, "rationale": ""},
            "decision_hygiene": {"score": 0, "rationale": ""},
            "risk_escalation":  {"score": 0, "rationale": ""},
            "state_accuracy":   {"score": 0, "rationale": ""},
            "pre_emption_judgment": {"score": 0, "rationale": "",
                                      "per_event": []},
            "tick_continuation_judgment": {"score": 0, "rationale": "",
                                            "per_event": []},
            "context_recall_score": {"score": 0, "rationale": "",
                                      "per_claim": []},
        })

    rd = _abandon_run_dir(tmp_path)
    grade_and_write(rd, judge=StubJudge(capturing))
    pre_payloads = [up for sp, up in captured
                    if "pre_emption_judgment" in sp]
    assert pre_payloads, "judge was not called for pre_emption_judgment"
    for payload in pre_payloads:
        assert "# Briefing" not in payload
        assert "BRIEFING" not in payload.upper() or "# Briefing" not in payload
        # The abandon payload should include the inbound that prompted it.
        assert "migration" in payload  # Maya's DM body should make it in


# ----- Axis 2: tick_continuation_judgment -----------------------------------


def _continuation_run_dir(tmp_path):
    """Build a run with one tick-continuation group: chat.read → chat.send
    in the same tick (sim_time_before of #2 == sim_time_after of #1)."""
    world = _empty_world()
    world["channels"] = [{"id": "channel.eng", "is_dm": False,
                          "members": ["person.tpm", "person.kai"]}]
    turns = [
        # First decision in tick.
        {"turn": 0, "tool": "chat.read",
         "args": {"channel_id": "channel.eng"}, "ok": True,
         "sim_time_before": 15, "sim_time_after": 16, "cost_minutes": 1},
        # Second decision in SAME tick (sim_time_before == prev sim_time_after).
        {"turn": 1, "tool": "chat.send",
         "args": {"channel_id": "channel.eng", "body": "ack"},
         "ok": True, "sim_time_before": 16, "sim_time_after": 17,
         "cost_minutes": 1},
        # A new tick (gap to 30) — single decision, NOT a continuation group.
        {"turn": 2, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 30, "sim_time_after": 31, "cost_minutes": 1},
    ]
    return _write_run_dir_with_scenario(tmp_path, world=world, turns=turns)


def test_tick_continuation_judgment_happy_path(tmp_path):
    """One tick continuation present; judge returns +0.8 → axis scored."""
    rd = _continuation_run_dir(tmp_path)
    judge = _phase4_axes_judge(axis_responses={
        "tick_continuation_judgment": {"score": 0.8,
                                         "rationale": "read then write"},
    })
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["tick_continuation_judgment"]
    assert axis.failed is False
    assert axis.not_applicable is False
    assert abs(axis.score - 0.8) < 1e-9


def test_tick_continuation_judgment_failure_excluded_from_composite(tmp_path):
    """Judge fails → axis failed=True, listed in errors, excluded."""
    rd = _continuation_run_dir(tmp_path)
    judge = _phase4_axes_judge(fail_on_axis="tick_continuation_judgment")
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["tick_continuation_judgment"]
    assert axis.failed is True
    assert any("tick_continuation_judgment" in e for e in result.errors)


def test_tick_continuation_judgment_not_applicable_when_no_chains(tmp_path):
    """Run has no consecutive same-tick decisions → axis is not_applicable."""
    turns = [
        {"turn": 0, "tool": "chat.read", "args": {}, "ok": True,
         "sim_time_before": 15, "sim_time_after": 16, "cost_minutes": 1},
        # Gap to 30 — next tick, no continuation.
        {"turn": 1, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 30, "sim_time_after": 31, "cost_minutes": 1},
    ]
    rd = _write_run_dir_with_scenario(tmp_path, turns=turns)
    judge = _phase4_axes_judge()
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["tick_continuation_judgment"]
    assert axis.not_applicable is True
    assert axis.failed is False
    assert "no tick continuations" in axis.rationale


def test_tick_continuation_judgment_payload_isolation(tmp_path):
    """Payload for tick_continuation_judgment must not contain briefing
    text or reasoning prose. It should contain tool names."""
    captured: list[tuple[str, str]] = []

    def capturing(system_prompt, user_payload):
        captured.append((system_prompt, user_payload))
        return RubricVerdict(score=0.0, raw={
            "items": [{"pass": False}],
            "specificity":      {"score": 0, "rationale": ""},
            "decision_hygiene": {"score": 0, "rationale": ""},
            "risk_escalation":  {"score": 0, "rationale": ""},
            "state_accuracy":   {"score": 0, "rationale": ""},
            "pre_emption_judgment": {"score": 0, "rationale": "",
                                      "per_event": []},
            "tick_continuation_judgment": {"score": 0, "rationale": "",
                                            "per_event": []},
            "context_recall_score": {"score": 0, "rationale": "",
                                      "per_claim": []},
        })

    rd = _continuation_run_dir(tmp_path)
    grade_and_write(rd, judge=StubJudge(capturing))
    tick_payloads = [up for sp, up in captured
                     if "tick_continuation_judgment" in sp]
    assert tick_payloads, "judge was not called for tick_continuation_judgment"
    for payload in tick_payloads:
        assert "# Briefing" not in payload
        assert "chat.read" in payload  # the chained tools should appear


# ----- Axis 3: context_recall_score -----------------------------------------


def _claim_run_dir(tmp_path):
    """Build a run where the agent sends a chat that references a task id
    that's in the final world snapshot."""
    world = _empty_world()
    world["tasks"] = [{"id": "task.MIG-1", "status": "Done",
                       "assignee_id": "person.tpm"}]
    world["channels"] = [{"id": "channel.eng", "is_dm": False,
                          "members": ["person.tpm", "person.kai"]}]
    world["messages"] = [
        # Agent outbound CLAIM referencing the task id.
        {"sender_id": "person.tpm", "channel_id": "channel.eng",
         "body": "task.MIG-1 is now Done — closing it out",
         "sim_time": 1500, "mentions": []},
    ]
    # An event log entry for the status change before the claim.
    events = [
        {"kind": "task_status_changed",
         "payload": {"task_id": "task.MIG-1", "status": "Done",
                     "sim_time": 1400}},
    ]
    turns = [
        # Agent read the tasks before claiming, but at a time when status
        # was already Done.
        {"turn": 0, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 1450, "sim_time_after": 1451, "cost_minutes": 1},
        {"turn": 1, "tool": "chat.send",
         "args": {"channel_id": "channel.eng",
                   "body": "task.MIG-1 is now Done — closing it out"},
         "ok": True, "sim_time_before": 1500, "sim_time_after": 1501,
         "cost_minutes": 1},
    ]
    return _write_run_dir_with_scenario(
        tmp_path, world=world, turns=turns, events=events,
    )


def test_context_recall_score_happy_path(tmp_path):
    """Run has an outbound claim referencing a known task id → judge scored."""
    rd = _claim_run_dir(tmp_path)
    judge = _phase4_axes_judge(axis_responses={
        "context_recall_score": {"score": 0.6, "rationale": "fresh claim"},
    })
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["context_recall_score"]
    assert axis.failed is False
    assert axis.not_applicable is False
    assert abs(axis.score - 0.6) < 1e-9


def test_context_recall_score_failure_excluded_from_composite(tmp_path):
    """Judge fails on context_recall_score → axis failed=True, excluded."""
    rd = _claim_run_dir(tmp_path)
    judge = _phase4_axes_judge(fail_on_axis="context_recall_score")
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["context_recall_score"]
    assert axis.failed is True
    assert any("context_recall_score" in e for e in result.errors)


def test_context_recall_score_not_applicable_when_no_claims(tmp_path):
    """No agent outbound references any known task id → not_applicable."""
    world = _empty_world()
    world["tasks"] = [{"id": "task.X", "status": "Done"}]
    world["channels"] = [{"id": "channel.eng", "is_dm": False,
                          "members": ["person.tpm"]}]
    world["messages"] = [
        # Outbound doesn't reference any task id.
        {"sender_id": "person.tpm", "channel_id": "channel.eng",
         "body": "I am here", "sim_time": 100, "mentions": []},
    ]
    rd = _write_run_dir_with_scenario(tmp_path, world=world, turns=[])
    judge = _phase4_axes_judge()
    result = grade_and_write(rd, judge=judge)
    axis = result.judge.axes["context_recall_score"]
    assert axis.not_applicable is True
    assert axis.failed is False
    assert "no verifiable state claims" in axis.rationale


def test_context_recall_score_payload_isolation(tmp_path):
    """Payload for context_recall_score must not include briefing strings
    or agent reasoning, but should include the claim text and snapshot."""
    captured: list[tuple[str, str]] = []

    def capturing(system_prompt, user_payload):
        captured.append((system_prompt, user_payload))
        return RubricVerdict(score=0.0, raw={
            "items": [{"pass": False}],
            "specificity":      {"score": 0, "rationale": ""},
            "decision_hygiene": {"score": 0, "rationale": ""},
            "risk_escalation":  {"score": 0, "rationale": ""},
            "state_accuracy":   {"score": 0, "rationale": ""},
            "pre_emption_judgment": {"score": 0, "rationale": "",
                                      "per_event": []},
            "tick_continuation_judgment": {"score": 0, "rationale": "",
                                            "per_event": []},
            "context_recall_score": {"score": 0, "rationale": "",
                                      "per_claim": []},
        })

    rd = _claim_run_dir(tmp_path)
    grade_and_write(rd, judge=StubJudge(capturing))
    ctx_payloads = [up for sp, up in captured
                    if "context_recall_score" in sp]
    assert ctx_payloads, "judge was not called for context_recall_score"
    for payload in ctx_payloads:
        assert "# Briefing" not in payload
        assert "task.MIG-1" in payload  # the task id should appear in claim


# ----- not_applicable axes are excluded from the composite ------------------


def test_not_applicable_axis_excluded_from_composite(tmp_path):
    """A run with no abandons / no continuations / no claims yields three
    not_applicable axes — none of them should contribute to the composite."""
    # Use the smoke driver so the full evaluator (metrics + axes + artifacts)
    # runs end-to-end. The smoke scenario has no abandons, no continuations,
    # and no state claims that match a task id.
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_phase4_axes_judge())
    for axis_name in (
        "pre_emption_judgment",
        "tick_continuation_judgment",
        "context_recall_score",
    ):
        axis = result.judge.axes[axis_name]
        assert axis.not_applicable is True, (
            f"{axis_name} should be not_applicable in smoke run "
            f"(score={axis.score}, failed={axis.failed})"
        )
    # The composite was computed but the not_applicable axes were excluded.
    # Eval Phase 5: the composite is the mean of CLUSTER scores (cluster-
    # weighted), not a flat mean over individual metrics/axes. Sanity-check
    # by recomputing from the contributing clusters only.
    contributing_cluster_scores = [
        c.score for c in result.clusters.values() if c.contributes_to_composite
    ]
    if contributing_cluster_scores:
        expected_composite = (
            sum(contributing_cluster_scores) / len(contributing_cluster_scores)
        )
        assert abs(result.composite_score - expected_composite) < 1e-9
    # And the judge mean section also excludes not_applicable axes.
    contributing_axes = [
        a.score for a in result.judge.axes.values()
        if not a.failed and not a.not_applicable
    ]
    contributing_count = len(contributing_axes)
    if contributing_count:
        expected_mean = sum(contributing_axes) / contributing_count
        assert abs(result.judge.mean_score - expected_mean) < 1e-9


def test_mix_of_not_applicable_and_scored_axes(tmp_path):
    """A run where pre_emption is scored but the other two are not_applicable.
    Composite should include the scored axis but exclude the others."""
    # Construct a run with an abandon that is NOT chained to the previous
    # turn (sim_time_before of abandon != sim_time_after of prev), and no
    # other tick continuations or task-id claims.
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "migration urgent", "sim_time": 145, "mentions": []},
    ]
    turns = [
        # An earlier action, ending at sim_time=160.
        {"turn": 0, "tool": "docs.create", "args": {}, "ok": True,
         "sim_time_before": 100, "sim_time_after": 160, "cost_minutes": 60},
        # Abandon at sim_time=200 — NOT chained to turn 0 (160 != 200).
        {"turn": 1, "tool": "abandon.current", "args": {}, "ok": True,
         "sim_time_before": 200, "sim_time_after": 202, "cost_minutes": 2},
        # Follow-up at 220 — NOT chained to turn 1 (202 != 220).
        {"turn": 2, "tool": "chat.dm",
         "args": {"recipient_id": "person.kai",
                   "body": "Maya's migration concern"},
         "ok": True, "sim_time_before": 220, "sim_time_after": 221,
         "cost_minutes": 1},
    ]
    rd = _write_run_dir_with_scenario(tmp_path, world=world, turns=turns)
    judge = _phase4_axes_judge(axis_responses={
        "pre_emption_judgment": {"score": 1.0, "rationale": "perfect"},
    })
    result = grade_and_write(rd, judge=judge)
    # pre_emption_judgment scored.
    assert result.judge.axes["pre_emption_judgment"].not_applicable is False
    assert result.judge.axes["pre_emption_judgment"].failed is False
    assert abs(result.judge.axes["pre_emption_judgment"].score - 1.0) < 1e-9
    # The other two not_applicable (no continuations, no claims).
    assert result.judge.axes["tick_continuation_judgment"].not_applicable is True
    assert result.judge.axes["context_recall_score"].not_applicable is True
    # Composite still computed; pre_emption's +1 should pull it positive
    # relative to the legacy axes' 0.4–0.5 contribution.
    assert -1.0 <= result.composite_score <= 1.0


# ----- Direct unit tests on the payload builders ----------------------------


def test_build_pre_emption_payload_skips_runs_with_no_abandon(tmp_path):
    from sim.evaluator.final import _build_pre_emption_payload
    rd = _write_run_dir_with_scenario(tmp_path, turns=[
        {"turn": 0, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 0, "sim_time_after": 1, "cost_minutes": 1},
    ])
    payload = _build_pre_emption_payload(
        json.loads((rd / "world_final.json").read_text()), rd, {},
    )
    assert payload["events"] == []


def test_build_pre_emption_payload_captures_inbound_within_window(tmp_path):
    """The recent_inbound list should only include inbound that arrived
    in the ~30 sim-min window before the abandon."""
    from sim.evaluator.final import _build_pre_emption_payload
    world = _empty_world()
    world["channels"] = [{"id": "dm.maya__tpm", "is_dm": True,
                          "members": ["person.tpm", "person.maya"]}]
    world["messages"] = [
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "too old", "sim_time": 50, "mentions": []},   # too old
        {"sender_id": "person.maya", "channel_id": "dm.maya__tpm",
         "body": "fresh signal", "sim_time": 145, "mentions": []},  # in window
    ]
    turns = [
        {"turn": 0, "tool": "docs.create", "args": {}, "ok": True,
         "sim_time_before": 100, "sim_time_after": 160, "cost_minutes": 60},
        {"turn": 1, "tool": "abandon.current", "args": {}, "ok": True,
         "sim_time_before": 160, "sim_time_after": 162, "cost_minutes": 2},
    ]
    rd = _write_run_dir_with_scenario(tmp_path, world=world, turns=turns)
    payload = _build_pre_emption_payload(world, rd, {})
    assert len(payload["events"]) == 1
    inbound = payload["events"][0]["recent_inbound"]
    bodies = [x["body"] for x in inbound]
    assert "fresh signal" in bodies
    assert "too old" not in bodies


def test_build_tick_continuation_payload_groups_by_sim_time_equality(tmp_path):
    """Consecutive turns where turns[i].sim_time_before == turns[i-1].sim_time_after
    are grouped; single-decision ticks are not yielded."""
    from sim.evaluator.final import _build_tick_continuation_payload
    turns = [
        # Tick at t=15, with a chain of 3 actions.
        {"turn": 0, "tool": "chat.read", "args": {}, "ok": True,
         "sim_time_before": 15, "sim_time_after": 16, "cost_minutes": 1},
        {"turn": 1, "tool": "chat.read", "args": {}, "ok": True,
         "sim_time_before": 16, "sim_time_after": 17, "cost_minutes": 1},
        {"turn": 2, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 17, "sim_time_after": 18, "cost_minutes": 1},
        # New tick at t=30 — single decision, not a continuation.
        {"turn": 3, "tool": "chat.send",
         "args": {"channel_id": "c", "body": "hi"},
         "ok": True, "sim_time_before": 30, "sim_time_after": 31,
         "cost_minutes": 1},
        # Another tick at t=45 with two-action chain.
        {"turn": 4, "tool": "chat.read", "args": {}, "ok": True,
         "sim_time_before": 45, "sim_time_after": 46, "cost_minutes": 1},
        {"turn": 5, "tool": "chat.send",
         "args": {"channel_id": "c", "body": "ack"},
         "ok": True, "sim_time_before": 46, "sim_time_after": 47,
         "cost_minutes": 1},
    ]
    rd = _write_run_dir_with_scenario(tmp_path, turns=turns)
    payload = _build_tick_continuation_payload({}, rd, {})
    events = payload["events"]
    assert len(events) == 2
    # First chain: 3 calls all reads (none of them is a write).
    assert events[0]["tick_start_sim_time"] == 15
    assert len(events[0]["decisions_in_tick"]) == 3
    # tasks.list is a read in our heuristic set, so had_write_or_action is False.
    assert events[0]["had_write_or_action"] is False
    # Second chain: read + send → has a write.
    assert events[1]["tick_start_sim_time"] == 45
    assert events[1]["had_write_or_action"] is True


def test_build_context_recall_payload_resolves_status_history(tmp_path):
    """The verifiable_state_snapshot captures status_at_claim from the
    events log and final_state from the world snapshot."""
    from sim.evaluator.final import _build_context_recall_payload
    world = _empty_world()
    world["tasks"] = [{"id": "task.X", "status": "Done"}]
    world["channels"] = [{"id": "c.1", "is_dm": False,
                          "members": ["person.tpm"]}]
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "c.1",
         "body": "task.X is now in Review", "sim_time": 500, "mentions": []},
    ]
    events = [
        {"kind": "task_status_changed",
         "payload": {"task_id": "task.X", "status": "In Progress",
                     "sim_time": 100}},
        {"kind": "task_status_changed",
         "payload": {"task_id": "task.X", "status": "Review",
                     "sim_time": 400}},
        {"kind": "task_status_changed",
         "payload": {"task_id": "task.X", "status": "Done",
                     "sim_time": 600}},  # after the claim
    ]
    turns = [
        {"turn": 0, "tool": "tasks.list", "args": {}, "ok": True,
         "sim_time_before": 200, "sim_time_after": 201, "cost_minutes": 1},
    ]
    rd = _write_run_dir_with_scenario(
        tmp_path, world=world, turns=turns, events=events,
    )
    payload = _build_context_recall_payload(world, rd, {})
    assert len(payload["claims"]) == 1
    snap = payload["claims"][0]["verifiable_state_snapshot"]
    assert snap["task_id"] == "task.X"
    # Claim at t=500 → most recent status_changed is "Review" (at 400).
    assert snap["state_at_claim"] == "Review"
    # Last read at t=200 → most recent status_changed is "In Progress" (at 100).
    assert snap["state_at_last_read"] == "In Progress"
    assert snap["last_read_sim_time"] == 200
    # Final world state is Done (from the world snapshot).
    assert snap["final_state"] == "Done"


# ---------------------------------------------------------------------------
# Eval Phase 5 — skill cluster scorecard + cluster-weighted composite
# ---------------------------------------------------------------------------


def _phase5_axes_judge(*, axis_scores=None):
    """Stub judge that returns deterministic scores for every Phase-4 axis.

    Same shape as `_phase4_axes_judge` but parameterised by a flat dict of
    `{axis_name: score}`. Used by Phase 5 tests so we can prove cluster
    arithmetic with known inputs.
    """
    axis_scores = axis_scores or {}

    def fn(system_prompt: str, user_payload: str) -> RubricVerdict:
        if "rubric" in system_prompt:
            return RubricVerdict(
                score=0.0,
                raw={"items": [{"pass": True}, {"pass": True}, {"pass": True}]},
            )
        if "state_accuracy" in system_prompt:
            return RubricVerdict(
                score=axis_scores.get("state_accuracy", 0.5),
                raw={"state_accuracy": {
                    "score": axis_scores.get("state_accuracy", 0.5),
                    "rationale": "ok",
                }},
            )
        for axis_name in ("pre_emption_judgment", "tick_continuation_judgment",
                          "context_recall_score"):
            if axis_name in system_prompt:
                return RubricVerdict(
                    score=axis_scores.get(axis_name, 0.5),
                    raw={axis_name: {
                        "score": axis_scores.get(axis_name, 0.5),
                        "rationale": "ok",
                    }},
                )
        # The legacy three-axis prompt.
        return RubricVerdict(
            score=0.0,
            raw={
                "specificity":      {"score": axis_scores.get("specificity", 0.5),
                                      "rationale": "ok"},
                "decision_hygiene": {"score": axis_scores.get("decision_hygiene", 0.5),
                                      "rationale": "ok"},
                "risk_escalation":  {"score": axis_scores.get("risk_escalation", 0.5),
                                      "rationale": "ok"},
            },
        )
    return StubJudge(fn)


def test_clusters_populate_with_full_eval(tmp_path):
    """After `evaluate_run` on a full smoke run, all four clusters are in
    the output dict — even ones with zero contributing members."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_phase5_axes_judge())
    for cluster_name in ("outcomes_achieved", "decision_quality",
                         "coordination", "time_management"):
        assert cluster_name in result.clusters, (
            f"cluster {cluster_name} missing from scorecard"
        )
        cluster = result.clusters[cluster_name]
        assert cluster.name == cluster_name
        assert -1.0 <= cluster.score <= 1.0
        # Every cluster reports either included or excluded members (or both).
        assert cluster.member_count == (
            len(cluster.included_members) + len(cluster.excluded_members)
        )


def test_cluster_score_is_mean_of_members(tmp_path):
    """Per-cluster score is the arithmetic mean of its CONTRIBUTING members."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    # Pin every axis the smoke run can produce to a known score.
    result = grade_and_write(rd, judge=_phase5_axes_judge(axis_scores={
        "specificity": 0.6, "decision_hygiene": 0.4, "risk_escalation": 0.2,
        "state_accuracy": 0.0, "pre_emption_judgment": 0.8,
        "tick_continuation_judgment": -0.4, "context_recall_score": 1.0,
    }))
    metrics_by_name = {m.name: m for m in result.metrics}
    axes = result.judge.axes

    for cluster_name, cluster in result.clusters.items():
        if not cluster.contributes_to_composite:
            continue
        expected_values: list[float] = []
        for member in cluster.included_members:
            if member in metrics_by_name:
                expected_values.append(metrics_by_name[member].normalized)
            elif member in axes:
                expected_values.append(axes[member].score)
            elif member == "artifacts_mean":
                # No artifacts in the smoke scenario; if this ever lands here
                # it's a bug.
                raise AssertionError("artifacts_mean unexpected in smoke run")
            else:
                raise AssertionError(f"unknown member {member}")
        expected = sum(expected_values) / len(expected_values)
        assert abs(cluster.score - expected) < 1e-9, (
            f"cluster {cluster_name}: score={cluster.score}, "
            f"expected={expected} from members={cluster.included_members}"
        )


def test_composite_is_cluster_weighted(tmp_path):
    """Composite = mean of cluster scores, NOT mean of individual members.

    The smoke run produces clusters with very different member counts
    (decision_quality with ~4 members vs outcomes_achieved with 1). The
    composite must weight each cluster equally — so it equals the simple
    mean of cluster scores, never the member-count-weighted mean.
    """
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_phase5_axes_judge(axis_scores={
        "specificity": 0.5, "decision_hygiene": 0.5, "risk_escalation": 0.5,
        "state_accuracy": 0.5,
    }))
    contributing_scores = [
        c.score for c in result.clusters.values() if c.contributes_to_composite
    ]
    expected_composite = sum(contributing_scores) / len(contributing_scores)
    assert abs(result.composite_score - expected_composite) < 1e-9

    # And explicitly: the composite differs from a member-weighted flat mean
    # when clusters have unequal sizes. Build the flat mean to verify.
    flat_metric_vals = [m.normalized for m in result.metrics if m.contributes]
    flat_axis_vals = [a.score for a in result.judge.axes.values()
                       if not a.failed and not a.not_applicable]
    flat_mean_inputs = flat_metric_vals + flat_axis_vals
    # decision_quality has many more members than outcomes_achieved, so the
    # flat mean and the cluster-weighted mean should NOT coincide on smoke.
    if flat_mean_inputs:
        flat_mean = sum(flat_mean_inputs) / len(flat_mean_inputs)
        # If they happen to coincide on this particular run (very rare), the
        # property we care about (equal cluster weighting) still holds — but
        # we want a real test, so we configure the smoke run so the bucket
        # sizes diverge. decision_quality has at least 2 contributing axes;
        # outcomes_achieved has 1 (deadline_hit_rate). Sizes differ.
        assert abs(result.composite_score - flat_mean) > 1e-6, (
            "flat-mean and cluster-weighted composite happened to coincide; "
            "the test scenario should have unequal cluster sizes"
        )


def test_excluded_members_listed_separately(tmp_path):
    """A metric/axis that's reported but contributes=False (or failed /
    not_applicable) lands in excluded_members, NOT included_members, and
    does not affect the cluster score."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_phase5_axes_judge())
    # All three anti-hack metrics aren't declared in smoke eval.yaml so they
    # report contributes=False; they should appear in decision_quality's
    # excluded_members.
    decision = result.clusters["decision_quality"]
    for excluded in (
        "anti_hack_must_consult_before_decision",
        "anti_hack_forbidden_external_keywords",
        "anti_hack_forbidden_log_work",
    ):
        assert excluded in decision.excluded_members, (
            f"{excluded} should be in decision_quality.excluded_members"
        )
        assert excluded not in decision.included_members
    # The cluster score should match the mean of ONLY the included axes
    # (not folding in the excluded anti-hack metrics' raw values).
    included_axis_scores = [
        result.judge.axes[n].score for n in decision.included_members
        if n in result.judge.axes
    ]
    if included_axis_scores:
        expected = sum(included_axis_scores) / len(included_axis_scores)
        assert abs(decision.score - expected) < 1e-9


def test_cluster_with_no_contributing_members_skipped():
    """If every member of a cluster is excluded, that cluster's
    `contributes_to_composite=False` and the composite is computed from the
    remaining clusters only.

    Unit test on `_build_clusters` directly so we have full control over
    which metrics are present and what their `contributes` flag says — we
    don't depend on the quirks of any particular scenario's metrics.
    """
    from sim.evaluator.final import _build_clusters, JudgeAxis
    from sim.evaluator.metrics import MetricResult
    metrics = [
        # outcomes_achieved members — both excluded.
        MetricResult("deadline_hit_rate", "requires_full_run", 0.0, 0.0,
                     {"reason": "no_objectives"}, contributes=False),
        MetricResult("hidden_fact_discovery_rate", "slice_safe", 0.0, 0.0,
                     {"reason": "no_hidden_facts"}, contributes=False),
        # time_management — error_rate contributes.
        MetricResult("error_rate", "slice_safe", 0.5, 0.1,
                     {"errors": 1, "total": 10}, contributes=True),
    ]
    axes: dict[str, JudgeAxis] = {
        # decision_quality — one contributing axis.
        "specificity": JudgeAxis(score=0.4, rationale="ok"),
    }
    errors: list[str] = []
    clusters = _build_clusters(metrics, axes, scored_artifacts=[], errors=errors)
    # outcomes_achieved: both members in excluded_members → does not contribute.
    outcomes = clusters["outcomes_achieved"]
    assert outcomes.contributes_to_composite is False
    assert outcomes.score == 0.0
    assert set(outcomes.excluded_members) == {
        "deadline_hit_rate", "hidden_fact_discovery_rate"
    }
    # decision_quality contributes via the single axis.
    assert clusters["decision_quality"].contributes_to_composite is True
    # time_management contributes via error_rate.
    assert clusters["time_management"].contributes_to_composite is True
    # coordination has no metric present at all → 0 members → skipped.
    assert clusters["coordination"].contributes_to_composite is False
    assert clusters["coordination"].member_count == 0


def test_failed_axis_excluded_from_cluster(tmp_path):
    """A judge axis with `failed=True` appears in excluded_members and
    does not affect the cluster score."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_all_failing_judge())
    # All judge axes failed → every axis is excluded from its cluster.
    decision = result.clusters["decision_quality"]
    for axis_name in ("specificity", "decision_hygiene", "risk_escalation",
                       "state_accuracy"):
        # state_accuracy is only added in full-runs, but smoke IS full —
        # so we expect every axis listed by the cluster spec to land in
        # excluded_members when the judge fails.
        if axis_name in result.judge.axes:
            assert result.judge.axes[axis_name].failed is True
            assert axis_name in decision.excluded_members, (
                f"{axis_name} should be in excluded_members when failed"
            )
            assert axis_name not in decision.included_members


def test_not_applicable_axis_excluded_from_cluster(tmp_path):
    """A judge axis marked `not_applicable=True` (no signal in the run)
    appears in excluded_members and is not folded into the cluster score."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_phase5_axes_judge())
    # smoke produces zero abandons / continuations / state claims, so all
    # three Phase-4 axes land not_applicable in time_management.
    time_mgmt = result.clusters["time_management"]
    for axis_name in ("pre_emption_judgment", "tick_continuation_judgment",
                       "context_recall_score"):
        assert result.judge.axes[axis_name].not_applicable is True
        assert axis_name in time_mgmt.excluded_members
        assert axis_name not in time_mgmt.included_members


def test_artifact_mean_rolls_into_decision_quality(tmp_path):
    """Multiple per-artifact rubric scores collapse to a single
    `artifacts_mean` member of decision_quality. Different declared-artifact
    counts shouldn't shift the cluster's weight."""
    import yaml
    rd = _drive_smoke(tmp_path, [])
    # Inject two artifacts the locator can find — the same artifact body
    # serves both (we just need >1 successful artifact score to prove they
    # collapse into ONE synthetic member).
    world_path = rd / "world_final.json"
    world = json.loads(world_path.read_text())
    world.setdefault("email_threads", []).append({
        "id": "t.fake", "subject": "launch readiness"
    })
    world.setdefault("emails", []).append({
        "id": "e.fake", "thread_id": "t.fake", "sender_id": "person.tpm",
        "to": ["person.tpm"], "cc": [],
        "body": "ready to ship Wednesday", "sim_time": 100,
    })
    world_path.write_text(json.dumps(world))

    eval_path = rd / "scenario" / "eval.yaml"
    existing = yaml.safe_load(eval_path.read_text()) or {}
    locator = {
        "kind": "email_thread",
        "thread_subject_contains": "launch",
        "sender_id": "person.tpm",
        "recipient_id": "person.tpm",
    }
    existing.setdefault("artifacts", []).extend([
        {"id": "art_one", "description": "a", "locator": locator,
         "rubric": ["item 1", "item 2"]},
        {"id": "art_two", "description": "a", "locator": locator,
         "rubric": ["item 1", "item 2"]},
    ])
    eval_path.write_text(yaml.safe_dump(existing))

    result = grade_and_write(rd, judge=_stable_judge())
    # Two artifacts in the scorecard, but only ONE synthetic member in
    # decision_quality.included_members.
    assert len(result.artifacts) == 2
    decision = result.clusters["decision_quality"]
    assert "artifacts_mean" in decision.included_members
    # And it appears only once, not per-artifact.
    assert decision.included_members.count("artifacts_mean") == 1


def test_orphan_metric_logged_to_errors(monkeypatch, tmp_path):
    """A metric name not in any CLUSTERS bucket is logged to `errors[]`
    (but does not crash)."""
    from sim.evaluator import final as final_mod
    # Patch CLUSTERS so `error_rate` is no longer assigned to any cluster.
    new_clusters = {
        k: {**v, "metrics": [m for m in v["metrics"] if m != "error_rate"]}
        for k, v in final_mod.CLUSTERS.items()
    }
    monkeypatch.setattr(final_mod, "CLUSTERS", new_clusters)

    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    result = grade_and_write(rd, judge=_phase5_axes_judge())
    # `error_rate` produces a MetricResult every run; with the patched
    # CLUSTERS it's now an orphan → a warning lands in errors[].
    orphan_msgs = [e for e in result.errors if "error_rate" in e and "cluster" in e]
    assert orphan_msgs, (
        f"orphan metric should be logged to errors; got errors={result.errors}"
    )
    # And of course no cluster should claim it.
    for cluster in result.clusters.values():
        assert "error_rate" not in cluster.included_members
        assert "error_rate" not in cluster.excluded_members


def test_regrade_byte_identical_with_clusters(tmp_path):
    """Two consecutive `evaluate_run` calls on the same run dir produce
    byte-identical JSON, with the new `clusters` section present."""
    rd = _drive_smoke(tmp_path, [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ])
    judge = _phase5_axes_judge()
    a = grade_and_write(rd, judge=judge).model_dump_json(indent=2)
    b = evaluate_run(rd, judge=judge).model_dump_json(indent=2)
    assert a == b
    # Sanity: clusters appear in the output and are not empty.
    assert "\"clusters\"" in a
    for cluster_name in ("outcomes_achieved", "decision_quality",
                          "coordination", "time_management"):
        assert cluster_name in a


def test_artifacts_failed_do_not_appear_as_member(tmp_path):
    """If every declared artifact fails the judge, no synthetic
    `artifacts_mean` member is added to decision_quality."""
    import yaml
    rd = _drive_smoke(tmp_path, [])
    # Inject an artifact whose locator finds the body.
    world_path = rd / "world_final.json"
    world = json.loads(world_path.read_text())
    world.setdefault("email_threads", []).append({
        "id": "t.fake", "subject": "launch readiness"
    })
    world.setdefault("emails", []).append({
        "id": "e.fake", "thread_id": "t.fake", "sender_id": "person.tpm",
        "to": ["person.tpm"], "cc": [],
        "body": "ready", "sim_time": 100,
    })
    world_path.write_text(json.dumps(world))

    eval_path = rd / "scenario" / "eval.yaml"
    existing = yaml.safe_load(eval_path.read_text()) or {}
    existing.setdefault("artifacts", []).append({
        "id": "fail_art", "description": "",
        "locator": {"kind": "email_thread",
                     "thread_subject_contains": "launch",
                     "sender_id": "person.tpm",
                     "recipient_id": "person.tpm"},
        "rubric": ["item one"],
    })
    eval_path.write_text(yaml.safe_dump(existing))

    # Failing judge — the artifact rubric call fails, so scored_artifacts is
    # empty. The legacy axes/state_accuracy paths also fail; that's fine
    # for this test (we only care about whether `artifacts_mean` is added).
    result = grade_and_write(rd, judge=_all_failing_judge())
    decision = result.clusters["decision_quality"]
    assert "artifacts_mean" not in decision.included_members
    assert "artifacts_mean" not in decision.excluded_members
