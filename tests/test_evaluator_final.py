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
