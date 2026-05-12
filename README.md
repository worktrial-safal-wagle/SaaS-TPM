# SaaS-TPM

A simulation environment that drops an LLM agent into the role of a Technical Program Manager during their first work week at a small/medium SaaS company. The agent reads chat, email, tasks, calendar, and docs; talks to LLM-driven NPC coworkers; attends meetings; and is graded on how well it moves projects forward with minimum noise. Designed for evaluation of agent behavior, not training.

The system is single-node, event-driven, and fully replayable. Time advances on a **configurable tick cadence** (default 15 sim-min) regardless of agent action — so the agent can't freeze time by thinking. Every run produces a self-contained directory that can be re-scored from disk.

## Quickstart

```bash
# Install
pip install -e '.[anthropic,dev]'

# Verify
pytest tests/ -q                                    # ~370 tests, sub-second

# Lint the seeded scenario
sim lint scenarios/week_one_launch

# Run a scripted no-op agent end-to-end (no API key needed)
sim run --scenario scenarios/smoke

# Run the reference LLM agent (requires ANTHROPIC_API_KEY)
sim run --scenario scenarios/week_one_launch --agent anthropic --model claude-sonnet-4-6

# Same, but with a coarser tick cadence — tests strategic-cadence skill
sim run --scenario scenarios/week_one_launch --agent anthropic --model claude-opus-4-7 --tick-size 60

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
│  Event kinds include: actor_poll (tick cadence),         │
│  calendar_event_start, npc-scheduled inbound, heartbeats │
└──────┬─────────────────────────────────────┬─────────────┘
       │ fire                                │ enqueue
       ▼                                     │
┌──────────────────────────┐                 │
│  World (in-memory)       │                 │
│  Person, Task, Message,  │                 │
│  Email, CalendarEvent,   │                 │
│  Doc, MeetingTranscript  │                 │
│  Person.busy_until +     │                 │
│  Person.next_poll_at     │                 │
└──┬──────────────────┬────┘                 │
   │ mutate           │ subscribe            │
   │                  │                      │
┌──┴────────┐   ┌─────┴──────┐               │
│ Tools     │   │ NPC        │───────────────┤
│ (agent →) │   │ runtime    │   reactive: polled on tick cadence,
│ validate, │   │  + brain   │   sees accumulated triggers, decides
│ ACL,      │   │  (LLM)     │   one action per tick (or skips when
│ cost,     │   └────────────┘   queue is empty)
│ apply     │
└────┬──────┘
     │ tool call
┌────┴──────────┐
│ AgentDriver   │   tick loop: every 15 sim-min (default), agent is
│ + Briefing    │   polled with a *delta* briefing (changes since
│ (delta)       │   last tick). Opt-in continuation chains up to 4
│               │   decisions per tick. `idle.until` / `abandon.current`
│               │   surface time-management decisions explicitly.
└───────────────┘
```

See `docs/architecture.md` for the full walkthrough and `docs/grading.md` for the evaluator design.

## Tool surface

| Tool | Operations |
|---|---|
| `chat` | `send`, `dm`, `read`, `list`, `mark_read` (read/mark_read accept `recipient_id` for DMs) |
| `email` | `send`, `read`, `list` |
| `calendar` | `list`, `get`, `rsvp`, `create` |
| `tasks` | `list`, `get`, `create`, `update_status`, `assign`, `log_work`, `add_dependency`, `comment` |
| `docs` | `create`, `read`, `edit`, `list`, `comment` |
| `meetings` | `attend`, `get_transcript` |
| `directory` | `list`, `get`, `presence` |
| `notifications` | `list`, `mark_read` |
| `idle` | `until` (defer next poll up to 120 sim-min, auto-interrupted by urgent inbound) |
| `abandon` | `current` (pre-empt the current long action with a 2-min transition cost) |
| `wait` | `until` (skip clock to a target sim_time without taking action) |

Each tool validates args, applies ACL/visibility, declares its sim-time cost up front, and returns a typed `ToolResult`. Failed tool calls cost the action's *declared* sim-time (a failed 10-min `docs.create` wastes 10 sim-min, not 1) so failure loops surface in the budget. Validation errors name the missing field so the agent can recover. See `docs/architecture.md`.

## Realism

The world pushes back through explicit levers:

