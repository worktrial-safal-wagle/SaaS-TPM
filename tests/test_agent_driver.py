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


def test_briefing_renders_stall_warning_when_counter_high():
    """The briefing's stall warning surfaces when sim_time_stalled_for_turns
    is high enough — defensive UI for any case where sim_time doesn't advance
    (e.g., wait.until called with target equal to current sim_time, or
    no-cost mark_read calls repeated)."""
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    b = assembler.build(
        now_sim_time=rt.scheduler.sim_time,
        last_turn_sim_time=rt.scheduler.sim_time,
        sim_time_stalled_for_turns=4,
    )
    assert "Stall warning" in render_briefing(b)
    # Below threshold (3), the warning is absent.
    b2 = assembler.build(
        now_sim_time=rt.scheduler.sim_time,
        last_turn_sim_time=rt.scheduler.sim_time,
        sim_time_stalled_for_turns=1,
    )
    assert "Stall warning" not in render_briefing(b2)


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


# ---------------------------------------------------------------------------
# Recent-read full-content preservation (transcript-dive fix)
# ---------------------------------------------------------------------------


def test_briefing_renders_full_result_for_recent_read_calls():
    """The most recent read calls should have their full result body in the
    rendered briefing — without it the agent re-reads the same artifact on
    consecutive turns because result_summary is truncated."""
    from sim.agent.briefing import (
        Briefing, BriefingRecentAction, render_briefing,
    )
    long_body = "Hey Robin — quick heads-up. The v2.4 migration's been flaky in my dry-runs this weekend (~1 in 5 timeouts under load). Worth a chat with Kai before deploy?"
    actions = [
        BriefingRecentAction(
            turn=0, sim_time=10, tool="chat.read",
            args_summary='{"channel_id":"dm.maya__tpm"}', ok=True,
            result_summary="1 messages; sample=[{\"body\": \"Hey Robin — quick heads-up. The v2.4 migration's been flaky...\"}]",
            result_full=f'{{"messages":[{{"body":"{long_body}"}}]}}'
        ),
    ]
    b = Briefing(
        now_sim_time=20, now_label="Mon 09:20", end_sim_time=480,
        agent_persona={"id": "person.tpm", "display_name": "TPM",
                       "role": "tpm", "persona_notes": "", "team": None},
        unread_notifications_count=0, unread_chats=[], unread_emails=[],
        open_commitments=[], project_board=[], upcoming_calendar=[],
        recent_actions=actions,
    )
    md = render_briefing(b)
    # Full content is present — the agent can see Maya's actual question
    assert "Worth a chat with Kai before deploy?" in md
    assert "full result" in md


def test_briefing_uses_summary_for_older_reads_beyond_n5():
    """Only the 5 most recent read calls get the full result. Older reads
    fall back to the terse summary — guards against unbounded briefing growth
    and preserves the metric's ability to flag genuinely-distant re-reads."""
    from sim.agent.briefing import (
        Briefing, BriefingRecentAction, render_briefing,
    )
    # 6 read calls; oldest should not get full content rendered.
    actions = []
    for i in range(6):
        actions.append(BriefingRecentAction(
            turn=i, sim_time=i * 10, tool="chat.read",
            args_summary=f'{{"channel_id":"channel.{i}"}}', ok=True,
            result_summary=f"summary for read {i}",
            result_full=f"FULL_BODY_FOR_READ_{i}_THIS_IS_VERY_DISTINCTIVE",
        ))
    b = Briefing(
        now_sim_time=100, now_label="Mon 10:40", end_sim_time=480,
        agent_persona={"id": "person.tpm", "display_name": "TPM",
                       "role": "tpm", "persona_notes": "", "team": None},
        unread_notifications_count=0, unread_chats=[], unread_emails=[],
        open_commitments=[], project_board=[], upcoming_calendar=[],
        recent_actions=actions,
    )
    md = render_briefing(b)
    # 5 most recent reads (turns 1..5) have full content
    for i in range(1, 6):
        assert f"FULL_BODY_FOR_READ_{i}_THIS_IS_VERY_DISTINCTIVE" in md, f"read {i} should have full content"
    # The oldest read (turn 0) does NOT have full content rendered
    assert "FULL_BODY_FOR_READ_0_THIS_IS_VERY_DISTINCTIVE" not in md
    # ... but its terse summary IS rendered
    assert "summary for read 0" in md


def test_driver_populates_result_full_only_for_read_tools():
    """Driver should set result_full on read-tool turns and leave it None on
    write-tool turns (no body to recall)."""
    from sim.agent.driver import _full_result_content
    # Read tool with content
    chat_result = {"messages": [{"body": "hello", "sender_id": "person.maya"}]}
    full = _full_result_content("chat.read", chat_result)
    assert full is not None
    assert "hello" in full
    # Write tool — no need to recall
    send_result = {"message_id": "msg.123", "ok": True}
    full = _full_result_content("chat.send", send_result)
    assert full is None


# ---------------------------------------------------------------------------
# Failure-cost: failed tool calls advance sim_time (prevent infinite loops)
# ---------------------------------------------------------------------------


def test_failed_tool_call_advances_sim_time():
    """Failed tool calls cost 1 sim-minute so the scheduler eventually
    advances past end_sim_time even if the agent loops on a broken call."""
    from sim.tools import ToolCall
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    start = rt.scheduler.sim_time
    # Unknown tool — fails on the first dispatch path
    result = rt.agent_registry.dispatch(ToolCall(tool="bogus.tool", args={}))
    assert result.ok is False
    assert result.cost_minutes == 1
    assert rt.scheduler.sim_time == start + 1


def test_repeated_failed_calls_eventually_cross_end_sim_time():
    """The agent loop terminates against end_sim_time even when the agent
    keeps calling a failing tool — this was the infinite-loop bug."""
    from sim.tools import ToolCall
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=10)
    # Agent keeps calling an unknown tool. With cost=1 on failure, sim_time
    # crosses 10 in <= 11 turns.
    agent = ScriptedAgent([], default=ToolCall(tool="bogus.tool", args={}))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=100, end_sim_time=10),
    )
    turns = driver.run()
    # Loop should terminate around turn 10-11 (each failed call advances
    # sim_time by 1), well before the max_turns=100 cap.
    assert len(turns) <= 12, f"expected <= 12 turns, got {len(turns)}"
    assert rt.scheduler.sim_time >= 10
