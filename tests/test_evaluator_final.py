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
    for required in ("error_rate", "turns_per_sim_hour", "repeat_read_rate", "anti_hack_max_messages"):
        assert required in metric_names


def test_full_run_engages_requires_full_run_tier(tmp_path):
    rd = _drive_smoke(tmp_path, [])  # agent waits straight through to end
    result = evaluate_run(rd, judge=_stable_judge())
    assert result.tier_used == "full"
    assert result.completed is True
    metric_names = {m.name for m in result.metrics}
    assert "deadline_hit_rate" in metric_names
    assert "stakeholder_contact_rate" in metric_names


def test_anti_hack_message_volume_penalises_spam(tmp_path):
    calls = [
        ToolCall(tool="chat.send", args={"channel_id": "channel.general", "body": f"spam {i}"})
        for i in range(50)
    ]
    rd = _drive_smoke(tmp_path, calls)
    result = evaluate_run(rd, judge=_stable_judge())
    anti = next(m for m in result.metrics if m.name == "anti_hack_max_messages")
    # Smoke scenario's default cap is 120, so 50 is fine — but a sensible
    # taper means we're below the +1.0 ceiling.
    assert -1.0 <= anti.normalized <= 1.0


def test_repeat_read_rate_penalises_repeated_reads(tmp_path):
    calls = [
        ToolCall(tool="tasks.get", args={"task_id": "task.SMOKE-1"}),
        ToolCall(tool="tasks.get", args={"task_id": "task.SMOKE-1"}),
        ToolCall(tool="tasks.get", args={"task_id": "task.SMOKE-1"}),
        ToolCall(tool="tasks.get", args={"task_id": "task.SMOKE-1"}),
    ]
    rd = _drive_smoke(tmp_path, calls)
    result = evaluate_run(rd, judge=_stable_judge())
    metric = next(m for m in result.metrics if m.name == "repeat_read_rate")
    assert metric.normalized < 1.0  # Reading the same task many times is penalised


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


def test_anti_hack_per_channel_volume_fires_on_violation(tmp_path):
    """30 messages to one channel on day 0 with cap=25 → 1 violating bucket."""
    from sim.evaluator.metrics import anti_hack_per_channel_volume
    world = _empty_world()
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "channel.spam",
         "body": f"msg {i}", "sim_time": i, "mentions": []}
        for i in range(30)
    ]
    eval_truth = {"anti_hack": [{
        "id": "per_chan", "description": "",
        "signal": {"kind": "max_messages_per_channel_per_day", "max": 25},
    }]}
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_per_channel_volume(rd, eval_truth)
    assert result.contributes is True
    assert result.raw == 1.0
    assert abs(result.normalized - 0.6) < 1e-9


def test_anti_hack_per_channel_volume_clean(tmp_path):
    """10 messages, cap=25 → no violation → contributes +1.0."""
    from sim.evaluator.metrics import anti_hack_per_channel_volume
    world = _empty_world()
    world["messages"] = [
        {"sender_id": "person.tpm", "channel_id": "channel.spam",
         "body": f"msg {i}", "sim_time": i, "mentions": []}
        for i in range(10)
    ]
    eval_truth = {"anti_hack": [{
        "id": "per_chan", "description": "",
        "signal": {"kind": "max_messages_per_channel_per_day", "max": 25},
    }]}
    rd = _write_test_run_dir(tmp_path, world=world)
    result = anti_hack_per_channel_volume(rd, eval_truth)
    assert result.contributes is True
    assert result.normalized == 1.0


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
    """Same forbidden phrase to an INTERNAL recipient is not penalized."""
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
    assert result.normalized == 1.0


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
    assert abs(result.normalized - 0.5) < 1e-9


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
    assert result.normalized == 1.0


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
    assert abs(result.normalized - 0.6) < 1e-9


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
    assert result.normalized == 1.0


def test_anti_hack_signals_not_declared_do_not_contribute(tmp_path):
    """No signal in eval.yaml → metric is reported but excluded from composite."""
    from sim.evaluator.metrics import (
        anti_hack_forbidden_external_keywords,
        anti_hack_forbidden_log_work,
        anti_hack_must_consult_before_decision,
        anti_hack_per_channel_volume,
    )
    rd = _write_test_run_dir(tmp_path, world=_empty_world(), turns=[])
    eval_truth: dict = {}
    for fn in (
        anti_hack_per_channel_volume,
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
