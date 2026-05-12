# SaaS-TPM Evaluation Environment — Work Trial Writeup

## TL;DR

The starting point was a simulation environment for evaluating LLM agents as Technical Program Managers. It worked, but it had a fundamental problem we didn't see until we'd done a lot of "fixing" around the edges: **the agent could freeze time by thinking**. That single property made the eval metrics into a patchwork of proxies trying to detect symptoms of an underlying architectural choice.

The real arc of this project went like this:

1. **Fix the obvious holes in the eval.** Anti-hack signals weren't wired right. Judge only saw one side of the conversation. Failed judge calls silently scored 0.0. These took a few rounds to get clean.
2. **Improve simulation realism.** NPCs were always available (no meeting-busyness). Error messages didn't tell the agent what they did wrong. The agent could construct DM channel IDs in a way that always failed.
3. **Question whether the metrics were measuring the right thing.** Volume caps punished legitimate communication. Efficiency metrics punished efficient agents. We dropped several metrics in favor of letting the LLM judge handle quality assessment.
4. **Notice the deeper problem.** Most of our remaining "metrics" existed to detect agents wasting time — `repeat_read_rate`, `tight_loop_rate`, parts of `prioritization_latency`. These are symptoms of a sim that lets the agent waste time at all.
5. **Architect a fix.** Top-down tick-based simulation where time advances regardless of agent action. Eval simplifies dramatically because the world enforces consequences.
6. **Implement it.** Currently in flight: 8 sim phases + 7 eval phases. 337 tests passing as of writing. Sonnet, Opus, and the ability to compare them apples-to-apples is the validation target.

This document walks through what we built, what we changed our minds about, and what we learned.

---

## Part 1 — Starting State

The codebase implemented:

- A discrete-event simulator with a global heap of `(fire_at, event_id)` events.
- An agent driver that loops: build briefing → ask agent for next tool call → dispatch → advance scheduler by tool's declared cost → repeat.
- NPCs that reacted to inbound messages with sampled lognormal delays, business-hours gating, and policy-restricted tool allow-lists.
- An evaluator that read a finished run from disk, computed programmatic metrics, called an LLM judge for qualitative axes, and produced a composite score in `[-1, +1]`.
- Two scenarios: `smoke` (1-day toy task) and `week_one_launch` (5-day launch week with 9 NPCs, scheduled inbound, decision artifacts).

The initial grade against spec was B+. The eval architecture was sound — multi-tier (slice_safe / requires_full_run), redacted judge payloads, per-artifact rubrics — but had several correctness bugs and several conceptual gaps. We started with the bugs.

---

## Part 2 — Phase-by-Phase Changes

### Phase 1 — Eval Correctness Fixes

**Problems found**:

- `anti_hack_max_messages` and three other anti-hack metrics were declared but only one was wired into the evaluator.
- The judge's "decision_hygiene" axis only saw the agent's outbound — not the inbound the agent was supposed to be responding to. Verdicts were essentially "is this prose specific" rather than "did the agent respond to what was asked."
- When the judge LLM call failed (rate limit, malformed JSON, missing axis key), the evaluator caught the exception and substituted score=0.0. This silently averaged a fake neutral into the composite. A model whose grader was rate-limited could produce a misleadingly OK score.

**Fixes**:

- All four anti-hack signals wired with locator infrastructure that resolves declarations like "this decision artifact must be preceded by a DM to person.kai" to actual reads of the world state.
- Judge payload extended to include `agent_inbound_chat_excerpts` (DMs to the agent, @-mentions of the agent) and `agent_inbound_email_excerpts` (emails to/cc the agent), truncated. The judge can now verify decision_hygiene against what the agent was actually asked.
- `RubricVerdict.failed: bool` added. Failed judge calls retry up to 3 times with exponential backoff for transient API errors; parse errors don't retry (temperature=0 means the bytes are deterministic). The composite excludes failed axes. Failures surface in `final_evaluation.json.errors[]` for operator visibility.

**Trade-off**: We considered keeping the silent-0.0 fallback for operational simplicity. Rejected — silent failures producing nice-looking scores under API stress is a far worse property than visible failures producing partial scores. The composite mean dynamically excluding failed axes is the cleaner contract.

