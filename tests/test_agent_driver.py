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
        open_commitments=[], project_board=[], upcoming_calendar=[],
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


# ---------------------------------------------------------------------------
# Phase 3: bounded project board in the briefing
# ---------------------------------------------------------------------------


def _smoke_world_with_extras():
    """Load smoke and return its runtime; helper for project-board tests so we
    can add tasks/people directly to a real World instance."""
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    return s, rt


def test_project_board_includes_tasks_across_all_assignees():
    """TPM coordinates the whole team — the board must show OTHERS' tasks
    too, not just the agent's."""
    from sim.store.entities import Person, Task
    s, rt = _smoke_world_with_extras()
    rt.world.add_person(Person(id="person.kai", display_name="Kai",
                               role="eng", team="eng"))
    rt.world.add_task(Task(
        id="task.KAI-1", project="proj", title="Kai's work",
        status="In Progress", assignee_id="person.kai", priority="P0",
    ))
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    ids = [item.task_id for item in b.project_board]
    assert "task.SMOKE-1" in ids  # agent's own
    assert "task.KAI-1" in ids    # other person's — must still be visible


def test_project_board_excludes_done_tasks():
    from sim.store.entities import Person, Task
    s, rt = _smoke_world_with_extras()
    rt.world.add_person(Person(id="person.kai", display_name="Kai",
                               role="eng", team="eng"))
    rt.world.add_task(Task(
        id="task.DONE-1", project="proj", title="Already done",
        status="Done", assignee_id="person.kai", priority="P0",
    ))
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    ids = [item.task_id for item in b.project_board]
    assert "task.DONE-1" not in ids


def test_project_board_sorts_by_priority_then_deadline():
    from sim.store.entities import Person, Task
    s, rt = _smoke_world_with_extras()
    rt.world.add_person(Person(id="person.kai", display_name="Kai",
                               role="eng", team="eng"))
    rt.world.add_task(Task(
        id="task.P0-LATE", project="proj", title="P0 late",
        status="Backlog", assignee_id="person.kai",
        priority="P0", deadline_sim_time=1000,
    ))
    rt.world.add_task(Task(
        id="task.P0-SOON", project="proj", title="P0 soon",
        status="Backlog", assignee_id="person.kai",
        priority="P0", deadline_sim_time=100,
    ))
    rt.world.add_task(Task(
        id="task.P1-NONE", project="proj", title="P1 no deadline",
        status="Backlog", assignee_id="person.kai", priority="P1",
    ))
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    ids = [item.task_id for item in b.project_board]
    # P0 sooner-deadline before P0 later-deadline before P1 no-deadline
    assert ids.index("task.P0-SOON") < ids.index("task.P0-LATE")
    assert ids.index("task.P0-LATE") < ids.index("task.P1-NONE")


def test_project_board_caps_at_twenty():
    """Bounded so the briefing payload doesn't grow unboundedly."""
    from sim.store.entities import Person, Task
    s, rt = _smoke_world_with_extras()
    rt.world.add_person(Person(id="person.kai", display_name="Kai",
                               role="eng", team="eng"))
    for i in range(30):
        rt.world.add_task(Task(
            id=f"task.X-{i:02d}", project="proj", title=f"task {i}",
            status="Backlog", assignee_id="person.kai", priority="P2",
        ))
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    assert len(b.project_board) == 20


def test_project_board_marks_blocked_when_dependency_not_done():
    from sim.store.entities import Person, Task
    s, rt = _smoke_world_with_extras()
    rt.world.add_person(Person(id="person.kai", display_name="Kai",
                               role="eng", team="eng"))
    rt.world.add_task(Task(
        id="task.UPSTREAM", project="proj", title="upstream",
        status="In Progress", assignee_id="person.kai", priority="P1",
    ))
    rt.world.add_task(Task(
        id="task.DOWNSTREAM", project="proj", title="downstream",
        status="Backlog", assignee_id="person.kai", priority="P1",
        depends_on=["task.UPSTREAM"],
    ))
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(now_sim_time=0, last_turn_sim_time=-1)
    by_id = {item.task_id: item for item in b.project_board}
    assert by_id["task.DOWNSTREAM"].blocked is True
    assert by_id["task.UPSTREAM"].blocked is False
