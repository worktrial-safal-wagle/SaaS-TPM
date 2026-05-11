# SaaS-TPM

A simulation environment that drops an LLM agent into the role of a Technical Program Manager during their first work week at a small/medium SaaS company. The agent reads chat, email, tasks, calendar, and docs; talks to LLM-driven NPC coworkers; attends meetings; and is graded on how well it moves projects forward with minimum noise. Designed for evaluation of agent behavior, not training.

The system is single-node, event-driven, and fully replayable. Every run produces a self-contained directory that can be re-scored from disk.

## Quickstart

```bash
# Install
pip install -e '.[anthropic,dev]'

# Verify
pytest tests/ -q                                    # ~166 tests, sub-second

# Lint the seeded scenario
sim lint scenarios/week_one_launch

# Run a scripted no-op agent end-to-end (no API key needed)
sim run --scenario scenarios/smoke

# Run the reference LLM agent (requires ANTHROPIC_API_KEY)
sim run --scenario scenarios/week_one_launch --agent anthropic --model claude-sonnet-4-6

# Grade a finished run
sim grade runs/<timestamp>_<scenario>
cat runs/<timestamp>_<scenario>/final_evaluation.json
```

## What's in a run directory

```
runs/<timestamp>_<scenario>/
├── turns.jsonl              # one line per agent decision turn
├── events.jsonl             # one line per world-state mutation
├── verdicts.jsonl           # windowed-evaluator verdicts (one per N turns)
├── world_final.json         # final World snapshot
├── transcript.md            # human-readable turn-by-turn
├── run.json                 # metadata (scenario id, seed, status)
├── index.json               # aggregate stats (turn count, tool counts, errors)
├── SCHEMA.md                # auto-generated schema reference
├── artifacts/               # long payloads hoisted by sha256
├── scenario/                # copy of the scenario bundle
└── final_evaluation.json    # final scorecard (after `sim grade`)
```

## CLI

| Command | Purpose |
|---|---|
| `sim lint <scenario>` | Validate a scenario bundle (no LLM calls). |
| `sim run --scenario X` | Run an agent against a scenario; writes `runs/<ts>_<id>/`. |
| `sim inspect <scenario>` | Pretty-print a scenario's loaded world state. |
| `sim grade <run_dir>` | Re-score a finished run, writes `final_evaluation.json`. |

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Scheduler — priority queue, key = (sim_time, event_id)  │
│  sim_time advances only when events fire / tools cost    │
└──────┬─────────────────────────────────────┬─────────────┘
       │ fire                                │ enqueue
       ▼                                     │
┌──────────────────────────┐                 │
│  World (in-memory)       │                 │
│  Person, Task, Message,  │                 │
│  Email, CalendarEvent,   │                 │
│  Doc, MeetingTranscript  │                 │
│  + ._emit(event_name)    │                 │
└──┬──────────────────┬────┘                 │
   │ mutate           │ subscribe            │
   │                  │                      │
┌──┴────────┐   ┌─────┴──────┐               │
│ Tools     │   │ NPC        │───────────────┤
│ (agent →) │   │ runtime    │   schedules NPC events
│ validate, │   │  + brain   │   based on policy + LLM
│ ACL,      │   │  (LLM)     │
│ cost,     │   └────────────┘
│ apply     │
└────┬──────┘
     │ tool call
┌────┴──────────┐
│ AgentDriver   │   perceive (briefing) → decide (LLM) → act (tool) loop
│ + Briefing    │   wakes on: incoming msg, calendar event, hourly heartbeat
└───────────────┘
```

See `docs/architecture.md` for the full walkthrough and `docs/grading.md` for the evaluator design.

## Tool surface (9 tools)

| Tool | Operations |
|---|---|
| `chat` | `send`, `dm`, `read`, `list`, `mark_read` |
| `email` | `send`, `read`, `list` |
| `calendar` | `list`, `get`, `rsvp`, `create` |
| `tasks` | `list`, `get`, `create`, `update_status`, `assign`, `log_work`, `add_dependency`, `comment` |
| `docs` | `create`, `read`, `edit`, `list`, `comment` |
| `meetings` | `attend`, `get_transcript` |
| `directory` | `list`, `get` |
| `notifications` | `list`, `mark_read` |
| `wait` | `until`, `for_next_event` |

Each tool validates args, applies ACL/visibility, declares its sim-time cost up front, and returns a typed `ToolResult`. See `docs/architecture.md`.

## Realism

The world pushes back through explicit levers:

- **1-minute discrete-event sim clock.** Tool calls advance `sim_time` by declared cost. Real wall-clock inference latency is invisible.
- **Business-hours gating with jitter.** NPC actions queued out-of-hours snap to next business open + 0–60 min jitter so the morning isn't a thundering herd.
- **Per-NPC `responsiveness` multiplier** scales delay distributions (junior eng = 2.5, VP = 0.3).
- **NPC three-layer split.** LLM brain proposes tool calls → Python runtime applies delay + business hours + hop caps → policy allow-list gates outputs. The brain is content; the runtime is behavior; the policy is authority.
- **Peer-to-peer hop cap** on `(sender, recipient)` pairs prevents NPC↔NPC chatter cascades.
- **Task effort gating.** Tasks carry `estimated_effort_seconds`; `tasks.log_work` decrements remaining; `Done` is rejected while remaining > 0.
- **Meetings auto-generate transcripts** whether the agent attends or not.
- **Replayable execution.** NPC brain outputs are cached by `(scenario_id, seed, event_id)` so re-running a scenario produces identical NPC behavior.

## Evaluation (two layers)

1. **Windowed live evaluator** (Haiku 4.5 by default, stubbed in tests). Every 10 agent turns, scores the window on the single principle *"is the agent moving projects forward with minimum noise?"*. The latest verdict feeds the next briefing — the agent can course-correct mid-run.

2. **End-of-run final evaluator** (Sonnet 4.6 by default). Reads the run dir from disk and writes `final_evaluation.json`:
   - **Programmatic metrics** (in `[-1, +1]`): `error_rate`, `turns_per_sim_hour`, `repeat_read_rate`, `anti_hack_max_messages`, and for completed runs `deadline_hit_rate`, `stakeholder_contact_rate`.
   - **Judge axes** (in `[-1, +1]`): `specificity`, `decision_hygiene`, `risk_escalation`, and for completed runs `state_accuracy`. The judge sees only a redacted summary — never the agent transcript or self-claims.
   - **Per-artifact rubrics** judged in isolation against 3–5 yes/no items each. Fold into `decision_hygiene`.
   - **Composite** = arithmetic mean across all contributing axes (tier changes the denominator, not weights).

A well-run scenario lands between +0.3 and +0.6. A spammy run lands below -0.20. See `docs/grading.md` for the anti-hack story.

## Extending

- **New scenario:** copy `scenarios/smoke/`, edit YAMLs, point the runner at it with `--scenario <path>`. Pure data; no Python required.
- **New tool:** subclass `Tool` in `sim/tools/base.py`, register it via the registry, declare its sim-time cost in `costs.py`.
- **New metric:** add a producer to `sim/evaluator/metrics.py` with a `tier` label.

See `docs/extending.md`.

## License

This is a take-home project artifact. No license declared.