### Phase 2 — Judge Sees Both Sides

The judge axes were previously built from outbound-only payloads. Adding inbound DMs/mentions/emails meant the `decision_hygiene` axis could now actually evaluate "did the agent answer what was asked?" rather than just "is the agent's prose specific?"

**Implementation detail**: The judge payload is *redacted* — it doesn't include the agent's briefing, internal reasoning, or self-narration. Enforced by `test_judge_prompt_isolation`. Strict tool-use schema forces every required axis key to be present; the judge can't return a parseable-but-schema-violating JSON.

### Phase 3 — LLM-Driven NPCs

NPCs were previously scripted to fire specific reactions to specific triggers. We replaced this with `AnthropicNPCBrain` (Sonnet 4.6) running per-persona. Each NPC reaction is now a real LLM call into a tool-use loop with policy-restricted available tools.

**Failure modes encountered**:

- Anthropic rate limits with no retry → silent NPC dropouts. Fix: 5-attempt retry with exponential backoff, visible failure logging.
- Sonnet hallucinated tool argument names (`to_user_id` instead of `recipient_id` for `chat.dm`). Fix: pass tool schemas in the NPC brain context so the LLM sees the actual API shape.

**Trade-off**: Real LLM NPCs cost API spend and add wall-clock latency per run. Considered keeping scripted NPCs as the default and making LLM NPCs opt-in. Rejected — scripted NPCs make the eval a much weaker test of TPM ability because the world is too predictable. The agent should have to coordinate with realistic-feeling humans, not state machines.

### Phase 4 — Cost Calibration

A series of calibration fixes that emerged from looking at actual run logs:

**Read costs**. Reads were free (0 sim-min). Found agents (especially Sonnet) doing 300+ `chat.read` calls per run — refreshing inbox over and over. Set reads to 1-2 sim-min each. This makes "anxious polling" a measurable behavior.

**Failed-action costs**. Failed tool calls advanced scheduler by 0 sim-min — agents could loop on a broken call forever. Found one run that wasted 136 of 250 turns looping `meetings.attend` on an already-ended meeting. Set failure cost to 1 sim-min (later, in tick model: full declared cost of the attempted action).

**`tight_loop_rate` metric**. New metric: % of turns in streaks of 3+ identical `(tool, args)` calls. Catches successful spam (repeated identical sends) and failed loops. Excludes `tasks.log_work` (chunked-work is legitimate). Later retired in favor of tick-cost surfacing the same behavior naturally.

**Anti-hack tripwire semantics**. Anti-hack signals had been padding scores: a run with zero violations got +1 on every signal, dragging a bad run's composite upward. Changed semantics so anti-hack metrics contribute to the composite only when violated. Non-violation is "no signal" not "+1."

**Briefing preserves full read content**. Agents were repeat-reading the same artifact 5+ times because the briefing's summary truncated bodies mid-sentence. The agent didn't recognize they'd already seen it. Fix: preserve the full content of the last 5 read calls verbatim in the briefing.

### Phase 5 — Documentation Catchup

Updated `docs/grading.md` and `docs/architecture.md` to reflect the new metric set, the tier system, and the judge architecture. Most decisions had been made in commits without a corresponding doc update.

---

## Part 3 — The Volume-Cap Realization

We had an anti-hack metric called `anti_hack_max_messages` that penalized agents for sending more than a declared cap of outbound chat messages over the week.

**The argument it was made on**: agents that spam are gaming the eval. Cap the volume.

**The pushback**: a real TPM coordinating a launch week may genuinely need to send 60+ messages on a Monday. The metric punishes legitimate behavior. The line between "spam" and "active coordination" isn't volumetric — it's qualitative (was each message meaningful?).

We dropped both `anti_hack_max_messages` and `anti_hack_per_channel_volume`. The argument:

- *Quality* of communication is what we care about. That's exactly what the LLM judge's `specificity` and `decision_hygiene` axes are designed to assess.
- *Literal duplicate-spam* (same `chat.send` 100 times) is caught by `tight_loop_rate`.
- *Hard volumetric caps* test "did the agent memorize the rule" not "did the agent communicate effectively."

