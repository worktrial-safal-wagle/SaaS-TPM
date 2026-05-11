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
| Meeting transcripts | `sim/npc/meetings.py` + `sim/tools/meetings.py` | Same brain cache for synthesized + attended. |
| Scenario loader + lint | `sim/scenario/` | YAML → hydrated `World` + scheduled events. Pure data. |
| Agent driver | `sim/agent/driver.py` | Perceive-decide-act loop; one tool per turn. |
| Briefing | `sim/agent/briefing.py` | Structured pydantic → markdown for the LLM. |
| Reference agent | `sim/agent/reference_agent.py` | Anthropic SDK + JSON tool-call parsing. |
| Run logger | `sim/logging/run_logger.py` | JSONL streams + content-addressed artifacts. |
| Windowed evaluator | `sim/evaluator/windowed.py` | Per-window judge with feedback into briefing. |
| Final evaluator | `sim/evaluator/final.py` | Reads run dir; programmatic + judge + rubrics. |
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

## Determinism

Three sources of nondeterminism, all controlled:

1. **Scheduler ordering** — `(fire_at, event_id)` with `event_id` from a monotonic counter. No randomness.
2. **NPC delay sampling** — seeded `random.Random` keyed by the scenario's seed. Same seed → same sequence.
3. **NPC brain outputs** — `BrainCache` keyed by `(scenario_id, seed, event_id)`. Cache miss is the only path that calls the underlying brain. Same scenario + same agent path → identical NPC content across runs.

The judge in the final evaluator uses `temperature=0` and is wrapped in `CachedJudge` — re-running `sim grade` on the same run dir produces byte-identical scorecards.

## Composability

The critical invariant: **scenario seeds and tool calls mutate the world through the same path**. A `Message` inserted by the scenario loader runs `World.add_message`, which emits `message_inserted`, which triggers the NPC runtime — bit-identical to a message sent by the agent's `chat.send` tool. There is no special "seed mode".

This is what makes scenarios pure data: nothing about the seeded state needs to know whether it'll be acted on by the agent or by an NPC's later reaction. Same emitter, same subscribers, same effects.

## Schemas

`SCHEMA.md` is auto-generated inside each run dir from the pydantic models. It never drifts. To inspect the current schema without running: `sim run --scenario scenarios/smoke --log-dir runs && cat runs/<ts>_smoke/SCHEMA.md`.
