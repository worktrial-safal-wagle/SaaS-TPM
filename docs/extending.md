# Extending

## Add a scenario

Scenarios are pure data — no Python required.

```
scenarios/<your_scenario>/
├── scenario.yaml           # required: id, seed, agent_id, start, end_sim_time
├── personas/*.yaml         # one file per Person; NPCs include npc_policy
├── seed/
│   ├── channels.yaml       # optional
│   ├── chats.yaml          # optional
│   ├── emails.yaml         # optional
│   ├── tasks.yaml          # optional
│   ├── docs.yaml           # optional
│   └── calendar.yaml       # optional
├── events.yaml             # optional: pre-scheduled future events
└── eval.yaml               # objectives / artifacts / anti_hack / hidden_facts /
                            # follow_up_targets / appropriate_abandonments
```

`scenario.yaml` can also declare `tick_size_minutes` (default 15). A scenario authored for incident-response cadence might set 5; a strategic-planning scenario might set 60. CLI `--tick-size N` overrides per-run.

Steps:

1. Copy `scenarios/smoke/` as a starting point.
2. Define personas in `personas/*.yaml`. Mark the agent with `is_agent: true`. NPCs need an `npc_policy` with `allowed_tools` and `delays`.
3. Seed initial world state in `seed/*.yaml`.
4. Pre-schedule any future events (customer escalation arriving Tue, CEO email Wed, etc.) in `events.yaml`.
5. Author the ground truth in `eval.yaml`. Add programmatic objectives, declare decision artifacts with rubrics, list anti-hack signals.
6. Validate with `sim lint scenarios/<your_scenario>`.

The lint command catches dangling task dependencies, manager chain references, unknown attendees/participants, and pre-scheduled events outside the scenario window.

## Add a tool

A tool *op* is one named operation under a tool namespace. The pattern in `sim/tools/chat.py` is the reference.

1. Define pydantic args model:
   ```python
   class FooBarArgs(BaseModel):
       channel_id: str
       body: str
   ```
2. Define the handler:
   ```python
   def foo_bar(world, scheduler, args, caller_id) -> dict:
       # validate ACL via sim/tools/acl.py
       # mutate world (which emits events)
       # return a JSON-serialisable result
   ```
3. Declare a cost in `sim/tools/costs.py` (a constant int or a `cost_fn(args) -> int`).
4. Bundle the op:
   ```python
   def foo_ops():
       return [
           ToolOp("foo.bar", FooBarArgs, COST_FOO_BAR, foo_bar),
       ]
   ```
5. Register the bundle in `sim/tools/__init__.py::all_ops`.
6. Add tests under `tests/test_tools_foo.py`.

If the tool advances `sim_time` itself (like `wait.until`, `idle.until`, or `meetings.attend`), declare cost 0 and call `scheduler.advance(...)` inside the handler. The registry reports `cost_minutes` from the actual delta.

If the tool affects the actor's next-poll schedule (like `idle.until` setting `person.next_poll_at` to a later time, or `abandon.current` truncating `person.busy_until`), the driver's tick loop reads those fields and reschedules the next `actor_poll` event accordingly. See `sim/tools/idle.py` and `sim/tools/abandon.py` for the reference patterns.

## Add a metric

Final-evaluator metrics live in `sim/evaluator/metrics.py`. Each metric:

- Is a pure function on a `Path` (the run dir) plus optionally the loaded `eval.yaml` truth dict.
- Returns a `MetricResult` with `tier` in `{"slice_safe", "requires_full_run"}` and `normalized` in `[-1, +1]`.

```python
def my_metric(run_dir: Path) -> MetricResult:
    turns = _load_turns(run_dir)
    raw = ...
    normalized = max(-1.0, min(1.0, ...))
    return MetricResult("my_metric", "slice_safe", normalized, raw, {})
```

Wire it into `sim/evaluator/final.py::evaluate_run` by appending to the `metrics` list (in the appropriate tier branch).

