# Grading

## Design intent

Score the **outcome** the agent produced, not the **activity** it generated. Every signal lives in `[-1, +1]`. The composite is an arithmetic mean over all contributing axes — no single axis dominates, and gaming one buys you very little.

The grader never sees the agent's transcript or self-narration when it asks an LLM to judge anything. Judges see a tightly-scoped payload assembled by Python: rubric items + the artifact body + ground-truth context. The agent cannot talk its way to a higher score.

## Evaluation architecture

The active evaluation pipeline is the **end-of-run final evaluator** (`sim/evaluator/final.py`). It reads a finished run directory from disk and writes `final_evaluation.json`. The grader never references live objects — any run is re-scoreable forever.

### Tiers

| Tier | Engages when | What it adds |
|---|---|---|
| `slice_safe` | Always (even on crashed/partial runs). | `error_rate`, `turns_per_sim_hour`, `repeat_read_rate`, 5 anti-hack signals, judge axes `specificity` / `decision_hygiene` / `risk_escalation`. |
| `requires_full_run` | Only when `final sim_time ≥ end_sim_time`. | Adds `deadline_hit_rate`, `stakeholder_contact_rate`, judge axis `state_accuracy`. |

Composite = arithmetic mean over all contributing axes. Tier changes the *denominator*, not the per-axis weight. Failed judge verdicts and signals that aren't declared in a scenario are excluded from the mean (see Failure handling below).

### Why no windowed live evaluator by default

The codebase contains a `sim/evaluator/windowed.py` that scores a sliding window of agent turns mid-run. **It is deliberately unwired in `sim run`.** Its original design feeds the latest verdict into the next briefing's `last_verdict` field — the agent can course-correct on its own score. That makes it a *training-flavored* tool. For *evaluating* a model's TPM ability we don't want the model to see feedback signals during the run; we want to observe behavior, then grade it. The windowed infrastructure remains for callers who want training-style telemetry but is not part of the default eval path.

## Programmatic metrics

Each metric is a pure function on the run directory's files. None of them call an LLM — they're deterministic state assertions the agent can't argue with.

| Metric | Tier | What it measures | Normalisation |
|---|---|---|---|
| `error_rate` | slice_safe | Tool errors / total tool calls. | 0 errors → +1; ≥20% → -1. |
| `turns_per_sim_hour` | slice_safe | Activity density. | 5–15 ideal → +1; <2 → -0.3; >30 → -1. |
| `repeat_read_rate` | slice_safe | Reading the same artifact ≥3× without acting. | 0 bad reads → +1; high → -1. |
| `anti_hack_max_messages` | slice_safe | Outbound messages vs `eval.yaml` cap. | Below cap → mild taper; over → linear to -1 at 2× cap. |
| `anti_hack_per_channel_volume` | slice_safe | Per-channel-per-day cap; one violation per `(channel, day)` bucket exceeded. | 0 violating buckets → +1; 5+ → -1. |
| `anti_hack_forbidden_external_keywords` | slice_safe | Forbidden phrases in agent emails whose recipients include anyone with `team: external` (or explicit IDs). | 0 hits → +1; 3+ → -1. |
| `anti_hack_must_consult_before_decision` | slice_safe | Each declared decision artifact must be preceded by an agent → consult-target DM, mention, or email strictly earlier. | 0 violations → +1; 2+ → -1. |
| `anti_hack_forbidden_log_work` | slice_safe | Agent must not `tasks.log_work` on tasks whose `assignee_id` is not the agent. | 0 violations → +1; 5+ → -1. |
| `deadline_hit_rate` | requires_full_run | Programmatic objective checks against task statuses + scheduled deadlines. | (rate × 2) − 1. |
| `stakeholder_contact_rate` | requires_full_run | Stakeholder SLA objectives met (`email_sent_to_by`, `agent_messaged_person_about`, `email_reply_within`). | (rate × 2) − 1. |

The `eval.yaml` ground truth declares each objective and anti-hack signal with its `check.kind` / `signal.kind` and parameters; metrics dispatch on the kind.

**Scenario-specific signals don't drag scores on unrelated scenarios.** Anti-hack metrics carry a `contributes: bool` flag (`sim/evaluator/metrics.py:MetricResult`). When `eval.yaml` doesn't declare a signal, the corresponding metric returns `contributes=False` — it's still reported in the scorecard for transparency, but excluded from the composite mean. The smoke scenario only declares `max_messages_total`; the four newer signals all report `signal_not_declared` and don't influence its composite.

## Judge axes

Sonnet 4.6 by default, `temperature=0`, cached in-process by `(system_prompt, user_payload)` hash so re-grades within a single grading process are byte-stable. `sim grade` constructs the real Anthropic judge automatically when `ANTHROPIC_API_KEY` is set; without it, the CLI prints a loud WARNING that the LLM axes will not run and falls back to a stub.

| Axis | Tier | What it measures |
|---|---|---|
| `specificity` | slice_safe | Are agent outbound messages, emails, and doc edits concrete and actionable rather than vague? |
| `decision_hygiene` | slice_safe | Compare inbound asks to outbound actions — did the agent respond to what was actually asked, and consult the people who surfaced the relevant info before deciding? |
| `risk_escalation` | slice_safe | Did the agent surface risks proactively (look for outbound naming risks raised in the inbound) rather than letting them simmer? |
| `state_accuracy` | requires_full_run | Do the agent's outbound claims about world state match the actual final world? |

