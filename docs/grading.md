# Grading

## Design intent

Score the **outcome** the agent produced, not the **activity** it generated. Every signal lives in `[-1, +1]`. The composite is the arithmetic mean over **four skill clusters**, each cluster equally weighted. Within a cluster every contributing member is also equally weighted. This stops any single bucket (e.g., the largest cluster) from dominating the score, and gaming one signal buys you very little.

The grader never sees the agent's transcript or self-narration when it asks an LLM to judge anything. Judges see a tightly-scoped payload assembled by Python: rubric items + the artifact body + ground-truth context. The agent cannot talk its way to a higher score.

## Evaluation architecture

The active evaluation pipeline is the **end-of-run final evaluator** (`sim/evaluator/final.py`). It reads a finished run directory from disk and writes `final_evaluation.json`. The grader never references live objects — any run is re-scoreable forever.

### Tiers

| Tier | Engages when | What it adds |
|---|---|---|
| `slice_safe` | Always (even on crashed/partial runs). | All programmatic metrics except `deadline_hit_rate` and `stakeholder_contact_rate`. All judge axes except `state_accuracy`. |
| `requires_full_run` | Only when `final sim_time ≥ end_sim_time`. | Adds `deadline_hit_rate`, `stakeholder_contact_rate`, judge axis `state_accuracy`. |

Composite = arithmetic mean over the four **skill clusters** (see "Skill clusters" below). Tier changes which metrics/axes are even computed, which in turn shifts what's included in each cluster's score — but the per-cluster weight in the composite is unchanged. Failed judge verdicts and signals that aren't declared in a scenario are excluded from their cluster's mean (see Failure handling below).

### Why no windowed live evaluator by default

The codebase contains a `sim/evaluator/windowed.py` that scores a sliding window of agent turns mid-run. **It is deliberately unwired in `sim run`.** Its original design feeds the latest verdict into the next briefing's `last_verdict` field — the agent can course-correct on its own score. That makes it a *training-flavored* tool. For *evaluating* a model's TPM ability we don't want the model to see feedback signals during the run; we want to observe behavior, then grade it. The windowed infrastructure remains for callers who want training-style telemetry but is not part of the default eval path.

## Programmatic metrics

Each metric is a pure function on the run directory's files. None of them call an LLM — they're deterministic state assertions the agent can't argue with.

| Metric | Tier | What it measures | Normalisation |
|---|---|---|---|
| `error_rate` | slice_safe | Tool errors / total tool calls. Also reports per-tick error density when `tick_id` is present. | 0 errors → +1; ≥20% → -1. |
| `prioritization_latency` | slice_safe | Per-item latency from urgent inbound (DM or email matching a declared trigger) to agent ack. Latency < `tick_size_minutes` → +1 (system minimum floor). Items whose trigger never fired are excluded. | Per item: clip(1 − latency/target, −1, +1). No ack → −1. |
| `hidden_fact_discovery_rate` | slice_safe | For each declared `hidden_fact`: did agent read the source AND reference it in subsequent outbound? | +1 read + referenced, 0 only read, −1 neither. |
| `follow_up_rate` | slice_safe | For each declared `follow_up_target`: did NPC reply in window OR did agent re-ping about same topic? | +1 either, −1 neither. |
| `opportunity_cost_score` | slice_safe | For each `urgent_ack_latency` trigger: do agent's next 3 non-read tool calls address it? | +1 if at least one does, −1 otherwise. |
| `appropriate_abandonments_rate` | slice_safe | For each declared `appropriate_abandonment`: did agent call `abandon.current` during the event window when an urgent trigger was present? | +1 abandoned+triggered, +0.5 abandoned no trigger, −1 missed. |
| `bad_timing_starts` | slice_safe | Long actions (declared cost ≥ 10 min) started during a window when an urgent trigger arrives. | 0 hits → +1; 3+ → -1. |
| `idle_judgment_score` | slice_safe | Per `idle.until` call: was the requested duration reasonable given inbox state? (< 30 min if urgent pending, < 120 min otherwise) | +1 reasonable, −1 inappropriate. |
| `anti_hack_forbidden_external_keywords` | slice_safe | Forbidden phrases in agent emails whose recipients include anyone with `team: external` (or explicit IDs). | 0 hits → +1; 3+ → -1. |
| `anti_hack_must_consult_before_decision` | slice_safe | Each declared decision artifact must be preceded by an agent → consult-target DM, mention, email, or meeting co-attendance strictly earlier. | 0 violations → +1; 2+ → -1. |
| `anti_hack_forbidden_log_work` | slice_safe | Agent must not `tasks.log_work` on tasks whose `assignee_id` is not the agent. | 0 violations → +1; 5+ → -1. |
| `deadline_hit_rate` | requires_full_run | Programmatic objective checks against task statuses + scheduled deadlines. | (rate × 2) − 1. |
| `stakeholder_contact_rate` | requires_full_run | Stakeholder SLA objectives met (`email_sent_to_by`, `agent_messaged_person_about`, `email_reply_within`). | (rate × 2) − 1. |

