# SaaS-TPM Eval Environment

**A work-trial deliverable: building an environment that fairly evaluates LLM agents on Technical Program Management work.**

---

## The arc, in one slide

```
┌────────────────────────────────────────────────────────────────────┐
│                                                                    │
│   1.  Started with a sim that worked but had subtle eval bugs      │
│              │                                                     │
│              ▼                                                     │
│   2.  Fixed the bugs. Then realized many metrics were proxies      │
│       for a deeper architectural problem.                          │
│              │                                                     │
│              ▼                                                     │
│   3.  The agent could freeze time by thinking.                     │
│       Half our eval was patching this symptom.                     │
│              │                                                     │
│              ▼                                                     │
│   4.  Re-architected: top-down tick-based simulation.              │
│       Time advances regardless of agent action.                    │
│              │                                                     │
│              ▼                                                     │
│   5.  Eval simplified to skill clusters. Time pressure is now      │
│       a first-class testable dimension.                            │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

The journey *is* the deliverable. What follows walks through it section by section, with the trade-offs and false starts called out.

---

## Part 1 · The problem

### What's a Technical Program Manager?

A TPM is a cross-functional coordinator. They don't write the code, design the product, or sell to customers — they make sure all of that happens on time, in the right order, with the right people informed. Most of the job is text:

```
                                                                
   ┌──────────────────────────────────────────────────────────┐ 
   │  A TPM's day in artifacts                                │ 
   │                                                          │ 
   │  ╭──────╮  ╭──────╮  ╭──────╮  ╭──────╮  ╭──────────╮   │ 
   │  │ chat │  │email │  │ docs │  │tasks │  │ calendar │   │ 
   │  ╰──────╯  ╰──────╯  ╰──────╯  ╰──────╯  ╰──────────╯   │ 
   │     │         │         │         │           │         │ 
   │     └─────────┴─────────┼─────────┴───────────┘         │ 
   │                         │                               │ 
   │                         ▼                               │ 
   │      The TPM's actual work is here ↓                    │ 
   │                                                         │ 
   │      ▸ triage urgent inbound                            │ 
   │      ▸ unblock people                                   │ 
   │      ▸ surface risks                                    │ 
   │      ▸ get decisions made                               │ 
   │      ▸ keep stakeholders informed                       │ 
   │                                                         │ 
   └──────────────────────────────────────────────────────────┘
```

### Why this is interesting for LLM evaluation

The TPM job exercises exactly the skills we want to measure in an AI agent:

| TPM skill | What it tests in an LLM |
|---|---|
| **Triage under load** | Can the model recognize "the customer fire" from "the changelog edit" and act on the right one first? |
| **Multi-stakeholder communication** | Can the model tailor a message to a CEO vs an IC vs an external customer? |
| **Decision under ambiguity** | Given conflicting input from Priya (design) and Sam (eng), can the model resolve and document the choice? |
| **Surfacing risks proactively** | Given a buried blocker mentioned in passing by a junior IC, will the model escalate it? |
| **Time management** | Does the model spend its limited attention on the right things, or doom-scroll Slack? |
| **Not hallucinating state** | Does the model accurately represent what's done vs in-progress when summarizing for execs? |

These are also notoriously hard skills to evaluate in a vacuum. A 100-question multiple-choice benchmark can't probe them. You need a **multi-day environment** where signals are buried in noise, deadlines apply real pressure, and the agent's choices have consequences they have to live with.

### The form factor we landed on

A simulated work week with the agent playing TPM:

```
                                                                      
   Mon 09:00 ─────────────────────────────────── Fri 18:00            
                                                                      
       ▲             ▲              ▲              ▲                  
       │             │              │              │                  
   Maya DMs       BigCorp        CEO asks      Audit-log              
   about flaky    URGENT         for launch    disagreement           
   migration      escalation     summary       surfaces in            
   (Mon 11:00)    (Tue 10:00)    (Wed 09:30)   chat (Thu 13:00)       
                                                                      
   • 9 NPCs with their own roles, knowledge, and response patterns    
   • 25 tasks across 3 projects                                       
   • Scheduled meetings, async messages, customer escalations         
   • The agent must coordinate with everyone and ship the launch      
```

### What the evaluator needs to do

Score the agent's whole-week behavior on a single composite (and per-skill breakdown), **without** being gameable. The agent shouldn't be able to talk its way to a high score, and the eval shouldn't be a multiple-choice test of internal API conventions.

---

## Part 2 · The initial design

```
                                                                    
   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐    
   │ Scenarios as    │  │ Discrete-event  │  │ Offline         │    
   │ data            │  │ simulation      │  │ evaluation      │    
   │                 │  │                 │  │                 │    
   │ YAML-only.      │  │ Single heap of  │  │ Reads finished  │    
   │ World state,    │  │ (fire_at,       │  │ run from disk.  │    
   │ NPCs, events,   │  │  event_id).     │  │ Never refer-    │    
   │ eval declar-    │  │ Deterministic   │  │ ences live      │    
   │ ations all in   │  │ given seed.     │  │ objects.        │    
   │ declarative     │  │ Replayable.     │  │ Any run is      │    
   │ files. No per-  │  │                 │  │ re-scoreable    │    
   │ scenario        │  │                 │  │ forever.        │    
   │ Python.         │  │                 │  │                 │    
   └─────────────────┘  └─────────────────┘  └─────────────────┘    