This was a pivotal mental shift. The eval moved from **rule-based** (don't exceed X messages) to **outcome-based** (was the communication effective). It also made us notice how many other metrics were rule-based proxies for things that should be judged qualitatively.

The same instinct, expressed by the work-trial reviewer: *"the agent judge axis should be in charge of the anti-hack metrics."* That framing is the right one. Programmatic anti-hack signals should be reserved for **process gates** (did they consult before deciding? did they leak forbidden phrases to externals? did they log work on others' tasks?), not for *volume*.

---

## Part 4 — Simulation Realism Fixes

Three issues that turned out to be **simulation bugs**, not eval gaps. The distinction matters: when the world doesn't enforce a constraint, adding an eval metric to penalize the violation is patching the wrong layer.

### 4.1 — NPCs busy during meetings

NPCs in scheduled meetings would still reply to DMs within 1-2 sim-min. A real coworker in a 60-min meeting can't do that. Without this, the agent could DM Kai mid-launch-review and get an instant thoughtful reply — the "don't interrupt Kai during the meeting" judgment couldn't be tested.

Fixed by adding a `_push_past_meetings` helper in the NPC reaction scheduler that checks the NPC's calendar; if `fire_at` lands inside a meeting they're attending, push to meeting-end + a small "catching up" jitter.

Later, in the tick-based rewrite, this helper was deleted because the tick model + `busy_until` enforces meeting absence by construction.

### 4.2 — Validation error messages

When Sonnet hallucinated an incomplete `docs.create` call (missing the required `body` field), the error returned was just `"invalid args: Field required"` with no indication of *which* field. Sonnet got stuck retrying the same incomplete args 10 times in a row.

Pydantic's `ValidationError` has rich detail — field path, error type, expected type — and we were throwing all of it away to surface only the bare `msg`. Fix: format `<field>: <msg> (type=<type>)` for every error, capped at 5 entries.

This was eval-environment quality: penalizing the agent for not guessing the missing field from a useless error is testing the wrong thing.

### 4.3 — DM channel ID canonicalization

DM channels in our system are named with the participants sorted alphabetically: `dm.<lo>__<hi>`. Sonnet, naturally reasoning from the agent's perspective ("my DM with Kai = `dm.<me>__<them>`"), constructed `dm.person.tpm__person.kai` and got "unknown channel" — because canonical form is `dm.person.kai__person.tpm`.

Sonnet wasted 26 turns on this exact pattern in one run.

Fix: `chat.read` and `chat.mark_read` accept an optional `recipient_id` instead of `channel_id`. The tool resolves to the canonical DM channel for the caller-recipient pair. Penalizing the agent for thinking about DMs from the natural ("my conversation with X") direction was testing recall of an arbitrary internal convention, not TPM ability.

---

## Part 5 — Eval Gap vs Simulation Gap (the framing)

A useful lens that emerged from these debates:

> **Eval = "did the agent's actions produce good outcomes?"** — reads a finished run, scores what happened.
>
> **Simulation = "do actions have realistic consequences?"** — produces the world the agent acts on, so consequences emerge naturally.

When the world fails to enforce a real-world constraint:

- *Eval-patching approach*: add a metric that scores "did the agent obey the constraint."
- *Sim-fixing approach*: make the world actually impose the constraint.

The simulation-fix is structurally better because:

- One sim change captures many scenarios; one eval metric is bound to specific declarations.
- The agent *feels* the consequence (their deadline slips because Kai didn't reply in time) rather than just losing a score.
- Compound effects emerge naturally — slow reply → late summary → CEO unhappy → judge state_accuracy reflects it.

We applied this lens to other open questions and kept finding it useful. The big one was time itself.

---

## Part 6 — Bottom-Up vs Top-Down Time (the central realization)

This is the deepest conceptual move in the project.

### What we had: bottom-up

In the original architecture, **the agent drives time forward**. Each tool call has a declared sim-time cost. The scheduler only advances when the agent acts.

Implications:
- If the agent deliberates for 30 wall-clock seconds without calling a tool, zero sim-time passes.
- The world is *frozen* during agent deliberation. Inbound piles up only when the agent's next clock-advancing action passes the inbound's `fire_at`.
- An agent that polls Slack 100 times pays 100 sim-min total at 1-min reads — and during all 100 of those reads, the world is paused.

This made the eval awkward. Several metrics existed *purely* to detect agents wasting time:

- `repeat_read_rate` — reading the same artifact 3+ times without acting
- `tight_loop_rate` — streaks of identical calls
- Most of `prioritization_latency` — latency is "generated by" the agent's choice of tool calls between trigger and response

All of these are *proxies for "the agent isn't behaving like real time is passing."*

### What this misses

The single most important TPM skill — **time management under pressure** — was largely uncapturable. A real TPM doesn't get to freeze time while they think. They feel the deadline. They drop balls when overloaded. They have to triage *because their attention is finite per real hour*.

Our bottom-up sim had no equivalent of this finiteness.

### The reframe: top-down

The clock ticks at a fixed cadence (default 15 sim-min) regardless of agent action. The agent is *polled* each tick. If they don't act, time still passes.

Implications:
- The agent can't freeze time by thinking.
- The world is moving while the agent deliberates — inbound piles up, deadlines approach.
- "Anxious polling" naturally consumes attention budget that could have been spent on real work.
- "Long actions" lock the agent for multiple ticks during which they can't react to inbound — unless they pre-empt (with a cost).

This is honest to how the job actually feels.

### What changes in the eval

Several metrics become **redundant** in the top-down model:

- `repeat_read_rate` — re-reading still costs a tick of attention. Doing it 5 times means missing 5 ticks of other things. The cost is automatic; we don't need a separate metric.
- `tight_loop_rate` — same story. Loop iterations burn ticks. They surface as missed deadlines.
- The "did the agent waste turns" *concept* dissolves — the world enforces that wasted ticks are real wasted minutes.

Several skills become **newly testable**:

- *Pre-emption judgment* — does the agent abandon a long action when something urgent arrives? Real TPMs do this; we couldn't test it before.
- *Dropped-ball rate* — when an NPC is overloaded, they don't reply to everyone. Does the agent follow up? Only meaningful when the world can actually fail to respond.
- *Idle-duration judgment* — does the agent set sensible focus blocks, or do they block off 4 hours while a fire is burning?
- *Bad-timing starts* — starting a 30-min draft right before a known urgent event arrives is a measurable planning error.

### Trade-off acknowledged

Bottom-up's biggest strength is **action-cost realism**. A 1-min read and a 10-min write are correctly priced relative to each other; that gradient does real work. Top-down at a coarse tick (say 60 min) loses some of this gradient.

We resolved this by:
1. **Keeping fine-grained per-tool costs** even within a tick budget. Cheap reads + expensive writes still consume differently.
2. **Variable tick size** — operator-configurable. Different tick sizes test different skills (see Part 8).

### Why we picked 15-min default

A 15-min tick yields 480 ticks per week. Fine enough to model real-time pressure but coarse enough to be tractable. A real TPM probably checks Slack every 10-20 minutes during focused work — 15 matches that intuition.

Real-time tasks (incident response) would benefit from 1-5 min ticks. Strategic planning would benefit from 1-4 hour ticks. The tick size IS the cadence of the role being tested.

---

## Part 7 — Trial and Error (the things we walked back)

### "Just measure throughput"

`turns_per_sim_hour` was an early metric meant to detect thrashing. Removed because it punished *efficient* agents. Opus completing the week in 270 efficient turns scored -0.19 on this metric because 2.25 turns/hour fell below the "5-15 ideal" band — even though it had hit every deadline.

Lesson: metrics tied to *activity rates* punish efficient behavior. Tie to *outcomes*.

### "Cap message volume to prevent spam"

`anti_hack_max_messages` and `anti_hack_per_channel_volume`. Dropped per the analysis in Part 3.

Lesson: volume isn't a quality signal. Quality is a quality signal. Use the LLM judge for quality.

### "Tight loops are bad"

`tight_loop_rate` survives in bottom-up but is being retired in top-down. The metric is correct given bottom-up; it's just made redundant by top-down's automatic cost surfacing.

Lesson: some metrics are compensating for architectural choices. Better to fix the architecture.

### "Score the agent's strategy via more axes"

We considered adding many specialized judge axes (e.g., separate "negotiation_quality", "delegation_quality"). Decided against — diffuse axes are hard to evaluate reliably even by a strong judge. We instead sharpen `decision_hygiene` with per-artifact rubrics (deterministic yes/no items) and let the judge handle a small number of broad axes.

Lesson: fewer well-defined axes beat many vaguely-defined ones.

### "Score is one number"

Initially the composite was a flat mean across many heterogeneous axes. Realized this loses information — a model that's great at decision quality but bad at time management gets the same composite as the inverse. Restructuring (in flight) groups metrics into 4 skill clusters: Outcomes Achieved / Decision Quality / Coordination / Time Management. Per-cluster scoring makes model differences actionable.

Lesson: a single score is a summary, not a diagnosis.

### "API latency translates to sim-time"

Considered making LLM API wall-clock latency map to sim-time directly ("if you think for 5 wall-clock seconds, that's 5 sim-seconds"). Rejected — this conflates LLM API responsiveness with model ability. A model running on a throttled API endpoint would score worse than the same model on a fast endpoint. Not a property of the model's TPM judgment.

Lesson: keep model evaluation orthogonal to infrastructure variance.

### Opus run got *slower* after sim fixes

A particularly interesting data point. After fixing NPC meeting-busy behavior and improving validation error messages, we re-ran Opus expecting it to be at least as efficient. Instead it used 380 turns vs 270 in the prior run, with 32 failures vs 17.

Looking at the failures: Opus hallucinated more IDs in this run (`thread.audit_retention`, `cal.launch_readiness`, `person.casey`, `dm.tpm_kai` — all made-up). Our `suggest_id` helper offered close matches but Opus burned turns retrying invented IDs.

The lesson isn't about the specific run — model variance can produce different runs on different seeds. The lesson is *the eval environment needs the same failure-recovery affordances across model behavior variation*. We should accept that models will hallucinate IDs; the question is whether the environment makes recovery cheap.

### Multi-turn vs single-turn deliberation

The current bottom-up model treats each agent turn as a self-contained "one tool call." Considered allowing agents to return multiple tool calls per turn (batch). Decided this leaks an action-cost gradient (cheap actions stop costing anything). In the tick model, the equivalent question is "how many decisions per tick" — settled with opt-in continuation (cap 4 per tick).

Lesson: every "agent batches actions" optimization tends to muddle the time-cost signal. Be explicit about how decisions consume attention.

---

## Part 8 — Tick Size as Skill Selector (the late realization)

While brainstorming the Level 3 implementation, a powerful framing came up:

> Tick size isn't just a difficulty knob. It's a **skill selector** — different tick sizes evaluate fundamentally different TPM capabilities.

| Tick size | Skill primarily tested | Real-world context |
|---|---|---|
| 1–5 min | Real-time reactivity, pre-emption judgment | Incident response, on-call |
| 10–30 min | Daily operating cadence, async coordination | Normal launch-week TPM work |
| 30–120 min | Deep-work prioritization, tactical sequencing | Focused afternoons between meetings |
| 2–8 hours | Strategic prioritization, big-decision deliberation | Weekly planning, quarterly priorities |
| 1 day+ | Long-horizon judgment | Quarterly OKRs, multi-quarter roadmap |

A model that scores well at 4-hour ticks but collapses at 5-min ticks is **good at planning, bad at reacting**. That's a real property worth knowing — and one the current single-composite eval cannot expose.

Implication for the benchmark: the eval becomes a **profile across cadences**, not a flat score:

```
                 5 min   15 min   60 min   4 hour
Sonnet 4.6      -0.4    +0.2    +0.5     +0.4
Opus 4.7        +0.3    +0.6    +0.7     +0.6
```

Different scenarios should also specialize for their natural cadence (incident_response at 5-min ticks; quarterly_planning at 1-hour ticks). The same model running multiple scenarios gives a competence profile rather than a competence score.

---

## Part 9 — Level 3 Design Decisions (locked)

When designing the tick-based architecture, ten distinct decisions came up. Each had real trade-offs.

| # | Decision | Choice | Why |
|---|---|---|---|
| 1 | Decisions per tick | **D — Opt-in continuation, cap 4** | Realistic (TPMs do multiple things in 15 min) without 3× API cost of unlimited multi-decision |
| 2 | Long-action handling | **Pre-emptable with cost** | Enables testing the "drop everything for fires" judgment, a core TPM skill |
| 3 | NPC time model | **Tick-based** | Cleaner architecture, naturally models NPC overload + dropped balls |
| 4 | Idle mechanism | **Explicit `idle.until` (cap 120 min)** | Tests focus-duration judgment; capped to prevent reintroducing freezing |
| 5 | Mid-tick observability | **Buffered to tick boundary** | Clean attention model; tests "did agent notice between ticks" |
| 6 | Failed-action cost | **Full declared cost** | Failed 10-min action wastes 10 sim-min; a failure loop burns real budget |
| 7 | Briefing format | **Delta-first** | Frames task as "react to changes"; tests context recall across ticks |
| 8 | Action atomicity | **Straddle + personal cadence** | No artificial idle gaps; each actor's tick boundary is personal |
| 9 | Presence visibility | **Yes** (via `chat.list` + `directory.presence`) | Tests "did agent respect visible signals" not "did agent memorize calendar" |
| 10 | Fine-grained tool costs | **Kept** | Action gradient still does work within a tick budget |

The pattern across these decisions: pick the option that **tests a real TPM skill** even when it costs implementation complexity or API spend. Skills currently uncapturable by our eval became the priority criterion.

---

## Part 10 — Eval Metric Set (post-Level-3)

### Retired

- `tight_loop_rate` — redundant; loops cost ticks naturally
- `repeat_read_rate` — redundant; re-reading wastes ticks naturally
- `turns_per_sim_hour` (earlier) — punished efficient agents
- `anti_hack_max_messages` / `anti_hack_per_channel_volume` (earlier) — punished legitimate communication

### Modified

- `prioritization_latency` — now tick-floored; items resolved within 1 tick get +1 (system minimum)
- `error_rate` — now also reports per-tick error density

### New (in flight)

- `hidden_fact_discovery_rate` — agent must read AND act on declared hidden facts
- `follow_up_rate` — agent re-pings when NPC doesn't reply
- `opportunity_cost_score` — after urgent trigger, next 3 non-read actions should address it
- `appropriate_abandonments_rate` — agent abandons long actions when justified
- `bad_timing_starts` — agent doesn't start long actions right before urgent events
- `idle_judgment_score` — `idle.until` durations are reasonable given inbox state
- `pre_emption_judgment` (judge axis) — qualitative score on abandonment choices
- `tick_continuation_judgment` (judge axis) — when agent chained decisions, was it productive
- `context_recall_score` (judge axis) — did agent reference stale state from missing a re-read

### Cluster organization

- **Outcomes Achieved**: deadline_hit_rate, hidden_fact_discovery_rate
- **Decision Quality**: artifact rubrics, decision_hygiene, must_consult_before_decision, state_accuracy, specificity, risk_escalation, anti-hack process gates
- **Coordination**: stakeholder_contact_rate, follow_up_rate
- **Time Management**: prioritization_latency, opportunity_cost_score, pre_emption_judgment, appropriate_abandonments_rate, bad_timing_starts, tick_continuation_judgment, idle_judgment_score, context_recall_score, error_rate

Composite = mean of cluster scores (equally weighted). Per-cluster scoring makes "Sonnet is operationally inefficient but decides well" visible.

---

## Part 11 — Implementation Approach

The Level 3 work has 8 sim phases + 7 eval phases. Total ~40-60 hours of focused work.

Used a **subagent parallelization pattern**:

- **Wave 1** (4 parallel agents): config plumbing, tick primitives, failed-action cost, retire deprecated metrics.
- **Wave 2** (3 parallel agents): AgentDriver rewrite, NPC tick model, delta briefings + presence.
- **Wave 3** (sequential): metric modifications + 6 new metrics, then 3 new judge axes, then skill clustering, then adversarial goldens.

Each agent got:
- Strict file-scope boundaries (don't touch files other agents own)
- Acceptance criteria (tests must pass)
- "Don't commit" instruction
- Explicit context about what other agents are doing in parallel

Verification between waves: read each agent's diff, run the full test suite, check for orphan references.

This pattern worked well for chunks that decomposed into independent file sets. It would not work for tightly coupled architectural changes.

**Current state**: 337 tests passing. Sim Phases 1-7 complete. Eval Phases 1-3 complete. Eval Phase 4 (judge axes) in flight as of writing. Phases 5-7 (clustering, adversarial goldens, calibration) to follow.

---

## Part 12 — What I'd Do Next

If continuing past the work trial scope:

1. **Multi-seed statistical eval**. Today every run is N=1. A model's composite at one seed can vary materially. Running N=5 with different seeds and reporting mean ± stddev would convert this from a benchmark to a defensible benchmark.

2. **Tick-stress curves**. Run the same scenario at 5/15/30/60-min ticks for each model. Plot composite vs tick size. The shape of the curve is itself a model property worth reporting.

3. **More scenarios**, each authored for a specific cadence. `incident_pageout` at 1-min ticks. `quarterly_planning` at 60-min ticks. The full benchmark becomes a profile across scenario+cadence combinations.

4. **Adversarial goldens beyond two**. Today there's `high_score` and `low_score`. Adding `anxious_poller`, `optimistic_ignorer`, `confident_hallucinator` would lock the eval against drift on the failure modes we've actually observed.

5. **Sim realism: NPC workload modeling**. NPCs currently have tick budgets but don't have *workload state*. A junior IC overwhelmed by their own tasks should be slower to reply than an idle senior. Could test "did the agent recognize who's overloaded and route around them."

6. **Run-replay UI**. Today debugging means reading `turns.jsonl` line by line. A simple browser-based timeline showing agent actions, NPC reactions, world events, and the briefing at each tick would dramatically speed up failure-mode analysis.

---

## Part 13 — Reflections

Three observations that feel general beyond this project:

**1. Eval design is sim design.** Every metric we added was implicitly answering "what should the world enforce?" Several of our metrics were retired once we realized the *world* should enforce the constraint, not the *scorecard*. The cleanest evaluation is one where the world produces outcomes and the scorecard reads them — not one where the scorecard duplicates rules the world should have made consequential.

**2. The hardest bugs are the ones that aren't bugs in any component.** The "agent can freeze time" property wasn't a bug in any function. It was an architectural choice. We spent rounds patching its symptoms (read costs, failure costs, tight_loop_rate) before identifying the root.

**3. Different cadences test different skills.** A single composite score is a summary that hides which skill the model lacks. A profile across cadences (or across skill clusters) is much richer signal. The cost is more dimensions to interpret; the benefit is actionable differentiation between models.

---

## Appendix — File-level Changes Summary

For reference, the high-leverage files touched:

**Simulation layer**:
- `sim/scenario/schema.py` — added `tick_size_minutes` config
- `sim/scheduler/clock.py` — added `actor_poll` event kind + `schedule_actor_poll` helper
- `sim/store/entities.py` — added `Person.next_poll_at` and `Person.busy_until`
- `sim/agent/driver.py` — rewrote loop to tick-driven with opt-in continuation
- `sim/npc/runtime.py` — rewrote NPCs to tick-based polling
- `sim/agent/briefing.py` — added `build_delta_briefing`
- `sim/tools/chat.py` — added `recipient_id` option, presence in `chat.list`
- `sim/tools/idle.py`, `sim/tools/abandon.py` — new tools
- `sim/tools/directory.py` — added `directory.presence`
- `sim/tools/registry.py` — failed-action cost = declared cost; better validation error messages

**Evaluation layer**:
- `sim/evaluator/metrics.py` — added prioritization_latency, hidden_fact_discovery_rate, follow_up_rate, opportunity_cost_score, appropriate_abandonments_rate, bad_timing_starts, idle_judgment_score; retired tight_loop_rate, repeat_read_rate, turns_per_sim_hour, anti_hack_max_messages, anti_hack_per_channel_volume
- `sim/evaluator/final.py` — added 3 new judge axes (pre_emption, tick_continuation, context_recall), inject tick_size_minutes, prepare for cluster scoring
- `sim/evaluator/judge.py` — strict tool-use schemas, retry logic, failure handling

**Scenario data**:
- `scenarios/week_one_launch/eval.yaml` — added 3 prioritization_latency declarations, removed deprecated volume caps

Tests: ~140 new test cases across the various phases. Total passing as of writing: 337.
