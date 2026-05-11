"""Top-level CLI: `sim` command.

Subcommands:
  - run     — run an agent against a scenario
  - inspect — pretty-print world state at a sim_time from a run dir
  - grade   — re-score a finished run
  - replay  — step through a finished run's audit log
  - lint    — validate a scenario bundle
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from sim.agent import AgentDriver, BriefingAssembler, ScriptedAgent
from sim.agent.driver import DriverConfig
from sim.logging import RunLogger
from sim.runtime import build_runtime
from sim.scenario import lint_scenario, load_scenario
from sim.tools import ToolCall


def _add_lint(sub):
    p = sub.add_parser("lint", help="Validate a scenario bundle.")
    p.add_argument("scenario", type=Path)
    p.set_defaults(func=_cmd_lint)


def _cmd_lint(args):
    scenario = load_scenario(args.scenario)
    issues = lint_scenario(scenario)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    for i in issues:
        print(f"  [{i.severity}] {i.code}: {i.message}")
    print(f"\n{len(errors)} errors, {len(warnings)} warnings")
    return 0 if not errors else 1


def _add_run(sub):
    p = sub.add_parser("run", help="Run an agent against a scenario.")
    p.add_argument("--scenario", type=Path, required=True)
    p.add_argument("--max-turns", type=int, default=200)
    p.add_argument("--agent", default="scripted_noop",
                   help="Which agent to run: 'scripted_noop' or 'anthropic'")
    p.add_argument("--model", default=None, help="Override model name for anthropic agent")
    p.add_argument("--log-dir", type=Path, default=Path("runs"),
                   help="Output directory for run artifacts. Use '-' to disable.")
    p.set_defaults(func=_cmd_run)


def _cmd_run(args):
    scenario = load_scenario(args.scenario)
    rt = build_runtime(scenario)
    assembler = BriefingAssembler(rt.world, end_sim_time=scenario.config.end_sim_time)

    # Set up the logger before driver wires its observer
    logger: RunLogger | None = None
    if str(args.log_dir) != "-":
        ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        run_dir = args.log_dir / f"{ts}_{scenario.config.id}"
        logger = RunLogger(run_dir)
        logger.wire(rt.world)

    if args.agent == "anthropic":
        from sim.agent.reference_agent import AnthropicModelClient, ReferenceAgent
        client = AnthropicModelClient(model=args.model) if args.model else AnthropicModelClient()
        agent = ReferenceAgent(client=client, tool_specs=rt.agent_registry.tool_specs())
    else:
        agent = ScriptedAgent([], default=ToolCall(
            tool="wait.until",
            args={"target_sim_time": scenario.config.end_sim_time},
        ))
    driver = AgentDriver(
        rt.world, rt.scheduler, rt.agent_registry, agent, assembler,
        DriverConfig(max_turns=args.max_turns, end_sim_time=scenario.config.end_sim_time),
        turn_observer=(logger.log_turn if logger else None),
    )
    turns = driver.run()
    if logger:
        logger.finalize(rt.world, scenario.config, args.scenario)
        print(f"Completed {len(turns)} turns; final sim_time={rt.scheduler.sim_time}; run dir: {logger.run_dir}")
    else:
        print(f"Completed {len(turns)} turns; final sim_time={rt.scheduler.sim_time}")
    return 0


def _add_inspect(sub):
    p = sub.add_parser("inspect", help="Inspect a loaded scenario at a sim_time.")
    p.add_argument("scenario", type=Path)
    p.add_argument("--at", type=int, default=0, help="sim_time (minutes from start)")
    p.set_defaults(func=_cmd_inspect)


def _cmd_inspect(args):
    scenario = load_scenario(args.scenario)
    rt = build_runtime(scenario)
    rt.scheduler.advance_to(args.at)
    snapshot = rt.world.snapshot()
    print(json.dumps(json.loads(snapshot.model_dump_json()), indent=2)[:2000])
    return 0


def _add_grade(sub):
    p = sub.add_parser("grade", help="Re-score a finished run from disk.")
    p.add_argument("run_dir", type=Path)
    p.set_defaults(func=_cmd_grade)


def _cmd_grade(args):
    from sim.evaluator.final import grade_and_write
    from sim.evaluator.judge import AnthropicJudge
    if os.environ.get("ANTHROPIC_API_KEY"):
        judge = AnthropicJudge()
    else:
        print(
            "WARNING: ANTHROPIC_API_KEY not set; the LLM judge will not run. "
            "All judge axes and per-artifact rubrics will score 0.0 from a stub.",
            file=sys.stderr,
        )
        judge = None
    result = grade_and_write(args.run_dir, judge=judge)
    print(f"composite_score: {result.composite_score:+.3f} (tier={result.tier_used})")
    if result.errors:
        print(f"  {len(result.errors)} judge issue(s):", file=sys.stderr)
        for e in result.errors:
            print(f"    - {e}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sim")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_run(sub)
    _add_inspect(sub)
    _add_lint(sub)
    _add_grade(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