**Scenario-specific signals**: if your metric only applies when the scenario opts in (declares the signal in `eval.yaml`), return `MetricResult(..., contributes=False, detail={"reason": "signal_not_declared"})` for the no-opt-in path. The metric will still appear in the scorecard for transparency but will be excluded from the composite mean. Pattern after `anti_hack_forbidden_external_keywords` / `anti_hack_must_consult_before_decision` / `anti_hack_forbidden_log_work` / `hidden_fact_discovery_rate` / `follow_up_rate` / `appropriate_abandonments_rate` in `metrics.py`.

**Cluster assignment**: every metric needs to be assigned to one of the four skill clusters (`outcomes_achieved`, `decision_quality`, `coordination`, `time_management`). Add your metric's name to the appropriate cluster's `metrics` list in `CLUSTERS` (`sim/evaluator/final.py`). Metrics that aren't in any cluster are flagged in `errors[]` at grade time — won't crash but won't contribute to the composite either.

## Add a per-artifact rubric

In `eval.yaml`:

```yaml
artifacts:
  - id: my_artifact
    description: "What this artifact is, for the ground-truth context block."
    locator:
      kind: email_thread       # or decision_signal
      thread_subject_contains: "your_keyword"
      sender_id: person.tpm
      recipient_id: person.X
    rubric:
      - "Concrete yes/no item one."
      - "Another yes/no item."
      - "Third item."
```

The grader auto-discovers it and runs `score_artifact` (`sim/evaluator/rubrics.py`) which:

1. Locates the artifact body in the final world.
2. Builds an isolated judge prompt with the rubric.
3. Aggregates pass-rate to a `[-1, +1]` score.

Add a new `locator.kind` by extending `_locate_artifact_body` in `rubrics.py`.

## Swap the agent

The driver accepts any object with `decide(briefing) -> ToolCall`. For testing, `ScriptedAgent` works. For Claude, `ReferenceAgent` uses the Anthropic SDK. To plug in another LLM, implement the protocol — that's the entire contract.

## Swap the judge

`sim/evaluator/judge.py::Judge` is a Protocol with one method. `StubJudge` and `AnthropicJudge` are reference implementations. To plug in a different judge, implement `evaluate(*, system_prompt, user_payload) -> RubricVerdict` and pass it to `evaluate_run(..., judge=YourJudge())`. To use a different model for the LLM-judged axes, construct `AnthropicJudge(model="claude-opus-4-7")` (or similar) — Opus is a defensible choice when you want the judge to be distinct from the Sonnet-class agent under test.

## Swap the NPC brain

`sim/npc/brain.py::NpcBrain` is the Protocol with one method: `respond(context: NpcBrainContext) -> NpcBrainOutput`. The default `AnthropicNPCBrain` (in `sim/npc/anthropic_brain.py`) uses Sonnet 4.6 with forced tool-use. To plug in a different model, implement `respond` and pass the brain to `build_runtime(scenario, brain=YourBrain())`.

The context carries everything the brain should see:
- `npc_id`, `persona_role`, `persona_notes`, `knowledge` (from the persona YAML)
- `triggers: list[NpcTrigger]` — accumulated inbound since the NPC's last poll (DMs, mentions, emails), most-recent-first. The brain prioritizes one and acts on it.
- `trigger_kind` and `trigger_payload` — kept as legacy fields for meeting-turn invocations (single-trigger context)
- `context_excerpt` (compact relevant world state — recent messages in the triggering channel, etc.)
- `available_tools` — the NPC's authorized tool schemas, so the brain knows what arg names to use

The output carries `tool_calls` (a list of `ToolCall` — the runtime filters them through `NpcPolicy.allowed_tools` before dispatch; NPCs are single-decision per tick so only the first allowed call runs), optional `speech` (for meeting turns), and a `rationale` string for debugging. Cache by `(scenario_id, seed, event_id)` is provided automatically by `BrainCache`; deterministic replays are free.

## Tests

```bash
pytest tests/                      # full suite, ~370 tests, sub-second
pytest tests/test_evaluator_final.py
pytest tests/ -k npc               # by keyword
pytest tests/test_golden_runs.py   # 6 adversarial scripted-agent goldens
```

All tests stub the LLM. No API key required.
