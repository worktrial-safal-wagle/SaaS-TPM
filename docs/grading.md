# Grading

## Design intent

Score the **outcome** the agent produced, not the **activity** it generated. Every signal lives in `[-1, +1]`. The composite is an arithmetic mean over all contributing axes — no single axis dominates, and gaming one buys you very little.

The grader never sees the agent's transcript or self-narration when it asks an LLM to judge anything. Judges see a tightly-scoped payload assembled by Python: rubric items + the artifact body + ground-truth context. The agent cannot talk its way to a higher score.

## Two layers

### 1. Windowed live evaluator (`sim/evaluator/windowed.py`)

Runs **during** the sim. Every `window_size` agent turns (default 10), a small judge (Haiku 4.5) scores the recent window on a single principle:

> *"Is the agent moving projects forward with minimum noise?"*

Output: `{score in [-1, +1], category, rationale}`. The most recent verdict is included in the **next** briefing's `last_verdict` field — the agent can course-correct mid-run.

We picked a single-principle rubric here on purpose. Multi-axis judges drift; one principle stays sharp.

### 2. End-of-run final evaluator (`sim/evaluator/final.py`)

Runs **after** the sim. Reads the run directory from disk and writes `final_evaluation.json`. The grader never references live objects, so any run is re-scoreable forever.

Two tiers:

| Tier | Engages when | What it adds |
|---|---|---|
| `slice_safe` | Always (even on crashed/partial runs). | `error_rate`, `turns_per_sim_hour`, `repeat_read_rate`, `anti_hack_max_messages`, judge axes `specificity` / `decision_hygiene` / `risk_escalation`. |
| `requires_full_run` | Only when `final sim_time ≥ end_sim_time`. | Adds `deadline_hit_rate`, `stakeholder_contact_rate`, judge axis `state_accuracy`. |

Composite = arithmetic mean over all contributing axes. Tier changes the *denominator*, not the per-axis weight.

## Programmatic metrics

Each metric is a pure function on the run directory's files.

| Metric | What it measures | Normalisation |
|---|---|---|
| `error_rate` | Tool errors / total tool calls. | 0 errors → +1; ≥20% → -1. |
| `turns_per_sim_hour` | Activity density. | 5–15 ideal → +1; <2 → -0.3; >30 → -1. |
| `repeat_read_rate` | Reading the same artifact ≥3× without acting. | 0 bad reads → +1; high → -1. |
| `anti_hack_max_messages` | Outbound messages vs `eval.yaml` cap. | Below cap → mild taper; over → linear to -1 at 2× cap. |
| `deadline_hit_rate` | Programmatic objective checks. | (rate × 2) − 1. |
| `stakeholder_contact_rate` | Stakeholder SLA objectives met. | (rate × 2) − 1. |

The `eval.yaml` ground truth declares each objective with its `check.kind` and parameters; metrics dispatch on `kind`.

## Judge axes

Sonnet 4.6 by default, `temperature=0`, cached by prompt hash so re-grades are byte-stable.

| Axis | Tier | What it measures |
|---|---|---|
| `specificity` | slice_safe | Are outputs concrete and actionable rather than vague? |
| `decision_hygiene` | slice_safe | Are decisions grounded in evidence and stakeholder consult? |
| `risk_escalation` | slice_safe | Are risks surfaced proactively to leadership? |
| `state_accuracy` | requires_full_run | Do the agent's claims match the actual world state? |

The axes payload is **redacted** before the judge sees it. It includes tool-call counts, the final task board, and truncated outbound message/email excerpts. It does **not** include briefings, rationales, or anything the agent said about itself.

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

## Anti-hack story

Bulk score is **programmatic state assertions** the agent can't fake.
The judge **never sees the agent transcript** — only a redacted view assembled by Python.
**High volume is penalised** (`turns_per_sim_hour` saturates negative).
**Repeat reads without action are penalised** (`repeat_read_rate`).
Per-artifact rubrics are **scoped to specific deliverables**, so verbosity in unrelated channels doesn't help.
The composite is an **arithmetic mean across many axes**, so any single axis is bounded — and the anti-hack signals are *part of the metric set*, not a separate penalty layer, which means they can't be argued around.

A test (`tests/test_evaluator_final.py::test_judge_prompt_isolation`) explicitly asserts that briefings never leak into judge prompts.

## Golden runs

Two regression-tested behaviors under `tests/test_golden_runs.py`:

| Golden | Agent behavior | Expected composite |
|---|---|---|
| `high_score` | Methodical, advances `task.SMOKE-1` through to Done, minimal comms. | `[+0.30, +0.70]` |
| `low_score` | Spams `chat.send` every turn for 100 turns; never touches the task. | `≤ -0.20` |

Any change to metrics, rubric weights, or judge prompts that moves these scores requires deliberately updating the band in the test. That's the contract.