The axes payload is **redacted** before the judge sees it. It includes:
- agent outbound chats and emails (truncated to 30 × 300 chars / 15 × 400 chars)
- **agent inbound chats and emails** — DMs to the agent, @-mentions of the agent, and emails to/cc the agent. Added in Phase 2 so the judge can verify `decision_hygiene` against what the agent was actually asked, not just against what it sent
- tool-call counts and the final task board

It does **not** include briefings, internal reasoning, or anything the agent said about itself. Enforced by `tests/test_evaluator_final.py::test_judge_prompt_isolation`.

Every judge prompt uses **forced Anthropic tool-use with strict schemas** — every required axis key must be present, the response must start with `{`, no preamble or markdown fences are accepted. This stops a class of silent failures where the model returned parseable but schema-violating JSON.

## Per-artifact rubrics

`eval.yaml` can declare specific **decision artifacts** with 3–5 yes/no rubric items each. For week_one_launch:

- `ceo_launch_readiness_email`
- `audit_log_date_decision`
- `bigcorp_escalation_reply`

For each artifact, the grader:

1. Locates the artifact body in the final world (e.g., the most recent email in a thread by sender X with subject containing "launch").
2. Builds a prompt with `{artifact_body, rubric_items, ground_truth_context}` — nothing else.
3. The judge returns a per-item pass/fail list.
4. Aggregates as pass-rate and scales to `[-1, +1]`.

The artifact mean is averaged with the judge's free-form `decision_hygiene` score to produce the final axis. This sharpens an otherwise diffuse axis with concrete artifact checks.

## Failure handling

The judge can fail for real reasons: rate-limited API call, malformed JSON, missing axis keys. Earlier versions caught these with a bare `except` and returned `score=0.0`, which silently blended a fake neutral into the composite. After Phase 1d:

- `RubricVerdict.failed: bool` marks an unparseable or unrecoverable judge call.
- `AnthropicJudge` retries up to 3 attempts with exponential backoff on transient API errors. Parse errors don't retry (temperature=0 means the bytes are deterministic; retrying just burns tokens).
- The composite **excludes** failed axes — they aren't blended in as 0.0.
- Failures surface in `final_evaluation.json.errors[]` so operators see exactly which axis or artifact failed.
- Recovery path: if the axes judge fails but per-artifact judges succeed, `decision_hygiene` recovers from the artifact mean instead of being dropped entirely. We don't throw away artifact signal because of an unrelated axes failure.

The artifact rubric scoring has parallel infrastructure (`ArtifactScore.failed`, `failure_reason`). A failed artifact judge is excluded from the artifact mean and surfaced in `errors[]` — it never silently becomes a `pass_rate=0.5` placeholder.

## Anti-hack story

The bulk of the score is **programmatic state assertions** the agent can't fake. The LLM judge is sandboxed by design.

- **Judge never sees the agent transcript** — only a redacted view assembled by Python. Enforced by `test_judge_prompt_isolation`.
- **Anti-hack signals are part of the metric set, not a separate penalty layer.** Five of them currently — `max_messages`, `per_channel_volume`, `forbidden_external_keywords`, `must_consult_before_decision`, `forbidden_log_work` — each declared in `eval.yaml` and computed programmatically from the run logs. They can't be argued around because they're not LLM calls.
- **High volume is penalised** (`turns_per_sim_hour` saturates negative; `anti_hack_max_messages` taper; `anti_hack_per_channel_volume` per-day bucket cap).
- **Repeat reads without action are penalised** (`repeat_read_rate`).
- **External-recipient phrase blacklist** (`forbidden_external_keywords`) — the agent can't promise descoped features to customers without it showing as a negative metric.
- **Consultation gate** (`must_consult_before_decision`) — major decision artifacts must be preceded by agent→consult-target traffic, verified by the same locator infrastructure used for artifact scoring.
- **Task-credit gate** (`forbidden_log_work`) — the agent can't pad apparent productivity by logging work on tasks assigned to other people.
- Per-artifact rubrics are **scoped to specific deliverables**, so verbosity in unrelated channels doesn't earn credit.
- The composite is an **arithmetic mean across many axes**, so any single axis is bounded.

A second-order defense: **judge calls themselves are auditable.** Failed calls show in `errors[]` rather than degrading silently to neutral scores, so an evaluator under API stress can't accidentally produce a misleadingly-OK composite.

## Golden runs

Two regression-tested behaviors under `tests/test_golden_runs.py`:

| Golden | Agent behavior | Expected composite |
|---|---|---|
| `high_score` | Methodical, advances `task.SMOKE-1` through to Done, minimal comms. | `[+0.30, +0.70]` |
| `low_score` | Spams `chat.send` every turn for 100 turns; never touches the task. | `≤ -0.20` |

Any change to metrics, rubric weights, or judge prompts that moves these scores requires deliberately updating the band in the test. That's the contract.
