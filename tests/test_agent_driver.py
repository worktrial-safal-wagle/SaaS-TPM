from __future__ import annotations

from pathlib import Path

import pytest

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.agent.briefing import render_briefing
from sim.runtime import build_runtime
from sim.scenario import load_scenario
from sim.tools import ToolCall

SMOKE = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"


def test_scripted_agent_runs_through_smoke():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    calls = [
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "In Progress"}),
        ToolCall(tool="tasks.log_work", args={"task_id": "task.SMOKE-1", "seconds": 600}),
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ]
    agent = ScriptedAgent(calls)
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=20, end_sim_time=s.config.end_sim_time),
    )
    turns = driver.run()
    # After three scripted calls, default = wait.until(end). One more turn fires it.
    assert len(turns) >= 4
    assert rt.world.tasks["task.SMOKE-1"].status == "Done"
    assert rt.scheduler.sim_time >= s.config.end_sim_time


def test_briefing_includes_open_commitments_for_owned_task():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    task_commits = [c for c in b.open_commitments if c.kind == "task_owned"]
    assert any(c.detail["task_id"] == "task.SMOKE-1" for c in task_commits)


def test_briefing_renders_to_markdown():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    md = render_briefing(b)
    assert "# Briefing" in md
    assert "task.SMOKE-1" in md


def test_driver_stops_on_max_turns():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    # Script of trivial reads — never hits end_sim_time, so max_turns is the gate
    agent = ScriptedAgent([], default=ToolCall(tool="tasks.list", args={}))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=5, end_sim_time=s.config.end_sim_time),
    )
    turns = driver.run()
    assert len(turns) == 5


def test_driver_records_turn_observer():
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    observed = []
    agent = ScriptedAgent([ToolCall(tool="tasks.list", args={})])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=3, end_sim_time=s.config.end_sim_time),
        turn_observer=observed.append,
    )
    driver.run()
    assert len(observed) >= 1
    assert observed[0].result.ok


def test_reference_agent_uses_structured_tool_call():
    """The agent receives `(tool_name, tool_input)` from the SDK and returns
    a ToolCall — no JSON-from-prose parsing."""
    from sim.agent.reference_agent import ReferenceAgent

    class StubModelClient:
        def __init__(self):
            self.received_tools = None

        def call_with_tools(self, *, system, user_message, tools):
            self.received_tools = tools
            # Return SDK-form name with double underscores
            return "chat__send", {"channel_id": "c", "body": "hi"}

    client = StubModelClient()
    specs = [{"name": "chat__send", "description": "send", "input_schema": {}}]
    agent = ReferenceAgent(client=client, tool_specs=specs)

    from sim.agent.briefing import Briefing
    briefing = Briefing(
        now_sim_time=0, now_label="Mon 09:00", end_sim_time=480,
        agent_persona={"id": "person.tpm", "display_name": "TPM",
                       "role": "tpm", "persona_notes": "", "team": None},
        unread_notifications_count=0, unread_chats=[], unread_emails=[],
        open_commitments=[], task_deltas=[], upcoming_calendar=[],
    )
    call = agent.decide(briefing)
    # SDK form should be translated back to dot form
    assert call.tool == "chat.send"
    assert call.args == {"channel_id": "c", "body": "hi"}
    # Tool list was passed through
    assert client.received_tools == specs


def test_reference_agent_requires_tool_specs():
    from sim.agent.reference_agent import ReferenceAgent
    class _Client:
        def call_with_tools(self, **k): return "x", {}
    with pytest.raises(ValueError):
        ReferenceAgent(client=_Client())


def test_briefing_surfaces_stall_warning_after_repeated_free_reads():
    """If the agent keeps calling free tools, sim_time doesn't advance and
    the briefing's stall counter grows."""
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    captured: list[int] = []

    class Recorder:
        def decide(self, briefing):
            captured.append(briefing.sim_time_stalled_for_turns)
            return ToolCall(tool="tasks.list", args={})  # free read

    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, Recorder(), assembler,
        DriverConfig(max_turns=5, end_sim_time=s.config.end_sim_time),
    )
    driver.run()
    # Stall counter grows as the agent keeps reading without advancing time.
    assert captured == [0, 1, 2, 3, 4]
    # And by turn 4+ the rendered briefing contains the warning.
    last = assembler.build(
        now_sim_time=rt.scheduler.sim_time,
        last_turn_sim_time=rt.scheduler.sim_time,
        sim_time_stalled_for_turns=4,
    )
    assert "Stall warning" in render_briefing(last)


def test_stall_counter_resets_when_sim_time_advances():
    """A write-cost action resets the stall counter."""
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    captured: list[int] = []

    calls = [
        ToolCall(tool="tasks.list", args={}),        # free
        ToolCall(tool="tasks.list", args={}),        # free
        ToolCall(tool="tasks.update_status",         # 1 min — advances clock
                 args={"task_id": "task.SMOKE-1", "status": "In Progress"}),
        ToolCall(tool="tasks.list", args={}),        # free again
    ]
    class Recorder:
        def __init__(self): self.n = 0
        def decide(self, briefing):
            captured.append(briefing.sim_time_stalled_for_turns)
            c = calls[self.n]; self.n += 1; return c

    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, Recorder(), assembler,
        DriverConfig(max_turns=4, end_sim_time=s.config.end_sim_time),
    )
    driver.run()
    # 0 at start, 1 after first free read, 2 after second, then 0 after the
    # write action (sim_time advanced), then 1 after the next free read.
    assert captured == [0, 1, 2, 0]
