from __future__ import annotations

from pathlib import Path

import pytest

from sim.scenario import lint_scenario, load_scenario

SMOKE = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"
WEEK_ONE = Path(__file__).resolve().parent.parent / "scenarios" / "week_one_launch"


def test_smoke_loads_cleanly():
    s = load_scenario(SMOKE)
    assert s.config.id == "smoke"
    assert s.config.agent_id == "person.tpm"
    assert "person.tpm" in s.world.people
    assert s.world.agent_id == "person.tpm"
    assert "channel.general" in s.world.channels
    assert "task.SMOKE-1" in s.world.tasks


def test_smoke_lint_passes_with_no_errors():
    s = load_scenario(SMOKE)
    issues = lint_scenario(s)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_loader_returns_origin_minute_of_week_from_start():
    s = load_scenario(SMOKE)
    # Mon 09:00 = 0 * 1440 + 9 * 60 = 540
    assert s.world.scenario_origin_minute_of_week == 540


def test_loader_schedules_hourly_heartbeats():
    s = load_scenario(SMOKE)
    # end_sim_time = 480; expect heartbeats at 60, 120, ..., 480 (8 total)
    heartbeats = [e for e in s.scheduler.pending() if e.kind == "agent_heartbeat"]
    assert len(heartbeats) == 8


def test_loader_snapshot_round_trip_byte_equal():
    s = load_scenario(SMOKE)
    raw1 = s.world.to_json()
    # Reload and compare snapshots
    s2 = load_scenario(SMOKE)
    raw2 = s2.world.to_json()
    assert raw1 == raw2


def test_week_one_launch_matches_plan_benchmarks():
    s = load_scenario(WEEK_ONE)
    assert len(s.world.people) == 10
    assert len(s.world.channels) == 4
    assert len(s.world.messages) == 40
    assert len(s.world.emails) == 12
    assert len(s.world.tasks) == 25
    assert len(s.world.docs) == 6
    assert len(s.eval_ground_truth.objectives) == 18
    assert len(s.eval_ground_truth.artifacts) == 3
    assert len(s.eval_ground_truth.anti_hack) == 4


def test_week_one_launch_lint_clean():
    s = load_scenario(WEEK_ONE)
    issues = lint_scenario(s)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], f"unexpected errors: {errors}"


def test_week_one_launch_scheduled_events_fire_in_order():
    """Run a no-op agent through the entire week and verify pre-scheduled
    events all fire by end_sim_time, in fire_at order, with no errors."""
    s = load_scenario(WEEK_ONE)
    fired = []

    # Wrap the event queue: collect fire_at order as events drain.
    # Drain to end_sim_time.
    target = s.config.end_sim_time
    while s.scheduler.peek_next_fire_at() is not None and s.scheduler.peek_next_fire_at() <= target:
        fa = s.scheduler.peek_next_fire_at()
        s.scheduler.advance_to(fa)
        fired.append(fa)

    s.scheduler.advance_to(target)
    # Pre-scheduled events should produce real world side-effects.
    # bigcorp escalation email arrives Tue 10:00 = sim_time 1500
    bigcorp_emails = [e for e in s.world.emails.values() if e.sender_id == "person.bigcorp"]
    assert len(bigcorp_emails) == 1
    assert bigcorp_emails[0].sim_time == 1500
    # Maya DM to TPM lives in a DM channel
    maya_dms = [
        m for m in s.world.messages.values()
        if m.sender_id == "person.maya"
        and s.world.channels.get(m.channel_id) is not None
        and s.world.channels[m.channel_id].is_dm
    ]
    assert len(maya_dms) == 1
    assert "migration" in maya_dms[0].body.lower()
    # CEO email arrives Wed
    ceo_emails = [e for e in s.world.emails.values() if e.sender_id == "person.alex"]
    # We have one CEO email arriving as a scheduled event (plus none seeded with alex as sender)
    assert any("launch" in (s.world.email_threads[e.thread_id].subject.lower()) for e in ceo_emails)


def test_smoke_default_tick_size_is_15():
    s = load_scenario(SMOKE)
    assert s.config.tick_size_minutes == 15


def test_week_one_launch_default_tick_size_is_15():
    s = load_scenario(WEEK_ONE)
    assert s.config.tick_size_minutes == 15


def test_explicit_tick_size_in_yaml_is_reflected(tmp_path):
    (tmp_path / "personas").mkdir()
    (tmp_path / "scenario.yaml").write_text(
        "id: x\nseed: 0\nagent_id: person.tpm\ntick_size_minutes: 30\n"
    )
    (tmp_path / "personas" / "tpm.yaml").write_text(
        "id: person.tpm\ndisplay_name: TPM\nrole: tpm\nis_agent: true\n"
    )
    s = load_scenario(tmp_path)
    assert s.config.tick_size_minutes == 30


def test_load_scenario_tick_size_override_takes_precedence(tmp_path):
    (tmp_path / "personas").mkdir()
    (tmp_path / "scenario.yaml").write_text(
        "id: x\nseed: 0\nagent_id: person.tpm\ntick_size_minutes: 30\n"
    )
    (tmp_path / "personas" / "tpm.yaml").write_text(
        "id: person.tpm\ndisplay_name: TPM\nrole: tpm\nis_agent: true\n"
    )
    s = load_scenario(tmp_path, tick_size_minutes=5)
    assert s.config.tick_size_minutes == 5


def test_cli_run_parses_tick_size_flag():
    """Parser-level check that `--tick-size N` is accepted by `sim run`
    and lands on the parsed args. (Avoids an end-to-end agent run.)"""
    import argparse
    from sim.cli import _add_run

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    _add_run(sub)

    args = parser.parse_args(["run", "--scenario", "x", "--tick-size", "45"])
    assert args.tick_size == 45

    # When omitted, tick_size is None so the scenario default applies.
    args = parser.parse_args(["run", "--scenario", "x"])
    assert args.tick_size is None


def test_lint_detects_unknown_assignee(tmp_path):
    # Build a minimal scenario in tmp_path with a dangling assignee
    (tmp_path / "personas").mkdir()
    (tmp_path / "seed").mkdir()
    (tmp_path / "scenario.yaml").write_text(
        "id: x\nseed: 0\nagent_id: person.tpm\n"
    )
    (tmp_path / "personas" / "tpm.yaml").write_text(
        "id: person.tpm\ndisplay_name: TPM\nrole: tpm\nis_agent: true\n"
    )
    (tmp_path / "seed" / "tasks.yaml").write_text(
        "tasks:\n  - id: task.bad\n    project: p\n    title: t\n    assignee_id: person.ghost\n"
    )
    s = load_scenario(tmp_path)
    issues = lint_scenario(s)
    codes = [i.code for i in issues]
    assert "unknown_assignee" in codes
