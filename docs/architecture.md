# Architecture

This doc walks the lifecycle of a single agent turn from `sim run` to scorecard. Where helpful, it links into the code by `file.py:symbol`.

## Components

| Concern | Module | Notes |
|---|---|---|
| Simulated clock + event queue | `sim/scheduler/` | Heap keyed by `(fire_at, event_id)` — fully deterministic ties. |
| In-memory world aggregate | `sim/store/world.py` | All mutations emit typed events via `World._emit`. |
| Entity dataclasses | `sim/store/entities.py` | Pydantic models for free JSON; mutable for ergonomics. |
| Tool surface | `sim/tools/` | One module per namespace; `registry.py` dispatches. |
| NPC behavior | `sim/npc/` | Three layers: brain (LLM), runtime (delays + ACL), policy (intent allow-list). |
| NPC brain (LLM) | `sim/npc/anthropic_brain.py` | Sonnet 4.6 via forced tool-use; persona + knowledge + per-NPC tool schemas; retries with backoff; silent on failure. Wired by default when `ANTHROPIC_API_KEY` is set. |
| Meeting transcripts | `sim/npc/meetings.py` + `sim/tools/meetings.py` | Same brain cache for synthesized + attended. |
| Scenario loader + lint | `sim/scenario/` | YAML → hydrated `World` + scheduled events. Pure data. |
| Agent driver | `sim/agent/driver.py` | Perceive-decide-act loop; one tool per turn. |
| Briefing | `sim/agent/briefing.py` | Structured pydantic → markdown for the LLM. Includes an ambient **project board** (top P0/P1 not-Done tasks across all assignees) so the TPM agent doesn't have to spend turns recreating it via `tasks.list`. |
| Reference agent | `sim/agent/reference_agent.py` | Anthropic SDK + forced tool-use. |
| Run logger | `sim/logging/run_logger.py` | JSONL streams + content-addressed artifacts. |
| Windowed evaluator | `sim/evaluator/windowed.py` | Per-window judge with feedback into briefing. **Deliberately unwired** in `sim run` — see `docs/grading.md`. |
| Final evaluator | `sim/evaluator/final.py` | Reads run dir; programmatic metrics + LLM judge axes + per-artifact rubrics. `sim grade` auto-wires the real LLM judge when `ANTHROPIC_API_KEY` is set. |
| Runtime assembly | `sim/runtime.py` | Wires it all together from a loaded scenario. |

## Lifecycle of a single turn

```
                           ┌──────────────────────────┐
   agent runs              │  AgentDriver.run()       │
                           └──────────┬───────────────┘
                                      │ per turn:
                  ┌───────────────────┴──────────────────┐
                  │  BriefingAssembler.build(now, last)  │
                  └───────────────────┬──────────────────┘
                                      │ Briefing pydantic
                                      ▼
                           ┌──────────────────────────┐
                           │  Agent.decide(briefing)  │
                           │  (LLM or scripted)       │
                           └──────────┬───────────────┘
                                      │ ToolCall
                                      ▼
                           ┌──────────────────────────┐
                           │  ToolRegistry.dispatch   │
                           │   1. validate args       │
                           │   2. ACL check           │
                           │   3. handler mutates     │
                           │   4. advance scheduler   │
                           │      by declared cost    │
                           │   5. drain pending evts  │
                           │   6. collect new notifs  │
                           └──────────┬───────────────┘
                                      │ ToolResult
                                      ▼
                            (turn_observer → RunLogger,
                             feedback_provider ← WindowedEvaluator)
```

### Time advance during step (4)

When the registry advances the scheduler by `cost`, the event queue drains every event with `fire_at ≤ new sim_time`. Those events can be:

- pre-scheduled scenario events (e.g. Tuesday 10:00 customer escalation email)
- hourly `agent_heartbeat` markers (for `wait.for_next_event`)
- queued NPC reactions that became due during the advance

Each fired event may mutate the world and schedule further events. Order is `(fire_at, event_id)` — fully deterministic.

### Why a separate `wait.for_next_event`

The agent shouldn't have to guess sim-time skips between actions. `wait.for_next_event` peeks the scheduler for the earliest event whose `kind` is in `AGENT_VISIBLE_EVENT_KINDS` (`chat_to_agent`, `email_to_agent`, `calendar_event_start`, `agent_heartbeat`) and advances to it.

### NPC perception lag

`NpcRuntime` adds a 1-minute "perception lag" to every NPC reaction, on top of the sampled delay. This ensures the NPC's reply lands *after* the triggering agent's tool dispatch returns, not inside it. Without this, a single agent tool call could cascade through several NPC reactions before the agent regained control, which is unrealistic and made cost accounting noisy.

### NPC brain wiring + visibility

`sim run` defaults to `AnthropicNPCBrain` (Sonnet 4.6) when `ANTHROPIC_API_KEY` is set. Without the key, it falls back to a stub brain and prints a loud WARNING — silent NPCs would mean the eval is grading one-sided agent outbound, which is a meaningless test of TPM judgment.

The brain receives the NPC's authorized **tool schemas** in its `NpcBrainContext.available_tools` — without these, the model would hallucinate parameter names (e.g., `to_user_id` instead of `recipient_id`) and every NPC dispatch would fail validation. The runtime extracts schemas from the NPC's filtered tool registry, so only the policy's allowed tools are visible to the brain.

Brain calls have **retries with exponential backoff** (5 attempts, 4s base → ~60s total wait) so transient rate limits don't silently turn NPCs into stubs for the rest of the run. `NpcRuntime` tracks `brain_failures` and `brain_successes` counters; `sim run` prints them at exit and warns when failure rate exceeds 20%. A run where 50% of NPC reactions silently API-erred used to be invisible — now it's loud.

## Determinism

Three sources of nondeterminism, all controlled:

1. **Scheduler ordering** — `(fire_at, event_id)` with `event_id` from a monotonic counter. No randomness.
2. **NPC delay sampling** — seeded `random.Random` keyed by the scenario's seed. Same seed → same sequence.
3. **NPC brain outputs** — `BrainCache` keyed by `(scenario_id, seed, event_id)`. Cache miss is the only path that calls the underlying brain. Same scenario + same agent path → identical NPC content across runs.

The judge in the final evaluator uses `temperature=0` and is wrapped in `CachedJudge` — re-running `sim grade` within a single process produces byte-identical scorecards. The cache is in-process only; across separate `sim grade` invocations the LLM is called again. A persistent on-disk cache is a planned addition.

Judge failures are first-class. `RubricVerdict.failed` marks any unparseable or unrecoverable response; the composite **excludes** failed axes (rather than blending a fake 0.0), and `final_evaluation.json.errors[]` surfaces every failure so an evaluator under API stress can't silently produce a misleadingly-OK composite.

## Composability

The critical invariant: **scenario seeds and tool calls mutate the world through the same path**. A `Message` inserted by the scenario loader runs `World.add_message`, which emits `message_inserted`, which triggers the NPC runtime — bit-identical to a message sent by the agent's `chat.send` tool. There is no special "seed mode".

This is what makes scenarios pure data: nothing about the seeded state needs to know whether it'll be acted on by the agent or by an NPC's later reaction. Same emitter, same subscribers, same effects.

## Schemas

`SCHEMA.md` is auto-generated inside each run dir from the pydantic models. It never drifts. To inspect the current schema without running: `sim run --scenario scenarios/smoke --log-dir runs && cat runs/<ts>_smoke/SCHEMA.md`.