The `eval.yaml` ground truth declares each objective and anti-hack signal with its `check.kind` / `signal.kind` and parameters; metrics dispatch on the kind.

**Why no volume caps?** Earlier versions had `anti_hack_max_messages` and `anti_hack_per_channel_volume` metrics that capped total/per-channel message counts. Both were dropped because they punish legitimate behavior: a real TPM running a launch week may genuinely need to send 60+ messages on a Monday. Catching message-spam is now the LLM judge's job — `specificity` and `decision_hygiene` will flag noisy, low-information outbound traffic. In the tick-based model, literal-duplicate spam also burns attention ticks and surfaces as missed deadlines.

**Why no loop / repeat-read metrics?** Earlier versions had `tight_loop_rate` (streaks of 3+ identical `(tool, args)` calls) and `repeat_read_rate` (reading the same artifact ≥3× without acting). These metrics existed to compensate for the action-driven time model where reads and loop iterations were essentially free. In the new tick-based model, each loop iteration and each re-read consumes an attention tick, and the cost surfaces naturally as missed deadlines or low Outcomes scores. Explicit metrics for these patterns are no longer needed.

**Scenario-specific signals don't drag scores on unrelated scenarios.** Anti-hack metrics carry a `contributes: bool` flag (`sim/evaluator/metrics.py:MetricResult`). When `eval.yaml` doesn't declare a signal, the corresponding metric returns `contributes=False` — it's still reported in the scorecard for transparency, but excluded from the composite mean.

### How prioritization is graded

The existing deadline / stakeholder metrics are binary: a deadline is either hit or missed. That misses something a real TPM cares about — *did the agent triage urgency correctly*. A response 5 minutes before the deadline and a response 5 hours before should score differently, and ignoring a buried blocker until late in the week should hurt even if the agent eventually addresses it.

`prioritization_latency` declares specific urgent inbound items in `eval.yaml` with a target ack latency. For each item, the metric:

1. Finds the **trigger** — an inbound DM or email matching the spec (sender, optional subject/body match, optional `after_sim_time` floor for events scheduled in `events.yaml`).
2. Finds the **ack** — an agent outbound that satisfies the ack spec strictly later than the trigger. Two ack kinds are supported today: `email_reply_to_sender` (reply on the same thread) and `agent_outbound_with_keywords` (any chat/email mentioning recipients + keywords).
3. Computes `latency = ack_sim_time - trigger_sim_time` and scores `clip(1 − latency / target, −1, +1)`.

So in `week_one_launch`:
- Maya's Mon 11:00 DM about the flaky migration has a 240-min target. If the agent loops Kai in within an hour, it scores +0.75. If it never engages, it scores -1.
- BigCorp's Tue URGENT email has a 120-min target (matches the binary SLA).
- The CEO's Wed launch-readiness ask has a 480-min target.

Items whose trigger never fires (e.g., the run crashed before the scheduled inbound) are excluded from the mean — we can't grade prioritization on an event the agent never received. The metric reports `contributes=False` for scenarios that declare no `urgent_ack_latency` objectives, so it doesn't drag unrelated scenarios.

