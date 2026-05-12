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
    keeps calling a failing tool — this was the infinite-loop bug. With
    the tick-driven loop, end_sim_time must be at least one tick (default
    15 sim-min) for the agent to be polled at all; we use 60 here so the
    agent gets multiple polls before termination."""
    from sim.tools import ToolCall
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    assembler = BriefingAssembler(rt.world, end_sim_time=60)
    # Agent keeps calling an unknown tool. With cost=1 on failure, sim_time
    # crosses 60 in <= 60 ticks.
    agent = ScriptedAgent([], default=ToolCall(tool="bogus.tool", args={}))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=100, end_sim_time=60),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    # Each failed bogus.tool call costs 1 sim-min; the next poll is 15 min
    # later. So sim_time roughly == 16 * N at end of tick N. We expect <= 5
    # ticks before crossing end=60. The cap is well below max_turns=100.
    assert len(turns) <= 12, f"expected <= 12 turns, got {len(turns)}"
    assert rt.scheduler.sim_time >= 60


# ---------------------------------------------------------------------------
# Phase 3: tick-driven loop
# ---------------------------------------------------------------------------


def _make_runtime_for_tick_tests():
    """Build a smoke runtime for tick-driven driver tests. Returns
    `(scenario, runtime)` so test sites can read tick_size_minutes /
    agent_id from the scenario config."""
    s = load_scenario(SMOKE)
    rt = build_runtime(s)
    return s, rt


def test_tick_driven_loop_polls_at_tick_cadence():
    """With tick_size=15, the agent's first poll fires at sim_time=15. Each
    one-min tool call leaves sim_time at 16 → next poll at 31, and so on.
    A scripted noop run lands on the expected sim_time grid."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    # Each tasks.list costs 1 sim-min. After 3 ticks, sim_time = 48.
    agent = ScriptedAgent([], default=ToolCall(tool="tasks.list", args={}))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=3, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    assert len(turns) == 3
    # Tick 1 fires at sim_time=15; tasks.list costs 1 → 16.
    assert turns[0].sim_time_before == 15
    assert turns[0].sim_time_after == 16
    # Tick 2 fires at max(16+15, 0) = 31; → 32.
    assert turns[1].sim_time_before == 31
    assert turns[1].sim_time_after == 32
    # Tick 3 fires at max(32+15, 0) = 47; → 48.
    assert turns[2].sim_time_before == 47
    assert turns[2].sim_time_after == 48