- **Tick-driven discrete-event sim clock.** Time advances on a fixed cadence (default 15 sim-min, configurable per scenario and via `--tick-size N`) regardless of agent action. The agent can't freeze time by deliberating — deadlines arrive whether or not it's ready.
- **Personal cadence per actor.** Agent and each NPC have their own `next_poll_at` schedule. Long actions straddle ticks; the actor's next poll is at `max(now + tick, busy_until)` — no global grid alignment, no artificial idle gaps.
- **NPCs go busy during meetings.** Each `CalendarEvent` start sets attendees' `busy_until = event.end_sim_time`. An NPC in a 60-min meeting genuinely can't reply for the duration. Agent that interrupts mid-meeting feels the cascading deadline pressure.
- **NPCs are LLM-driven characters, not stubs.** Each NPC has its own Sonnet 4.6 brain (`sim/npc/anthropic_brain.py`) seeded from its persona YAML. NPCs accumulate triggers between polls; on poll they see the queue, decide one action (or skip). NPCs naturally drop balls when overloaded. The brain is given the NPC's authorized tool schemas so arg names don't get hallucinated.
- **Empty-queue NPC ticks short-circuit.** When an NPC's trigger queue is empty at poll time, the runtime skips the LLM call entirely and just reschedules. Massive wall-clock savings for quiet NPCs without losing reactive realism.
- **Business-hours gating with jitter.** NPC actions outside their working hours snap to next business open + 0–60 min jitter so the morning isn't a thundering herd.
- **Per-NPC `responsiveness` multiplier** scales delay distributions (junior eng = 2.5, VP = 0.3).
- **NPC three-layer split.** LLM brain proposes tool calls → Python runtime applies delay + business hours + hop caps → policy allow-list gates outputs. The brain is content; the runtime is behavior; the policy is authority.
- **Brain failure visibility.** `NpcRuntime` tracks `brain_failures` / `brain_successes` / `empty_queue_skips`. The CLI prints these at run exit and warns when >20% of NPC reactions failed — silent NPC failure used to look identical to "model chose not to react." It doesn't anymore.
- **Peer-to-peer hop cap** on `(sender, recipient)` pairs prevents NPC↔NPC chatter cascades.
- **Task effort gating.** Tasks carry `estimated_effort_seconds`; `tasks.log_work` decrements remaining; `Done` is rejected while remaining > 0.
- **Meetings auto-generate transcripts** whether the agent attends or not.
- **Presence visibility.** `chat.list` returns presence per DM peer; `directory.presence(person_id)` returns it for any person. Tests "did the agent respect a visible signal" rather than "did the agent memorize Kai's calendar."
- **Replayable execution.** NPC brain outputs are cached by `(scenario_id, seed, event_id)` so re-running a scenario produces identical NPC behavior.
- **Delta-first briefings.** Tick 1 is a full briefing; subsequent ticks show only what changed since the last poll. Frames the agent's task as "react to changes." Full state remains accessible via tool calls.

## Evaluation

The end-of-run final evaluator (Sonnet 4.6 by default) reads the run dir from disk and writes `final_evaluation.json`. `sim grade` auto-wires the real LLM judge when `ANTHROPIC_API_KEY` is set; without it, falls back to a stub and warns loudly.

Every signal feeds into one of **four skill clusters**:

| Cluster | Constituent signals |
|---|---|
| **Outcomes Achieved** | `deadline_hit_rate`, `hidden_fact_discovery_rate` |
| **Decision Quality** | per-artifact rubric mean + judge axes (`decision_hygiene`, `specificity`, `risk_escalation`, `state_accuracy`) + process-gate anti-hack signals (`forbidden_external_keywords`, `must_consult_before_decision`, `forbidden_log_work`) |
| **Coordination** | `stakeholder_contact_rate`, `follow_up_rate` |
| **Time Management** | `prioritization_latency`, `opportunity_cost_score`, `appropriate_abandonments_rate`, `bad_timing_starts`, `tick_continuation_judgment`, `idle_judgment_score`, `context_recall_score`, `pre_emption_judgment`, `error_rate` |

**Composite** = mean of the four cluster scores (each weighted 25% regardless of member count). Each cluster = mean of its contributing members. Metrics declared in `eval.yaml` but not violated report `contributes=False` (tripwire semantics) so they don't pad the composite with neutrals. Judge axes that couldn't be sensibly scored on a given run report `not_applicable=True` and are also excluded.

The judge sees a **redacted payload** — agent outbound + inbound chats and emails, tool counts, and the final task board. **Never** the briefing, internal reasoning, or self-narration. Per-artifact rubrics judge the artifact body in isolation against declared yes/no items.

**Failure handling**: judge API errors retry with exponential backoff (3 attempts); unrecoverable failures surface as `RubricVerdict.failed=True`, are excluded from the composite, and listed in `final_evaluation.json.errors[]`. No silent zeros.

**Tier system**:
- `slice_safe` — every cluster's slice_safe members fire even on crashed/partial runs
- `requires_full_run` — `deadline_hit_rate`, `stakeholder_contact_rate`, `state_accuracy` engage only when `sim_time >= end_sim_time`

For a typical week_one_launch run, Opus 4.7 lands around +0.4 to +0.5 with a cluster profile showing where it excels vs where it's middling. See `docs/grading.md` for the full metric list and cluster design.

> **About the windowed evaluator**: code in `sim/evaluator/windowed.py` scores a sliding window of agent turns and feeds the verdict into the next briefing. It's deliberately *unwired* in `sim run`. Giving the agent mid-run feedback would convert this from an evaluation into training — we observe behavior, we don't tutor.

## Extending

- **New scenario:** copy `scenarios/smoke/`, edit YAMLs, point the runner at it with `--scenario <path>`. Pure data; no Python required.
- **New tool:** subclass `Tool` in `sim/tools/base.py`, register it via the registry, declare its sim-time cost in `costs.py`.
- **New metric:** add a producer to `sim/evaluator/metrics.py` with a `tier` label.

See `docs/extending.md`.

## License

This is a take-home project artifact. No license declared.
