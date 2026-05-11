"""Golden-run regression tests.

Two reference agent behaviors against `smoke`:
  - GOLDEN_HIGH_SCORE  — methodical, advances the task to Done, light comms.
  - GOLDEN_LOW_SCORE   — spams messages, repeat-reads, never updates state.

Each lands inside a score band documented in `docs/grading.md`. Any change to
metrics, weights, or the rubric that moves these scores requires a deliberate
band update in this test — that's the contract the team agrees to.

These tests stub the judge so they don't require a live LLM. The scores are
fully deterministic.
"""

from __future__ import annotations

from pathlib import Path

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.evaluator.final import evaluate_run
from sim.evaluator.judge import RubricVerdict, StubJudge
from sim.logging import RunLogger
from sim.runtime import build_runtime
from sim.scenario import load_scenario
from sim.tools import ToolCall

SMOKE = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"


def _judge_balanced():
    """A judge that scores axes positively when outbound activity is light and
    intentional, negatively when outputs are pure noise."""
    def fn(system_prompt: str, user_payload: str) -> RubricVerdict:
        import json as _j
        if "rubric" in system_prompt:
            return RubricVerdict(score=0.0, raw={"items": []})
        if "state_accuracy" in system_prompt:
            return RubricVerdict(score=0.3, raw={"state_accuracy": {"score": 0.3, "rationale": "ok"}})
        # axes — penalise heavy chat traffic mentioned in the payload
        data = _j.loads(user_payload)
        chat_count = data.get("tool_counts", {}).get("chat.send", 0)
        if chat_count > 10:
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


def _drive(tmp_path, calls):
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
        DriverConfig(max_turns=200, end_sim_time=scenario.config.end_sim_time),
        turn_observer=logger.log_turn,
    )
    driver.run()
    logger.finalize(rt.world, scenario.config, SMOKE)
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
    rd = _drive(tmp_path, calls)
    result = evaluate_run(rd, judge=_judge_balanced())
    assert 0.30 <= result.composite_score <= 0.70, (
        f"high-score golden composite was {result.composite_score:+.3f}; expected [0.30, 0.70]"
    )


def test_golden_low_score_lands_in_negative_band(tmp_path):
    # Spam: chat.send as the *default* tool every turn — never waits, never
    # advances the task. Max turns caps the run.
    scenario = load_scenario(SMOKE)
    rt = build_runtime(scenario)
    assembler = BriefingAssembler(rt.world, end_sim_time=scenario.config.end_sim_time)
    logger = RunLogger(tmp_path)
    logger.wire(rt.world)
    agent = ScriptedAgent([], default=ToolCall(
        tool="chat.send", args={"channel_id": "channel.general", "body": "spam"},
    ))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=100, end_sim_time=scenario.config.end_sim_time),
        turn_observer=logger.log_turn,
    )
    driver.run()
    logger.finalize(rt.world, scenario.config, SMOKE)
    result = evaluate_run(tmp_path, judge=_judge_balanced())
    assert result.composite_score <= -0.20, (
        f"low-score golden composite was {result.composite_score:+.3f}; expected <= -0.20"
    )


def test_golden_runs_are_byte_stable_on_regrade(tmp_path):
    calls = [
        ToolCall(tool="tasks.update_status",
                 args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ]
    rd = _drive(tmp_path, calls)
    judge = _judge_balanced()
    a = evaluate_run(rd, judge=judge).model_dump_json(indent=2)
    b = evaluate_run(rd, judge=judge).model_dump_json(indent=2)
    assert a == b
