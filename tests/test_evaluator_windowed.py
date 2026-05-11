from __future__ import annotations

import json
from pathlib import Path

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.evaluator import StubJudge, WindowedEvaluator
from sim.evaluator.judge import RubricVerdict
from sim.runtime import build_runtime
from sim.scenario import load_scenario
from sim.tools import ToolCall

SMOKE = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"


def _good_judge(_sys, payload):
    data = json.loads(payload)
    progress = sum(
        1 for t in data["turns"]
        if t["tool"].startswith("tasks.") and t["ok"]
    )
    score = min(1.0, max(-1.0, (progress - 3) / 7))
    return RubricVerdict(
        score=score,
        rationale="progress-driven heuristic",
        raw={"category": "progress" if score > 0 else "neutral"},
    )


def _spammy_judge(_sys, payload):
    data = json.loads(payload)
    chat_count = sum(1 for t in data["turns"] if t["tool"] == "chat.send")
    return RubricVerdict(
        score=-min(1.0, chat_count / 5),
        rationale="too much chat",
        raw={"category": "noise"},
    )


def test_window_size_default_10_no_verdict_before():
    judge = StubJudge(_good_judge)
    ev = WindowedEvaluator(judge, window_size=10)
    assert ev.latest_verdict() is None


def test_window_emits_verdict_after_n_turns():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    ev = WindowedEvaluator(StubJudge(_good_judge), window_size=3)
    calls = [
        ToolCall(tool="tasks.list", args={}),
        ToolCall(tool="tasks.get", args={"task_id": "task.SMOKE-1"}),
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "In Progress"}),
    ]
    agent = ScriptedAgent(calls)
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=3, end_sim_time=s.config.end_sim_time),
        turn_observer=ev.on_turn,
        feedback_provider=ev.latest_verdict,
    )
    driver.run()
    assert ev.latest_verdict() is not None
    assert len(ev.all_verdicts()) == 1


def test_verdict_is_passed_to_next_briefing():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    ev = WindowedEvaluator(StubJudge(_good_judge), window_size=2)
    captured: list = []
    class Recorder:
        def decide(self, briefing):
            captured.append(briefing.last_verdict)
            return ToolCall(tool="tasks.list", args={})
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, Recorder(), assembler,
        DriverConfig(max_turns=5, end_sim_time=s.config.end_sim_time),
        turn_observer=ev.on_turn,
        feedback_provider=ev.latest_verdict,
    )
    driver.run()
    # First two briefings have no verdict; turns 2,3,4 should see one.
    assert captured[0] is None
    assert captured[1] is None
    assert captured[2] is not None
    assert "score" in captured[2]


def test_spammy_agent_lands_negative_verdict():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    # Smoke has only one channel; add the agent to channel.general (it already is)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    ev = WindowedEvaluator(StubJudge(_spammy_judge), window_size=5)
    calls = [
        ToolCall(tool="chat.send", args={"channel_id": "channel.general", "body": f"noise {i}"})
        for i in range(5)
    ]
    agent = ScriptedAgent(calls)
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=5, end_sim_time=s.config.end_sim_time),
        turn_observer=ev.on_turn,
        feedback_provider=ev.latest_verdict,
    )
    driver.run()
    v = ev.latest_verdict()
    assert v is not None
    assert v["score"] <= -0.3


def test_judge_cache_byte_identical_on_repeat():
    judge = StubJudge(lambda s, p: RubricVerdict(score=0.5, rationale="ok"))
    ev = WindowedEvaluator(judge, window_size=2)
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    calls = [ToolCall(tool="tasks.list", args={}), ToolCall(tool="tasks.list", args={})]
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, ScriptedAgent(calls), assembler,
        DriverConfig(max_turns=2, end_sim_time=s.config.end_sim_time),
        turn_observer=ev.on_turn,
        feedback_provider=ev.latest_verdict,
    )
    driver.run()
    v1 = ev.all_verdicts()[0]
    # Now repeat with same window content via a fresh evaluator + judge cache.
    ev2 = WindowedEvaluator(judge, window_size=2)
    rt2 = build_runtime(load_scenario(SMOKE))
    driver2 = AgentDriver(
        rt2.world, rt2.scheduler, rt2.agent_registry,
        ScriptedAgent([ToolCall(tool="tasks.list", args={}), ToolCall(tool="tasks.list", args={})]),
        BriefingAssembler(rt2.world, end_sim_time=s.config.end_sim_time),
        DriverConfig(max_turns=2, end_sim_time=s.config.end_sim_time),
        turn_observer=ev2.on_turn,
        feedback_provider=ev2.latest_verdict,
    )
    driver2.run()
    v2 = ev2.all_verdicts()[0]
    assert v1.score == v2.score
