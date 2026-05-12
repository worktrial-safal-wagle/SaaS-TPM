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


def test_turns_per_sim_hour_clamps_in_slightly_quiet_region(tmp_path):
    """[2, 5) used to fall through to the >15 formula and produce values > 1.0
    (run #2 reported +1.69 for raw=4.63). Now properly bounded to [-1, +1]."""
    from sim.evaluator.metrics import turns_per_sim_hour
    # raw = 250 * 60 / 2940 ≈ 5.10 — should clamp at +1.0 (just barely in ideal)
    rd = _write_turns_for_density(tmp_path, 250, 2940)
    result = turns_per_sim_hour(rd)
    assert result.normalized == 1.0
    assert -1.0 <= result.normalized <= 1.0


def test_turns_per_sim_hour_ramp_at_intermediate_density(tmp_path):
    """raw=4 should land halfway through the linear ramp from -0.3 to +1.0."""
    from sim.evaluator.metrics import turns_per_sim_hour
    # 4 turns * 60 / 60 = raw 4.0 → -0.3 + (4-2) * 1.3/3 = -0.3 + 0.867 = 0.567
    rd = _write_turns_for_density(tmp_path, 4, 60)
    result = turns_per_sim_hour(rd)
    assert abs(result.normalized - 0.567) < 0.01
    assert -1.0 <= result.normalized <= 1.0


def test_turns_per_sim_hour_boundary_points_within_range(tmp_path):
    """All branches return values in [-1, +1]."""
    from sim.evaluator.metrics import turns_per_sim_hour
    # Below 2: raw=1, returns -0.3
    rd = _write_turns_for_density(tmp_path / "a", 1, 60)
    assert turns_per_sim_hour(rd).normalized == -0.3
    # Boundary: raw=5, returns +1.0
    rd = _write_turns_for_density(tmp_path / "b", 5, 60)
    assert turns_per_sim_hour(rd).normalized == 1.0
    # Boundary: raw=15, returns +1.0
    rd = _write_turns_for_density(tmp_path / "c", 15, 60)
    assert turns_per_sim_hour(rd).normalized == 1.0
    # Slightly noisy: raw=22, returns 1.0 - (22-15)/15 ≈ 0.533
    rd = _write_turns_for_density(tmp_path / "d", 22, 60)
    assert abs(turns_per_sim_hour(rd).normalized - 0.533) < 0.01
    # Too noisy: raw=40, returns -1.0
    rd = _write_turns_for_density(tmp_path / "e", 40, 60)
    assert turns_per_sim_hour(rd).normalized == -1.0


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


def test_tight_loop_rate_clean_run(tmp_path):
    """Varied tool calls — no tight loops, score +1.0."""
    from sim.evaluator.metrics import tight_loop_rate
    # Different tool each turn = no consecutive same-key streak.
    tools = ["tasks.list", "chat.read", "tasks.get", "email.read",
             "chat.send", "tasks.update_status", "email.send", "docs.read",
             "tasks.log_work", "chat.list"]
    turns = [_ok_turn(i, t, {"i": i}) for i, t in enumerate(tools)]
    rd = _write_turns_from_list(tmp_path, turns)
    result = tight_loop_rate(rd)
    assert result.normalized == 1.0
    assert result.raw == 0.0


def test_tight_loop_rate_catches_successful_spam(tmp_path):
    """100 identical successful chat.send calls is a tight loop too — the
    agent isn't varying its behaviour. This is the spam pattern that the
    `golden_low_score` test was originally meant to penalise."""
    from sim.evaluator.metrics import tight_loop_rate
    turns = [
        _ok_turn(i, "chat.send",
                 {"channel_id": "channel.general", "body": "spam"})
        for i in range(20)
    ]
    rd = _write_turns_from_list(tmp_path, turns)
    result = tight_loop_rate(rd)
    assert result.raw == 1.0  # all 20 turns in the loop
    assert result.normalized == -1.0


def test_tight_loop_rate_detects_5_failure_streak(tmp_path):
    """5 consecutive identical failed calls = loop of 5; 5/10 = 0.5 raw."""
    from sim.evaluator.metrics import tight_loop_rate
    # Varied warmup (each turn a different (tool, args) so no loop).
    turns = [
        _ok_turn(0, "tasks.list", {}),
        _ok_turn(1, "chat.read", {"channel_id": "x"}),
        _ok_turn(2, "tasks.get", {"task_id": "t.1"}),
        _ok_turn(3, "email.read", {"thread_id": "th.1"}),
        _ok_turn(4, "docs.read", {"doc_id": "d.1"}),
    ]
    # 5 failed identical calls (the loop)
    for i in range(5, 10):
        turns.append(_failed_turn(i, "meetings.attend", {"event_id": "cal.x"}))
    rd = _write_turns_from_list(tmp_path, turns)
    result = tight_loop_rate(rd)
    assert result.raw == 0.5
    assert result.normalized == -1.0  # 0.5 / 0.2 = 2.5, clamped
    assert result.detail["loop_turns"] == 5
    assert result.detail["longest_loop"]["length"] == 5
    assert result.detail["longest_loop"]["tool"] == "meetings.attend"


def test_tight_loop_rate_threshold_below_3_does_not_count(tmp_path):
    """2 consecutive identical failures is not a loop."""
    from sim.evaluator.metrics import tight_loop_rate
    turns = [_ok_turn(0, "x", {})]
    turns.append(_failed_turn(1, "meetings.attend", {"event_id": "cal.x"}))
    turns.append(_failed_turn(2, "meetings.attend", {"event_id": "cal.x"}))
    # 2 consecutive failures — below threshold of 3
    rd = _write_turns_from_list(tmp_path, turns)
    result = tight_loop_rate(rd)
    assert result.raw == 0.0


def test_tight_loop_rate_success_breaks_streak(tmp_path):
    """3 fails, 1 success, 3 fails = two separate 3-fail streaks (both counted)."""
    from sim.evaluator.metrics import tight_loop_rate
    turns = []
    for i in range(3):
        turns.append(_failed_turn(i, "meetings.attend", {"event_id": "cal.x"}))
    turns.append(_ok_turn(3, "tasks.list", {}))
    for i in range(4, 7):
        turns.append(_failed_turn(i, "meetings.attend", {"event_id": "cal.x"}))
    rd = _write_turns_from_list(tmp_path, turns)
    result = tight_loop_rate(rd)
    # 3 + 3 = 6 loop turns; total = 7
    assert result.detail["loop_turns"] == 6


def test_tight_loop_rate_different_args_do_not_form_loop(tmp_path):
    """Same tool but different args is not a tight loop."""
    from sim.evaluator.metrics import tight_loop_rate
    turns = [
        _failed_turn(0, "meetings.attend", {"event_id": "cal.a"}),
        _failed_turn(1, "meetings.attend", {"event_id": "cal.b"}),
        _failed_turn(2, "meetings.attend", {"event_id": "cal.c"}),
    ]
    rd = _write_turns_from_list(tmp_path, turns)
    result = tight_loop_rate(rd)
    # Each fails individually with different args — no streak ≥ 3
    assert result.raw == 0.0


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


