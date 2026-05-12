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
- **NPCs are LLM-driven characters, not stubs.** Each NPC has its own Sonnet 4.6 brain (`sim/npc/anthropic_brain.py`) seeded from its persona YAML. `sim run` wires this by default when `ANTHROPIC_API_KEY` is set; without the key, it warns and falls back to a stub. The brain is given the NPC's authorized tool schemas so arg names don't get hallucinated.
- **Business-hours gating with jitter.** NPC actions queued out-of-hours snap to next business open + 0–60 min jitter so the morning isn't a thundering herd.
- **Per-NPC `responsiveness` multiplier** scales delay distributions (junior eng = 2.5, VP = 0.3).
- **NPC three-layer split.** LLM brain proposes tool calls → Python runtime applies delay + business hours + hop caps → policy allow-list gates outputs. The brain is content; the runtime is behavior; the policy is authority.
- **Brain failure visibility.** `NpcRuntime` tracks `brain_failures` / `brain_successes`. The CLI prints both at run exit and warns when >20% of NPC reactions failed (rate limits, parse errors) — silent NPC failure used to look identical to "model chose not to react." It doesn't anymore.
- **Peer-to-peer hop cap** on `(sender, recipient)` pairs prevents NPC↔NPC chatter cascades.
- **Task effort gating.** Tasks carry `estimated_effort_seconds`; `tasks.log_work` decrements remaining; `Done` is rejected while remaining > 0.
- **Meetings auto-generate transcripts** whether the agent attends or not.
- **Replayable execution.** NPC brain outputs are cached by `(scenario_id, seed, event_id)` so re-running a scenario produces identical NPC behavior.
- **TPM-shaped briefing.** The agent's briefing includes an ambient **project board** (top 20 P0/P1 not-Done tasks across all assignees) so the TPM doesn't burn turns calling `tasks.list` to recreate a view it would have ambient in a real workplace.

## Evaluation

The end-of-run final evaluator (Sonnet 4.6 by default) reads the run dir from disk and writes `final_evaluation.json`. `sim grade` auto-wires the real LLM judge when `ANTHROPIC_API_KEY` is set; without it, falls back to a stub and warns loudly.

Three layers feed one composite score:

- **Programmatic metrics** (deterministic, no LLM): `error_rate`, `turns_per_sim_hour`, `repeat_read_rate`, plus **five anti-hack signals**: `max_messages_total`, `max_messages_per_channel_per_day`, `forbidden_keywords_in_external_emails`, `must_consult_before_decision`, `forbidden_log_work`. For completed runs add `deadline_hit_rate` and `stakeholder_contact_rate`.
- **Judge axes** (`temperature=0`): `specificity`, `decision_hygiene`, `risk_escalation`, and for completed runs `state_accuracy`. The judge sees a redacted payload — agent outbound + inbound chats and emails, tool counts, and the final task board. **Never** the briefing, internal reasoning, or self-narration.
- **Per-artifact rubrics** judged in isolation: 3–5 yes/no items per artifact, scored on artifact body + ground-truth context only.

**Composite** = arithmetic mean across contributing axes. Scenarios that don't declare a given anti-hack signal report that metric as `contributes=False` so it doesn't drag the score.

**Failure handling**: judge API errors retry with exponential backoff (3 attempts); unrecoverable failures surface as `RubricVerdict.failed=True`, are excluded from the composite, and are listed in `final_evaluation.json.errors[]`. No silent zeros.

A well-run scenario lands between +0.3 and +0.6. A spammy run lands below -0.20. See `docs/grading.md` for the anti-hack story and `docs/architecture.md` for how it fits with the rest of the system.

> **About the windowed evaluator**: code in `sim/evaluator/windowed.py` scores a sliding window of agent turns and feeds the verdict into the next briefing. It's deliberately *unwired* in `sim run`. Giving the agent mid-run feedback would convert this from an evaluation into training — we observe behavior, we don't tutor.

## Extending

- **New scenario:** copy `scenarios/smoke/`, edit YAMLs, point the runner at it with `--scenario <path>`. Pure data; no Python required.
- **New tool:** subclass `Tool` in `sim/tools/base.py`, register it via the registry, declare its sim-time cost in `costs.py`.
- **New metric:** add a producer to `sim/evaluator/metrics.py` with a `tier` label.

See `docs/extending.md`.

## License

This is a take-home project artifact. No license declared.