def test_tick_loop_terminates_when_end_sim_time_reached():
    """When the agent runs `wait.until(end_sim_time)`, the driver loop
    exits cleanly on the next iteration."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    agent = ScriptedAgent([
        ToolCall(tool="wait.until", args={"target_sim_time": s.config.end_sim_time}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=20, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    assert rt.scheduler.sim_time >= s.config.end_sim_time
    # The driver only ran ONE tick before sim_time crossed end.
    assert len(turns) == 1


def test_continue_in_tick_chains_two_decisions_in_same_tick():
    """Default 1 decision per tick. Setting `continue_in_tick=True` on the
    first call's response lets the agent do a second action immediately
    in the same tick."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    # Decision 1 chains; decision 2 is the last in the tick.
    agent = ScriptedAgent([
        ToolCall(tool="tasks.list", args={}, continue_in_tick=True),
        ToolCall(tool="tasks.list", args={}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=2, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    assert len(turns) == 2
    # Both turns fired in the SAME tick — sim_time_before of turn[1] is
    # turn[0].sim_time_after (no tick gap between).
    assert turns[1].sim_time_before == turns[0].sim_time_after
    # The first tick fired at 15; both decisions happened back-to-back.
    assert turns[0].sim_time_before == 15
    assert turns[1].sim_time_after == 17  # 15 + 1 + 1


def test_continue_in_tick_hard_cap_at_four():
    """Even if the agent requests 5 chained decisions, the driver only
    dispatches 4 per tick (the design-locked cap). The 5th decision
    lands in the next tick.

    Scripted call layout: [chain, chain, chain, chain, no-chain]. The cap
    triggers between calls 4 and 5; call 5 is dispatched on tick 2
    without further chaining.
    """
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    # Four chaining decisions followed by a non-chaining 5th. Without the
    # cap, the agent would also try to chain the 5th — we test that the
    # cap intervenes by checking that turn 5 (next-tick) does NOT chain.
    chained = [
        ToolCall(tool="tasks.list", args={}, continue_in_tick=True),
        ToolCall(tool="tasks.list", args={}, continue_in_tick=True),
        ToolCall(tool="tasks.list", args={}, continue_in_tick=True),
        ToolCall(tool="tasks.list", args={}, continue_in_tick=True),
        ToolCall(tool="tasks.list", args={}),  # 5th, no chain
    ]
    agent = ScriptedAgent(chained)
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        # max_turns=10 so the cap (not max_turns) terminates the chain.
        DriverConfig(max_turns=10, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    # All 4 chained decisions fired in tick 1; the 5th was deferred to
    # tick 2. After tick 2 the queue is exhausted and the default
    # `wait.until(end_sim_time)` ends the simulation in tick 3 (one more
    # turn), so we expect exactly 6 turns.
    assert len(turns) == 6
    # Tick 1 ran 4 chained decisions: sim_time 15→16→17→18→19.
    for i in range(4):
        assert turns[i].sim_time_before == 15 + i, (
            f"turn {i} sim_time_before was {turns[i].sim_time_before}, expected {15 + i}"
        )
        assert turns[i].sim_time_after == 16 + i
    # Cap kicked in: turn 4 (the 5th decision) landed in TICK 2, not
    # tick 1. Tick 2 fires at max(19+15, 0) = 34.
    assert turns[4].sim_time_before == 34


def test_action_straddles_tick_pushes_next_poll_past_action_end():
    """A 24-min action started at tick start (sim_time=15) ends at
    sim_time=39. The next poll fires at max(39 + 15, busy_until) = 54
    because the action straddled the 15-min tick boundary."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    # docs.create cost = min(90, 15 + len(body)/100). A 900-char body →
    # cost = 15 + 9 = 24 min.
    body_900_chars = "x" * 900
    agent = ScriptedAgent([
        ToolCall(tool="docs.create", args={
            "doc_id": "doc.straddle", "title": "straddle test", "body": body_900_chars,
        }),
        ToolCall(tool="tasks.list", args={}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=2, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    assert len(turns) == 2
    # Tick 1: docs.create runs at sim_time=15, costs 24 min, ends at 39.
    assert turns[0].sim_time_before == 15
    assert turns[0].sim_time_after == 39
    # Tick 2 fires at max(39 + 15, 0) = 54. The action straddled the 15-min
    # tick boundary, so the next poll lands AFTER the action's end, not
    # at the next tick-cadence multiple.
    assert turns[1].sim_time_before == 54


def test_idle_until_overrides_next_poll_at():
    """`idle.until` from inside a tick reschedules the next poll to a
    later sim_time and skips the standard tick cadence."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    agent = ScriptedAgent([
        # At sim_time=15, idle until sim_time=80 → poll@80.
        ToolCall(tool="idle.until", args={"target_sim_time": 80}),
        # Next tick: just list to mark the poll.
        ToolCall(tool="tasks.list", args={}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=2, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    assert len(turns) == 2
    # Tick 1 at sim_time=15. idle.until costs 1 → sim_time=16. Next poll
    # was set to 80 (idle target).
    assert turns[0].sim_time_before == 15
    assert turns[0].call.tool == "idle.until"
    # Tick 2 fires at sim_time=80 (the idle target).
    assert turns[1].sim_time_before == 80


def test_initial_actor_poll_scheduled_via_build_runtime():
    """`build_runtime` must schedule the agent's first poll. The smoke
    runtime should have exactly one pending `actor_poll` for the agent at
    sim_time = tick_size_minutes."""
    from sim.scheduler import EVENT_KIND_ACTOR_POLL
    s, rt = _make_runtime_for_tick_tests()
    polls = [
        e for e in rt.scheduler.pending()
        if e.kind == EVENT_KIND_ACTOR_POLL
        and e.payload.get("actor_id") == s.config.agent_id
    ]
    assert len(polls) >= 1, "expected at least one initial actor_poll for the agent"
    assert polls[0].fire_at == s.config.tick_size_minutes


def test_continue_in_tick_defaults_to_false():
    """A ToolCall without an explicit `continue_in_tick` flag has it set
    to False — backwards compatible with all existing tool-call sites."""
    call = ToolCall(tool="tasks.list", args={})
    assert call.continue_in_tick is False


def test_abandon_truncates_busy_until_inside_a_tick():
    """abandon.current called by the agent inside a tick truncates the
    agent's own busy_until to current + 2 (the transition cost)."""
    from sim.tools.abandon import ABANDON_TRANSITION_COST_MIN
    s, rt = _make_runtime_for_tick_tests()
    # Pre-set the agent's busy_until far in the future to simulate an
    # in-flight long action; the test then drives a single tick where the
    # agent calls abandon.current.
    rt.world.get_person(s.config.agent_id).busy_until = 200
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    agent = ScriptedAgent([
        ToolCall(tool="abandon.current", args={}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=1, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    driver.run()
    person = rt.world.get_person(s.config.agent_id)
    # Tick fired at 15; abandon called at sim_time=15 truncates busy_until
    # to 15 + 2 = 17.
    assert person.busy_until == 15 + ABANDON_TRANSITION_COST_MIN


def test_tick_driven_loop_max_turns_caps_total_decisions():
    """`max_turns` caps the TOTAL number of dispatched decisions across
    all ticks, not the number of ticks. Useful for the eval to bound
    runtime regardless of tick cadence."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    agent = ScriptedAgent([], default=ToolCall(tool="tasks.list", args={}))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=4, end_sim_time=s.config.end_sim_time),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    turns = driver.run()
    assert len(turns) == 4


def test_driver_advances_to_end_sim_time_when_next_poll_is_past_end():
    """When the agent's last action defers the next poll past end_sim_time
    (e.g., `idle.until` that lands beyond the week), the driver must advance
    `sim_time` to `end_sim_time` on termination — otherwise the eval's
    `requires_full_run` tier gate (`final_sim_time >= end_sim_time`) won't
    fire and `deadline_hit_rate` / `stakeholder_contact_rate` are silently
    dropped from the scorecard."""
    s, rt = _make_runtime_for_tick_tests()
    assembler = BriefingAssembler(rt.world, end_sim_time=s.config.end_sim_time)
    # Idle to a sim_time near (but before) end_sim_time, then idle again to
    # something past end_sim_time. The second idle.until reschedules the
    # next poll past end, so the driver should terminate and bump sim_time
    # to end_sim_time.
    end = s.config.end_sim_time
    agent = ScriptedAgent([
        ToolCall(tool="idle.until", args={"target_sim_time": end - 10}),
        ToolCall(tool="idle.until", args={"target_sim_time": end + 60}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=10, end_sim_time=end),
        tick_size_minutes=s.config.tick_size_minutes,
        agent_id=s.config.agent_id,
    )
    driver.run()
    # Without the fix, sim_time would be < end_sim_time (stuck wherever the
    # last idle.until landed). With the fix, the driver bumps it to end.
    assert rt.scheduler.sim_time >= end, (
        f"driver should advance sim_time to end_sim_time on graceful "
        f"termination; got {rt.scheduler.sim_time}, expected >= {end}"
    )
