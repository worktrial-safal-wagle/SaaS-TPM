from __future__ import annotations

import json
from pathlib import Path

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.logging import RunLogger
from sim.runtime import build_runtime
from sim.scenario import load_scenario
from sim.tools import ToolCall

SMOKE = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"


def _drive(run_dir):
    scenario = load_scenario(SMOKE)
    rt = build_runtime(scenario)
    assembler = BriefingAssembler(rt.world, end_sim_time=scenario.config.end_sim_time)
    logger = RunLogger(run_dir)
    logger.wire(rt.world)
    calls = [
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "In Progress"}),
        ToolCall(tool="tasks.log_work", args={"task_id": "task.SMOKE-1", "seconds": 600}),
        ToolCall(tool="tasks.update_status", args={"task_id": "task.SMOKE-1", "status": "Done"}),
    ]
    agent = ScriptedAgent(calls)
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=10, end_sim_time=scenario.config.end_sim_time),
        turn_observer=logger.log_turn,
    )
    driver.run()
    logger.finalize(rt.world, scenario.config, SMOKE)
    return run_dir


def test_run_dir_contains_all_required_files(tmp_path):
    run_dir = _drive(tmp_path / "run1")
    for name in ("turns.jsonl", "events.jsonl", "verdicts.jsonl",
                 "world_final.json", "run.json", "index.json",
                 "SCHEMA.md", "transcript.md"):
        assert (run_dir / name).is_file(), f"missing {name}"
    assert (run_dir / "scenario").is_dir()


def test_turns_jsonl_records_each_turn(tmp_path):
    run_dir = _drive(tmp_path / "r")
    lines = (run_dir / "turns.jsonl").read_text().strip().splitlines()
    assert len(lines) >= 3
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["tool"] == "tasks.update_status"
    assert parsed[0]["ok"] is True


def test_events_jsonl_captures_world_emits(tmp_path):
    run_dir = _drive(tmp_path / "r")
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines() if line]
    kinds = {e["kind"] for e in events}
    assert "task_status_changed" in kinds
    assert "task_work_logged" in kinds


def test_index_json_aggregates_correctly(tmp_path):
    run_dir = _drive(tmp_path / "r")
    idx = json.loads((run_dir / "index.json").read_text())
    assert idx["turn_count"] >= 3
    assert idx["error_count"] == 0
    assert idx["tool_counts"]["tasks.update_status"] >= 2


def test_world_final_snapshot_is_byte_stable(tmp_path):
    run_a = _drive(tmp_path / "ra")
    run_b = _drive(tmp_path / "rb")
    assert (run_a / "world_final.json").read_text() == (run_b / "world_final.json").read_text()


def test_transcript_md_human_readable(tmp_path):
    run_dir = _drive(tmp_path / "r")
    text = (run_dir / "transcript.md").read_text()
    assert "## Turn 0" in text
    assert "tasks.update_status" in text


def test_schema_md_lists_pydantic_models(tmp_path):
    run_dir = _drive(tmp_path / "r")
    text = (run_dir / "SCHEMA.md").read_text()
    for model_name in ("Person", "Task", "Message", "ToolCall", "Briefing"):
        assert f"## {model_name}" in text


def test_artifact_hoist_for_long_payloads(tmp_path):
    # Force a long payload by creating a doc with a large body
    scenario = load_scenario(SMOKE)
    rt = build_runtime(scenario)
    assembler = BriefingAssembler(rt.world, end_sim_time=scenario.config.end_sim_time)
    logger = RunLogger(tmp_path / "r")
    logger.wire(rt.world)
    big_body = "x" * 6000
    agent = ScriptedAgent([
        ToolCall(tool="docs.create", args={"doc_id": "doc.big", "title": "big", "body": big_body}),
        ToolCall(tool="docs.read", args={"doc_id": "doc.big"}),
    ])
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=5, end_sim_time=scenario.config.end_sim_time),
        turn_observer=logger.log_turn,
    )
    driver.run()
    logger.finalize(rt.world, scenario.config, SMOKE)
    artifacts = list((tmp_path / "r" / "artifacts").glob("*.json"))
    assert len(artifacts) >= 1
    # turns.jsonl should reference the artifact for the read result
    lines = [json.loads(line) for line in (tmp_path / "r" / "turns.jsonl").read_text().splitlines() if line]
    read_line = next(l for l in lines if l["tool"] == "docs.read")
    assert "_artifact_ref" in read_line["result"]