## Judge axes

Sonnet 4.6 by default, `temperature=0`, cached in-process by `(system_prompt, user_payload)` hash so re-grades within a single grading process are byte-stable. `sim grade` constructs the real Anthropic judge automatically when `ANTHROPIC_API_KEY` is set; without it, the CLI prints a loud WARNING that the LLM axes will not run and falls back to a stub.

| Axis | Tier | What it measures |
|---|---|---|
| `specificity` | slice_safe | Are agent outbound messages, emails, and doc edits concrete and actionable rather than vague? |
| `decision_hygiene` | slice_safe | Compare inbound asks to outbound actions — did the agent respond to what was actually asked, and consult the people who surfaced the relevant info before deciding? |
| `risk_escalation` | slice_safe | Did the agent surface risks proactively (look for outbound naming risks raised in the inbound) rather than letting them simmer? |
| `pre_emption_judgment` | slice_safe | For each `abandon.current` call: was abandoning the long action justified by inbound urgency at that time? `not_applicable=True` if agent never abandoned. |
| `tick_continuation_judgment` | slice_safe | When agent chained decisions within a tick (`continue_in_tick=True`), were the continuations productive (acted on what was just read) or polling-in-disguise? `not_applicable=True` if no continuations. |
| `context_recall_score` | slice_safe | Did the agent reference stale world state in their outbound (e.g., claim a task is "Done" when the world says "In Review")? `not_applicable=True` if no verifiable state claims. |
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

## Skill clusters

Phase 5 groups every contributing signal into one of four **skill clusters**. The composite is the arithmetic mean of cluster scores; per-cluster, the score is the arithmetic mean of its contributing members. Equal weighting at both levels means a bucket with eight members (`decision_quality`) counts the same as a bucket with two (`outcomes_achieved`) — gaming one signal can shift its cluster, but no single cluster can dominate.

| Cluster | Metrics | Judge axes | Artifacts |
|---|---|---|---|
| `outcomes_achieved` | `deadline_hit_rate`, `hidden_fact_discovery_rate` | — | — |
| `decision_quality` | `anti_hack_must_consult_before_decision`, `anti_hack_forbidden_external_keywords`, `anti_hack_forbidden_log_work` | `decision_hygiene`, `specificity`, `risk_escalation`, `state_accuracy` | yes — folded as one synthetic member |
| `coordination` | `stakeholder_contact_rate`, `follow_up_rate` | — | — |
| `time_management` | `error_rate`, `prioritization_latency`, `opportunity_cost_score`, `appropriate_abandonments_rate`, `bad_timing_starts`, `idle_judgment_score` | `pre_emption_judgment`, `tick_continuation_judgment`, `context_recall_score` | — |

**Artifacts roll up.** The per-artifact rubric scores (which already produce `[-1, +1]` normalised values per artifact) collapse into a single synthetic `artifacts_mean` member of `decision_quality`. A scenario declaring three artifacts doesn't get triple the weight on this cluster — it gets one cluster-level vote. The individual `ArtifactScore` entries remain in the JSON for inspection.