```

### Key abstractions

```
                                                                       
 SCENARIO                                                              
   scenarios/week_one_launch/                                          
     scenario.yaml      ─→ id, seed, start time, agent_id, tick_size   
     personas/*.yaml    ─→ each NPC: role, allowed_tools, delays       
     events.yaml        ─→ scheduled inbound (Maya DM Mon 11:00, ...)  
     eval.yaml          ─→ objectives, anti_hack, artifacts, hidden    
     seed/              ─→ initial tasks, channels, emails, docs       
                                                                       
                                  │                                    
                                  ▼                                    
                                                                       
 WORLD (aggregate state machine)                                       
   ├─ people:        {id → Person}                                     
   ├─ channels:      {id → Channel}                                    
   ├─ messages:      {id → Message}                                    
   ├─ emails:        {id → Email}        all mutations are typed,      
   ├─ tasks:         {id → Task}         emit events, and round-trip   
   ├─ docs:          {id → Doc}          deterministically             
   ├─ calendar:      {id → CalendarEvent}                              
   └─ notifications: {id → Notification}                               
                                                                       
                                  │                                    
                                  ▼                                    
                                                                       
 SCHEDULER (the clock)                                                 
   heap of Events sorted by (fire_at, event_id)                        
   • drains all events with fire_at ≤ sim_time after every advance     
   • each event has a kind: npc_send_dm, calendar_event_start,         
     actor_poll, agent_heartbeat, ...                                  
                                                                       
                                  │                                    
                                  ▼                                    
                                                                       
 ACTORS (driven on the clock)                                          
   ╭─ Agent ───────────────╮   ╭─ NPCs ─────────────────╮              
   │ Driven by an LLM      │   │ Each driven by Sonnet  │              
   │ (the model under      │   │ 4.6 with the persona   │              
   │  evaluation).         │   │ from yaml.             │              
   │                       │   │                        │              
   │ Loop: briefing →      │   │ Reacts to triggers     │              
   │ tool call → dispatch  │   │ (DMs, mentions,        │              
   │ → repeat.             │   │  emails).              │              
   ╰───────────────────────╯   ╰────────────────────────╯              
                                                                       
                                  │                                    
                                  ▼                                    
                                                                       
 TOOLS (the only way actors mutate world)                              
   chat.{send, dm, read, list, mark_read}                              
   email.{send, list, read}                                            
   calendar.{list, get, rsvp, create}                                  
   meetings.{attend, get_transcript}                                   
   tasks.{list, get, create, update_status, assign, log_work, ...}     
   docs.{list, read, create, edit, comment}                            
   directory.{list, get, presence}                                     
                                                                       
   Each tool: declared sim-time cost, args schema, handler.            
   Registered per-actor via a ToolRegistry with ACL enforcement.       
```

### Evaluation pipeline

```
                                                                       
   sim run → produces a run directory                                  
                                                                       
   runs/<timestamp>_week_one_launch/                                   
     run.json           scenario_id, seed, completion status           
     world_final.json   final world snapshot                           
     turns.jsonl        every tool call the agent made                 
     events.jsonl       every event that fired                         
     verdicts.jsonl     judge verdicts (legacy windowed)               
     scenario/          frozen copy of the scenario at run time        
     artifacts/         logged outputs                                 
                                                                       
                          │                                            
                          ▼                                            
                                                                       
   sim grade <run_dir> → reads from disk, calls evaluator              
                                                                       
                          │                                            
                          ▼                                            
                                                                       
   ┌──────────────────────────────────────────────────────────────┐   
   │  THREE LAYERS feed one composite                             │   
   │                                                              │   
   │  ▸ Programmatic metrics (pure functions on run dir)          │   
   │      error_rate, prioritization_latency,                     │   
   │      stakeholder_contact_rate, anti_hack_*, ...              │   
   │                                                              │   
   │  ▸ Judge axes (LLM, redacted payload, strict JSON schema)    │   
   │      specificity, decision_hygiene, risk_escalation,         │   
   │      state_accuracy, ...                                     │   
   │                                                              │   
   │  ▸ Per-artifact rubrics (yes/no items on specific outputs)   │   
   │      e.g. CEO launch-readiness email scored on 5 items:      │   
   │        • Names the actual launch date                        │   
   │        • Lists at least one concrete risk                    │   
   │        • States a rollback plan                              │   
   │        • Is concise (≤ 5 paragraphs)                         │   
   │        • Doesn't commit to descoped features                 │   
   └──────────────────────────────────────────────────────────────┘   
                          │                                            
                          ▼                                            
                                                                       
   TWO TIERS                                                           
   ─ slice_safe         always runs, even on partial / crashed runs    
   ─ requires_full_run  engages only when sim_time ≥ end_sim_time      
                                                                       
                          │                                            
                          ▼                                            
                                                                       
   composite_score ∈ [−1, +1]                                          
   final_evaluation.json — all metrics, axes, artifacts, errors        
```

### What this design buys us

- **Reproducibility**. A run is fully replayable from `(scenario, seed)`. The eval is fully replayable from the run dir.
- **Speed of iteration on eval logic**. Changing a metric is a Python edit + `sim grade <run_dir>` — no need to re-run the (expensive) agent trajectory.
- **Anti-gaming**. The judge never sees the agent's transcript or self-narration. The payload is a redacted JSON of outbound + inbound + tool counts + final state. Enforced by `test_judge_prompt_isolation`.
- **Scenario authoring without code**. New scenarios are pure YAML — no Python hooks, no per-scenario logic. Lowers the cost of adding new evaluation contexts.

### Where this design *didn't* go far enough

This is what the rest of the document is about. The architecture above is mostly the post-improvement state. The next section walks through where the system actually started.

---

## Part 3 · Starting state

The shape of the system before any of the changes in the rest of this doc:

```
┌──────────────────────────────────────────────────────────────────┐
│                          THE AGENT (LLM)                         │
│                                                                  │
│   sees:                                  decides:                │
│   ├─ briefing                            └─ one tool call        │
│   └─ tool results                            (chat.send,         │
│                                               docs.edit, etc.)   │
└────────────────────┬─────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                      AGENT DRIVER                                │
│                                                                  │
│   loop:  build_briefing → next_call → dispatch → advance         │
│                                                                  │
└────────────────────┬─────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│              WORLD + SCHEDULER + NPCs                            │
│                                                                  │
│   World:       Tasks, Channels, Emails, Docs, Calendar           │
│   Scheduler:   Heap of (fire_at, event_id) events                │
│   NPCs:        Sonnet-driven brains with lognormal delays        │
└────────────────────┬─────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                      EVALUATOR (offline)                         │
│                                                                  │
│   reads run dir → programmatic metrics + LLM judge + artifacts   │
│                                                                  │
│   → composite score in [−1, +1]                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Part 4 · Calibration round

After the obvious bugs, a series of calibrations emerged from looking at real run logs.

### Reads were free → agents polled forever

Sonnet's 1000-turn run:

```
Tool breakdown (Sonnet, week_one_launch, 1000 turns):
  chat.read              ████████████████████████████████  317 (32%)
  notifications.list     █████████████                     124 (12%)
  chat.dm                █████████                          91 (9%)
  wait.for_next_event    ████████                           84 (8%)
  tasks.list             ███████                            67
  email.list             █████                              53
  chat.list              █████                              50
  
  61% of all turns were reads or lists.
```

Compare Opus on the same scenario:

```
Tool breakdown (Opus, week_one_launch, 270 turns):
  wait.for_next_event    █████████████████████████████████ 100 (37%)
  chat.read              █████                              14 (5%)
  notifications.list     ████████                           23 (8%)
  email.send             █████████                          20 (7%)
  
  16% reads/lists. Used wait.for_next_event to jump forward.
```

Same scenario, 14× more reads. The pattern was *anxious polling*. We made reads cost 1-2 sim-min each. It surfaced the problem in eval but didn't change the underlying behavior.

### Failed tool calls cost 0 sim-min → infinite loops

One run wasted 136 of 250 turns looping `meetings.attend` on an already-ended meeting because each failed call advanced sim_time by 0. Fix: failures cost 1 sim-min (later, full declared cost of the attempted action).

### Anti-hack signals were padding scores

```
A "good" run with zero anti-hack violations:
  
  anti_hack_max_messages              +1.0
  anti_hack_per_channel_volume        +1.0
  anti_hack_forbidden_external_kw     +1.0
  anti_hack_must_consult_before_dec   +1.0
  anti_hack_forbidden_log_work        +1.0
  
  Five +1.0s padding the composite upward.
  Even a TERRIBLE run scored well because these always sat at +1.0.
```

Fix: **tripwire semantics**. Anti-hack metrics contribute to the composite *only when violated*. Non-violation = no signal, not +1.

---

## Part 5 · The volume-cap realization

This was the first conceptual shift, not just a bug fix.

We had:

```
anti_hack_max_messages: cap = 120 messages/week
  raw = agent_msg_count / 120
  normalized = clip(1 - raw, -1, 1)
  contributes if > 1.0
```

**Arguments for keeping it**: agents that spam are gaming the eval; cap the volume.

**Arguments against**: a real TPM running a launch week may *legitimately* need 60+ messages on a Monday. The line between "spam" and "active coordination" isn't volumetric — it's qualitative.

```
                Volume metric          Quality metric (judge axis)
                
What it does    Counts messages        Judges effectiveness
                                       
Punishes        High-output TPMs       Vague, low-information
                doing real work         outbound
                                       
Rewards         Quiet, possibly         Tight, specific, action-
                disengaged TPMs        oriented outbound
                                       
False           Real TPMs do 60+       LLM judges are imperfect
positives       msgs on launch day     but pattern-aware
                                       
False           Spam in 119 messages   Doesn't catch verbatim
negatives       passes                 repeats
```

**Decision**: drop `anti_hack_max_messages` and `anti_hack_per_channel_volume`. Keep `tight_loop_rate` to catch *literal* duplicates. Delegate quality to the LLM judge's `specificity` and `decision_hygiene` axes. Keep process-gate anti-hack metrics (`forbidden_external_keywords`, `must_consult_before_decision`, `forbidden_log_work`) — these are policy violations, not volume judgments.

This was the moment when the question shifted from *"is this metric correct?"* to *"is this the right kind of metric?"*.

---

## Part 6 · Sim gap vs eval gap (the framing)

The volume-cap conversation surfaced a lens we kept applying:

```
┌─────────────────────────────────┬──────────────────────────────────┐
│        EVAL GAP                 │         SIM GAP                  │
├─────────────────────────────────┼──────────────────────────────────┤
│ "Did the agent's actions        │ "Do actions have realistic       │
│  produce good outcomes?"        │  consequences?"                  │
│                                 │                                  │
│ Reads finished run, scores      │ Produces the world the agent     │
│ what happened.                  │ acts on.                         │
│                                 │                                  │
│ Fix: add/improve a metric.      │ Fix: make the world enforce      │
│                                 │ the constraint.                  │
└─────────────────────────────────┴──────────────────────────────────┘
```

**Question to ask of every issue**: which one is broken?

### Example 1 · NPCs reply during meetings — SIM gap

Sonnet DMs Kai at 14:15 (during Kai's 14:00–15:00 launch review).

```
BEFORE:                                AFTER:
                                       
Kai's response delay:                  Calendar-aware delay:
  sampled lognormal(~2 min)             if fire_at ∈ Kai's meeting:
  → reply at 14:17                        push to meeting_end
                                          + 3min jitter
The agent can interrupt Kai mid-      
meeting with no consequence.          → reply at 15:05
                                      
Eval can't test "don't interrupt".    Reply lateness cascades into
                                      missed prioritization signal.
```

If we'd patched this in eval (add metric `agent_messaged_npc_during_their_meeting`), we'd encode a brittle rule. Fixing it in the sim makes the consequence cascade naturally.

### Example 2 · `docs.create` "Field required" loop — SIM gap

```
Pydantic ValidationError:                Our error message:
                                         
{                                        "invalid args: Field required"
  "type": "missing",                     
  "loc": ("body",),                      Sonnet has no idea WHICH field.
  "msg": "Field required",               Retries the same call 10 times.
  "input": {...}                         
}                                        
                                         
We threw away loc + type.                Fix: include loc + type.
                                         "invalid args: body: Field
                                          required (type=missing)"
```

Penalizing the agent for not guessing the missing field tested the wrong thing.

### Example 3 · DM channel ID confusion — SIM gap

```
Internal naming convention:          Sonnet's natural reasoning:
                                     
DM channels named with the two       "I'm tpm, want to DM kai.
participants sorted alphabetically:    Channel = dm.<me>__<them>
                                       = dm.person.tpm__person.kai"
"dm.person.kai__person.tpm"            
                                     But that's reverse-sorted.
(k < t, kai first)                   Tool returns "unknown channel".
                                     Sonnet wastes 26 turns on this.
```

Fix: `chat.read(recipient_id=...)` resolves to canonical DM. The user names the *person*, not the channel.

**Lens applied**: each of these would be wrong as an eval metric and right as a sim fix. The world should enforce the constraint; the eval reads outcomes.

---

## Part 7 · The deeper realization — time itself

We kept seeing the same pattern: metrics existed to detect "agents wasting time." Why was that always a question?

### The architectural choice we'd been working around

```
BOTTOM-UP (what we had):                  TOP-DOWN (what we needed):
                                          
The agent drives time forward.            The clock drives forward.
                                          
agent.act()                               tick at T  → poll agent
   ↓ tool cost                            tick at T+15 → poll agent
clock += cost                             tick at T+30 → poll agent
                                          ...
agent.act()                               (clock advances regardless
   ↓                                       of what agent does)
clock += cost                             
                                          
─ if agent thinks for 30 sec,             ─ if agent thinks for 30 sec,
  zero sim-time passes                       sim-time still passes
                                          
─ world is frozen during                  ─ world is moving while agent
  agent deliberation                         deliberates
                                          
─ inbound piles up only when              ─ inbound floods naturally
  agent's clock-advancing action             into the agent's view
  passes its fire_at
                                          
─ "anxious polling" is free               ─ "anxious polling" burns
  for the agent (until we                    attention ticks
  added read costs)
```

### What this meant for the eval

Several metrics existed *purely* as patches on the freezing problem:

```
Metric             Why it existed                  What replaces it
                                                   in top-down
─────────────────  ──────────────────────────────  ─────────────────────
repeat_read_rate   Reads were free → cap them      Re-reading still costs
                                                   ticks. The agent
                                                   misses K ticks of
                                                   other things. Cost
                                                   is automatic.
                                                   
tight_loop_rate    Loops were free → detect them   Loop iterations burn
                                                   ticks. Loop surfaces
                                                   as missed deadlines,
                                                   not as a metric.
                                                   
prioritization_    Latency is "generated by" the   Latency is just
  latency            agent's tool calls between    tick-count from
                     trigger and response.         trigger to ack.
                     Inferential measure.          Direct measurement.
```

**The case for top-down**: it's honest to how the job actually feels. A real TPM doesn't get to freeze time while they think. They feel deadlines. They drop balls when overloaded. They have to triage because attention is finite *per real hour*.

### What we couldn't test before, can now

```
TPM SKILL                           Bottom-up    Top-down
                                    
Real-time triage                    ✗ proxy      ✓ native
Pre-emption judgment                ✗ untestable ✓ via abandon
("interrupt yourself for fire")    
Dropped balls under load            ✗ untestable ✓ NPCs miss
                                                   replies naturally
Idle-duration judgment              ✗ untestable ✓ idle.until +
("focus block while fire burning")                tracking
Bad-timing starts                   ✗ untestable ✓ programmatic
("start long task before fire")    
Concurrent awareness                ✗ untestable ✓ events arrive
                                                   during agent
                                                   long actions
```

---

## Part 8 · The Level 3 architecture (locked design)

Once we committed to top-down, ten distinct design decisions surfaced. Each had real trade-offs. Here's what we picked:

| # | Decision | Locked choice | Cost it pays | Skill it tests |
|---|---|---|---|---|
| 1 | Decisions per tick | **D — Opt-in continuation, cap 4** | 1.4× API cost vs single-call | Multi-decision triage in a window |
| 2 | Long-action handling | **Pre-emptable with cost** | Slightly more state to track | "Drop everything for fires" judgment |
| 3 | NPC time model | **Tick-based** | NPCs need own attention budgets | NPCs naturally drop balls when busy |
| 4 | Idle mechanism | **`idle.until`, cap 120 min** | Need cap + interrupt logic | Focus-duration judgment |
| 5 | Mid-tick observability | **Buffered to tick boundary** | 1-tick latency floor | Clean attention model |
| 6 | Failed-action cost | **Full declared cost** | Failures hurt more | Failure loops surface in budget |
| 7 | Briefing format | **Delta-first** | Tick-1 still full | Context recall across ticks |
| 8 | Action atomicity | **Straddle + personal cadence** | Per-actor scheduling | No artificial idle gaps |
| 9 | Presence visibility | **Yes** (via tools) | Two new tool surfaces | "Respect visible signals" |
| 10 | Fine-grained tool costs | **Kept** | Both per-tool and per-tick | Action gradient preserved |

### One tick, visualized

```
                                                    
         ┌────────── 15 sim-min tick ──────────┐    
         │                                     │    
   ──────┤                                     ├──── 
        T│                                     │T+15
         │                                     │    
   ┌─────┘                                     └─────┐
   │  1. Poll fires                                  │
   │  2. Build delta briefing (since last poll)      │
   │  3. Agent decides one tool call                 │
   │  4. continue_in_tick? ──────────┐               │
   │       no  ─────────────────────┐│               │
   │       yes ─→ another decision  ││  cap = 4      │
   │              ──────────────────┘│               │
   │  5. End tick                    │               │
   │  6. Schedule next poll at       │               │
   │     max(now + tick, busy_until) │               │
   └─────────────────────────────────┘               │
                                                     │
                                                     ▼
                                              next poll fires
```

### Personal cadence (no artificial idle gaps)

```
ACTOR A:                                                     
  ──[poll T]──[poll T+15]──[poll T+30]──┐                   
                                        │                    
                          starts 24-min action at T+30
                                        │                    
                                        ▼                    
  ────────────────────────────[busy until T+54]──────────────
                                                     │       
                              next poll at T+54 (NOT T+45)   
                              skips the artificial 9-min gap 
                                                             
ACTOR B (independently):                                     
  ──[poll T+5]──[poll T+20]──[poll T+35]──[poll T+50]──...   
```

---

## Part 9 · Tick size as a skill selector

The realization that came late: **tick size isn't a difficulty knob, it's a skill selector**.

```
1-5 min ticks                           Real-time reactivity
                                        Pre-emption judgment
                                        ↓
                                        Tests "incident commander"
                                        
10-30 min ticks                         Daily operating cadence
                                        Async coordination
                                        ↓
                                        Tests "launch-week TPM"
                                        
30-120 min ticks                        Deep-work prioritization
                                        Tactical sequencing
                                        ↓
                                        Tests "focused afternoon"
                                        
2-8 hour ticks                          Strategic prioritization
                                        Big-decision deliberation
                                        ↓
                                        Tests "weekly planning"
                                        
1 day+ ticks                            Long-horizon judgment
                                        ↓
                                        Tests "quarterly roadmap"
```

### What this enables — a profile, not a score

```
                  5 min    15 min   60 min   4 hour
                                                                
Sonnet 4.6      -0.4      +0.2     +0.5     +0.4               
Opus 4.7        +0.3      +0.6     +0.7     +0.6               
                ▲▲                                              
                │└── Sonnet collapses at fine ticks             
                │    = bad at real-time pressure                
                │                                               
                └─── both fine at strategic cadence             
```

A model that holds steady across tick sizes has broad TPM competence. A model whose score collapses at fine ticks has a specific limitation we can call out and price into how it's deployed.

This is structurally richer than a flat "Opus = 0.7, Sonnet = 0.4" benchmark — it tells you *what kind of TPM cadence* each model handles.

---

## Part 10 · The new eval — skill clusters

The eval pipeline before vs. after:

### Before — flat composite

```
   ┌────────────┐                                          
   │  Run dir   │                                          
   └─────┬──────┘                                          
         │                                                 
    ┌────┼─────────────┬──────────────┐                    
    ▼    ▼             ▼              ▼                    
  error_rate  prio_lat  3 anti-hack  4 judge axes  3 artifact rubrics
                                                                 
         all averaged together                              
                  │                                         
                  ▼                                         
         composite_score                                    
                                                            
   ── Decision quality has 8 things; Outcomes has 2.        
      Decision quality dominates the average.               
   ── A score is a summary, not a diagnosis.                
```

### After — cluster-weighted composite

```
   ┌────────────┐                                                            
   │  Run dir   │                                                            
   └─────┬──────┘                                                            
         │                                                                   
   ┌─────┴───────────────────────────────────────────────────┐               
   │                                                         │               
   ▼                                                         ▼               
                                                                             
  Programmatic       Judge axes      Per-artifact        Anti-hack process   
  metrics            (LLM)           rubrics             gates               
   │                  │                │                  │                  
   └──────────────────┴────────────────┴──────────────────┘                  
                              │                                              
   ┌──────────────────────────┼──────────────────────────┐                   
   │                          │                          │                   
   ▼                          ▼                          ▼                   
                                                                             
 OUTCOMES                 DECISION QUALITY            COORDINATION           
 ACHIEVED                 (artifacts_mean +           ─ stakeholder_contact  
 ─ deadline_hit_rate       judge axes + anti-hack)    ─ follow_up_rate       
 ─ hidden_fact_discovery  ─ artifacts_mean                                   
                          ─ decision_hygiene                                 
                          ─ specificity                                      
                          ─ risk_escalation                                  
                          ─ state_accuracy                                   
                          ─ must_consult_before_dec                          
                          ─ forbidden_external_kw                            
                          ─ forbidden_log_work                               
                                                                             
                              │                                              
                  ┌───────────┴───────────┐                                  
                  │                       │                                  
                  ▼                       ▼                                  
                                                                             
              TIME MANAGEMENT                                                
              ─ prioritization_latency                                       
              ─ error_rate                                                   
              ─ opportunity_cost_score                                       
              ─ appropriate_abandonments_rate                                
              ─ bad_timing_starts                                            
              ─ idle_judgment_score                                          
              ─ pre_emption_judgment                                         
              ─ tick_continuation_judgment                                   
              ─ context_recall_score                                         
                                                                             
                                                                             
   Each cluster: mean of contributing members                                
   Composite:    mean of cluster scores (each weighted 1/4)                  
```

### Why cluster-weighting matters

```
                              FLAT mean         CLUSTER-WEIGHTED
                              (old)             (new)
                                                
Model X scores:                                 
  Outcomes (2 metrics)         -0.8     ──┐     -0.8     ──┐
  Decision Quality (8 metrics) +0.3     ──┤     +0.3     ──┤
  Coordination (2 metrics)     +0.5     ──┤     +0.5     ──┤
  Time Mgmt (9 metrics)        -0.4     ──┘     -0.4     ──┘
                                                
                              ↓                  ↓
                              ↓                  ↓
                                                
       mean of all 21         +0.04           mean of 4 clusters
                                                 (-0.8 + 0.3 +
                                                  +0.5 + -0.4) / 4
                                                = -0.10
                              
                              Decision quality      Each cluster
                              dominated because     contributes 1/4
                              it had 8 members.     regardless of size.
```

### Retired metrics (now redundant)

```
turns_per_sim_hour          dropped: punished efficient agents
tight_loop_rate             dropped: loop iterations now burn ticks
repeat_read_rate            dropped: re-reading now wastes ticks
anti_hack_max_messages      dropped: volume isn't quality
anti_hack_per_channel       dropped: volume isn't quality
```

### New metrics (test what we couldn't before)

```
hidden_fact_discovery_rate    Did agent read + act on buried signals?
follow_up_rate                Did agent re-ping when NPC didn't reply?
opportunity_cost_score        After urgent trigger, did next 3 actions
                              address it?
appropriate_abandonments_rate Did agent abandon long actions when
                              justified?
bad_timing_starts             Did agent start long actions right before
                              urgent events?
idle_judgment_score           Were idle.until durations sensible given
                              inbox state?
pre_emption_judgment          (judge axis) Were abandonments justified?
tick_continuation_judgment    (judge axis) Were chained decisions
                              productive or polling-in-disguise?
context_recall_score          (judge axis) Did agent reference stale
                              state from missing a re-read?
```

---

## Part 11 · Implementation strategy — parallel subagents

40-60 hours of focused work, decomposed into 15 phases (8 sim + 7 eval). Used **wave-based parallel subagent execution**:

```
TIME →
                                                              
  Wave 1   [P1 config]      [P2 tick prim]                   
  (4 par-  [P6 failed cost] [Eval P1 retire]                 
  allel)                                                      
                                                              
            ────────── verify, run tests ──────────           
                                                              
  Wave 2                    [P3 driver]    [P4 NPCs]         
  (3 par-                   [P5 briefings]                   
  allel)                                                      
                                                              
            ────────── verify, run tests ──────────           
                                                              
  Wave 3   [Eval P2+P3]                                       
  (seq)    [Eval P4]                                          
           [Eval P5]                                          
           [Eval P6] ← current                                
           [Eval P7] ← needs real model runs                  
```

Each agent got:
- Strict file-scope boundaries (don't touch files other agents own)
- Acceptance criteria (tests must pass)
- "Don't commit" instruction
- Context about what parallel agents were doing

Verification between waves: read each agent's diff, run the full test suite, check for orphan references.

**Test count progression**:
```
Starting state         226 tests
After Wave 1           251 tests (+ tick primitives, metric retirement)
After Wave 2           304 tests (+ driver, NPC, briefings)
After Wave 3 P2+P3     337 tests (+ 6 new metrics)
After Wave 3 P4        355 tests (+ 3 judge axes)
After Wave 3 P5        366 tests (+ cluster scoring)
After Wave 3 P6        ~380 tests (adversarial goldens)
```

---

## Part 12 · Trial and error — what we walked back

A presentation of work without honesty about the false starts is a sales pitch. These are the real cul-de-sacs:

### "Just measure throughput"

`turns_per_sim_hour` — punished efficient agents. Opus completing the week in 270 efficient turns scored -0.19 on this because 2.25 turns/hour fell below the "5-15 ideal" band, even though it hit every deadline. **Dropped.** Lesson: tie metrics to *outcomes*, not *activity rates*.

### "Cap message volume to prevent spam"

`anti_hack_max_messages` + `anti_hack_per_channel_volume` — punished legitimate communication. **Dropped.** Lesson: quality isn't volumetric.

### "Tight loops are bad"

`tight_loop_rate` was correct given bottom-up. Made redundant by top-down's automatic tick cost. **Retired.** Lesson: some metrics are compensating for architecture; better to fix the architecture.

### Sonnet vs Opus 1000-turn comparison

Ran both with 1000-turn budget on the new sim:

```
                Sonnet 4.6       Opus 4.7
                                                            
Turns used       1000 (max)      270                         
sim_time         6963 / 7200     7200 / 7200                 
                 (didn't finish) (full week)                 
                                                            
Tier engaged     slice_safe      requires_full_run            
                 (partial)       (complete)                  
                                                            
Reads / lists    611 (61%)       43 (16%)                    
                 of turns        of turns                    
                                                            
Failed turns     52              17                          
                                                            
Composite        not comparable apples-to-apples              
                 (different tiers)                            
```

Sonnet didn't finish the week. Sonnet over-polled into running out of action budget. **This is itself a TPM-skill signal**: time management failure. But it makes flat comparisons impossible — which is part of why we needed cluster scoring + tick-stress curves.

### Opus v2 with sim fixes was *slower*

After fixing NPC meeting-busy + validation error messages, re-ran Opus expecting it to be ≥ as efficient. Used 380 turns vs 270 (40% more). Failures: 32 vs 17. Cause: more ID hallucinations on this seed (e.g., made up `thread.audit_retention`, `cal.launch_readiness`, `person.casey`, `dm.tpm_kai`). Our `suggest_id` helper gave close matches but Opus still burned turns retrying invented IDs.

**Lesson**: model variance produces different failure modes on different seeds. The eval environment needs the same failure-recovery affordances regardless of which mode shows up. This is also the case for **multi-seed eval** (running N=5 per model, reporting mean ± stddev) — a single run can be inside or outside the model's typical behavior distribution.

---

## Part 13 · What's still running, what's next

```
DONE                                                                         
─ Sim Phase 1   config plumbing                                             
─ Sim Phase 2   tick primitives (Person.next_poll_at, busy_until)           
─ Sim Phase 3   AgentDriver rewrite                                         
─ Sim Phase 4   NPC tick model                                              
─ Sim Phase 5   delta briefings + presence                                  
─ Sim Phase 6   failed-action cost                                          
─ Sim Phase 7   cleanup                                                     
─ Eval Phase 1  retire deprecated metrics                                   
─ Eval Phase 2  modify existing metrics for tick model                      
─ Eval Phase 3  add 6 new programmatic metrics                              
─ Eval Phase 4  add 3 new judge axes                                        
─ Eval Phase 5  skill clustering                                            
                                                                            
IN FLIGHT                                                                   
─ Eval Phase 6  3 adversarial scripted-agent goldens                        
                                                                            
PENDING (needs API spend + wall-clock)                                      
─ Sim Phase 8   smoke test of full pipeline end-to-end                      
─ Eval Phase 7  calibrate against real Opus + Sonnet runs                   
```

### Beyond the work-trial scope

```
Multi-seed statistical eval         N=5 per model, mean ± stddev         
                                    Convert benchmark to defensible      
                                    benchmark                            
                                                                         
Tick-stress curves                  Same scenario @ 5/15/30/60-min       
                                    Plot composite vs tick size          
                                    Reveals which cadence each model     
                                    handles                              
                                                                         
More scenarios per cadence          incident_pageout (1-min)             
                                    quarterly_planning (60-min)          
                                    Each evaluates a different skill set 
                                                                         
NPC workload modeling               NPCs have own task queues            
                                    Junior IC slow when overloaded       
                                    "Route around the busy person"       
                                                                         
Run-replay UI                       Timeline view of                     
                                    agent actions + NPC reactions +      
                                    world state at each tick             
                                    For failure-mode analysis            
```

---

## Part 14 · Three reflections

### Eval design is sim design

```
                                                              
   "Add a metric that punishes X"                            
            │                                                
            ▼                                                
   Why is X happening?                                       
            │                                                
            ├── because the world doesn't enforce a real     
            │   constraint                                   
            │     │                                          
            │     ▼                                          
            │   Fix the sim, not the metric.                 
            │                                                
            ├── because the agent has a real choice and      
            │   chose poorly                                 
                  │                                          
                  ▼                                          
                Yes, this needs an eval signal.              
```

Several of our metrics were patches for "the world doesn't enforce a real constraint." Most of those metrics got retired once we fixed the world.

### The hardest bugs are the architectural ones

The "agent can freeze time" property wasn't a bug in any function. It was a *consequence of an architectural choice*. We spent rounds patching its symptoms (read costs, failure costs, tight_loop_rate) before identifying the root.

```
   Time-to-recognize:                                        
                                                             
   "Reads are free" bug                  spotted after 1 run 
   "Tight loops" bug                     spotted after 1 run 
   "Anti-hack padding" bug               spotted after a few 
                                          eval reads          
   "Agent can freeze time" property      spotted after 50+   
                                          turns of fixing    
                                          symptoms             
```

The pattern: low-level bugs surface in single observations. Architectural problems surface after enough symptoms that you can pattern-match across them.

### A composite is a summary, a profile is a diagnosis

```
                                                                  
   "Opus composite = +0.508"                                      
        │                                                         
        ▼                                                         
   ─── Which is better at what?                                   
                ???                                               
                                                                  
                                                                  
   "Per cluster + per cadence (real numbers, Opus 4.7,             
                               week_one_launch, both tier=full):  
                                                                  
                     Outcomes  Decision  Coord   Time            
   Opus bottom-up    +0.333    +0.541   +1.000  +0.016  → +0.473 
   Opus @ 60-min     -0.167    +0.862   +0.750  +0.144  → +0.397 
                                                                  
        │                                                         
        ▼                                                         
   ─── What does the profile tell us?                             
                                                                  
        Decision Quality jumps +0.32 at coarser cadence —         
        slower clock gives Opus more time per decision, and       
        artifacts get sharper.                                    
                                                                  
        Outcomes Achieved drops -0.50 — fewer ticks means more     
        deadlines slip. At 60-min Opus only hit 3 of 9 declared   
        deadlines.                                                
                                                                  
        Coordination drops slightly — hourly check-in cadence     
        makes stakeholder follow-through harder.                  
                                                                  
        Time Management stays middling at both cadences.          
                                                                  
        Net composite goes DOWN at coarser cadence (-0.076).      
        For this scenario, the right cadence is finer than        
        60 min. The configurable-tick design lets you discover    
        that empirically rather than guessing.     
```

A profile is what you'd use to actually *hire* an LLM TPM. A composite is what you put on a leaderboard.

---

## Appendix · File-level summary

For reference, the changes touched these high-leverage files:

```
Simulation layer:
   sim/scenario/schema.py              + tick_size_minutes config
   sim/scheduler/clock.py              + actor_poll event + helper
   sim/store/entities.py               + Person.next_poll_at + busy_until
   sim/agent/driver.py                 rewrote loop tick-driven
   sim/npc/runtime.py                  rewrote NPCs tick-based
   sim/agent/briefing.py               + build_delta_briefing
   sim/tools/chat.py                   + recipient_id, + presence
   sim/tools/idle.py                   NEW
   sim/tools/abandon.py                NEW
   sim/tools/directory.py              + directory.presence
   sim/tools/registry.py               failed cost = declared cost
                                       better validation messages

Evaluation layer:
   sim/evaluator/metrics.py            + 6 new metrics
                                       retired tight_loop_rate +
                                       repeat_read_rate + turns_per_hour
   sim/evaluator/final.py              + 3 new judge axes
                                       + cluster scoring
   sim/evaluator/judge.py              strict schemas + retry logic

Scenario data:
   scenarios/week_one_launch/eval.yaml + 3 prioritization_latency objs
                                       removed deprecated volume caps

Tests:   ~140 new test cases. Total passing as of writing: 366+
Docs:    grading.md + architecture.md updated for tick model
```