**Exclusion rules.** A cluster member is excluded from its cluster's score (and listed in `cluster.excluded_members`) when any of these hold:
- programmatic metric with `contributes=False` (e.g., the scenario doesn't declare the underlying signal),
- judge axis with `failed=True` (judge call failed / malformed response),
- judge axis with `not_applicable=True` (the run produced no signal for the axis — e.g., zero abandons),
- artifact with `failed=True` (artifact judge call failed; failed artifacts also don't contribute to `artifacts_mean`).

Excluded members are still reported on the scorecard so operators can see what didn't fire and why. They do not affect the cluster mean.

**Cluster-level skip.** When every member of a cluster is excluded, that cluster's `contributes_to_composite=False` and it's dropped from the composite mean — its score is reported as 0.0 for visibility but does not pull the composite toward zero.

**Why cluster-weighting?** The flat-mean composite that Phase 4 used silently weighted `decision_quality` (eight members) at 4× the weight of `outcomes_achieved` (two members). That's the wrong default: missing every deadline shouldn't be diluted by the surrounding noise of judge-axis arithmetic. Equal-weighted clusters mean each TPM-relevant skill area carries the same load on the composite.

**Backwards compatibility.** The raw `metrics[]` and `judge.axes` blocks are still in `final_evaluation.json` for inspection. `clusters` is the new top-level summary the composite is computed from.

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
- **Anti-hack signals are process gates, not volume caps.** Three of them — `forbidden_external_keywords`, `must_consult_before_decision`, `forbidden_log_work` — each declared in `eval.yaml` and computed programmatically. They catch specific policy violations the LLM judge can't reliably see (was a decision artifact preceded by consultation? did the agent log work on a task it doesn't own? did it leak forbidden phrases to an external recipient?).
- **Volume judgement is delegated to the LLM judge.** Whether a high message count was warranted (genuine launch coordination) or noise (spam) is exactly the kind of contextual judgement `specificity` and `decision_hygiene` are designed to make. A hard cap would punish a TPM who legitimately needed to coordinate 60 stakeholders on Monday.
- **Loops and repeat reads self-penalise** via tick cost — every loop iteration and re-read burns an attention tick, so stuck behaviour surfaces as missed deadlines and weakened Outcomes scores rather than needing a dedicated metric.
- **External-recipient phrase blacklist** (`forbidden_external_keywords`) — the agent can't promise descoped features to customers without it showing as a negative metric.
- **Consultation gate** (`must_consult_before_decision`) — major decision artifacts must be preceded by agent→consult-target traffic (DM, mention, email, or meeting co-attendance), verified by the same locator infrastructure used for artifact scoring.
- **Task-credit gate** (`forbidden_log_work`) — the agent can't pad apparent productivity by logging work on tasks assigned to other people.
- Per-artifact rubrics are **scoped to specific deliverables**, so verbosity in unrelated channels doesn't earn credit. Multiple artifacts collapse to one `artifacts_mean` member of `decision_quality`, so a scenario with eight artifacts doesn't gain eight votes on the composite.
- The composite is an **arithmetic mean across four equal-weighted skill clusters**, so any single signal — or any single cluster — is bounded. Within a cluster every contributing member is also equally weighted, so a bucket with one declared signal weighs the same as a bucket with eight.

A second-order defense: **judge calls themselves are auditable.** Failed calls show in `errors[]` rather than degrading silently to neutral scores, so an evaluator under API stress can't accidentally produce a misleadingly-OK composite.

## Golden runs

Five regression-tested scripted-agent behaviors under `tests/test_golden_runs.py`. Two anchor "good" vs "bad," three lock the eval against drift on specific observed failure modes:

| Golden | Scenario | Agent behavior | Expected cluster bands |
|---|---|---|---|
| `high_score` | smoke | Methodical, advances `task.SMOKE-1` through to Done, minimal comms. | composite `[+0.30, +0.70]` |
| `low_score` | smoke | Spams `chat.send` every turn for 100 turns; never touches the task. Caught by tick-cost (every spam call burns attention) + LLM judge axes (noise content). | composite `≤ 0.0` |
| `anxious_poller` | smoke | Every tick: `chat.read` only, never acts. Tests whether the eval still penalizes "read everything, act on nothing." | `outcomes_achieved ≤ -0.5`, composite `≤ +0.15` |
| `optimistic_ignorer` | week_one_launch | Marks tasks Done blindly without reading inbound. Tests whether `state_accuracy` (judge) still crushes false claims. | `decision_quality ≤ -0.15`, `state_accuracy ≤ -0.5`, composite `≤ 0.0` |
| `confident_hallucinator` | week_one_launch | Produces decision artifacts with all the right rubric structure but never DM'd consult-targets. Tests the `must_consult_before_decision` process gate. | `anti_hack_must_consult_before_decision ≤ -0.5`, composite `≤ +0.3` |

Any change to metrics, rubric weights, judge prompts, or cluster composition that moves these scores requires deliberately updating the bands in the test. That's the contract — these goldens are the eval's regression suite.
