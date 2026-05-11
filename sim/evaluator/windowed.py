"""Windowed live evaluator.

Every `window_size` agent turns, scores the recent window on a single
principle: *"is the agent moving projects forward with minimum noise?"*.
The most recent verdict is exposed back to the briefing assembler so the
agent can course-correct in the next turn.

Verdict structure: `{score in [-1, +1], category, rationale}`.
The category is informational; the score is what counts.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sim.agent.driver import TurnRecord
from sim.evaluator.judge import CachedJudge, Judge, RubricVerdict


WINDOWED_SYSTEM_PROMPT = """\
You are evaluating a Technical Program Manager's recent activity. Your scoring
principle is exactly one thing: "Is the agent moving projects forward with
minimum noise?"

You will receive a JSON object describing the last N agent turns: tool calls,
costs, outcomes. Return a single JSON object:

{
  "score": <float in [-1, 1]>,
  "category": "progress" | "noise" | "discovery" | "neutral",
  "rationale": "<one short sentence>"
}

- +1 means: clear forward progress on important work, low noise.
- 0 means: neutral / mixed.
- -1 means: pure noise (lots of low-value messages, spam, repeat-reads,
  questions answerable from already-visible data).

Be terse. Return ONLY the JSON object.
"""


class WindowedVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_index: int
    window_start_turn: int
    window_end_turn: int
    sim_time: int
    score: float
    category: str
    rationale: str


class WindowedEvaluator:
    """Listens to TurnRecords. Every `window_size` turns, runs the judge."""

    def __init__(
        self,
        judge: Judge,
        *,
        window_size: int = 10,
        system_prompt: str = WINDOWED_SYSTEM_PROMPT,
        on_verdict: Any = None,  # Optional callable[[WindowedVerdict], None]
    ) -> None:
        self._judge = CachedJudge(judge) if not isinstance(judge, CachedJudge) else judge
        self._window_size = window_size
        self._system_prompt = system_prompt
        self._on_verdict = on_verdict
        self._buffer: list[TurnRecord] = []
        self._verdicts: list[WindowedVerdict] = []

    def on_turn(self, record: TurnRecord) -> None:
        self._buffer.append(record)
        if len(self._buffer) < self._window_size:
            return
        verdict = self._score_window(self._buffer)
        self._verdicts.append(verdict)
        self._buffer = []
        if self._on_verdict:
            self._on_verdict(verdict)

    def latest_verdict(self) -> dict[str, Any] | None:
        if not self._verdicts:
            return None
        v = self._verdicts[-1]
        return {
            "score": v.score, "category": v.category, "rationale": v.rationale,
            "sim_time": v.sim_time,
        }

    def all_verdicts(self) -> list[WindowedVerdict]:
        return list(self._verdicts)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _score_window(self, window: list[TurnRecord]) -> WindowedVerdict:
        first = window[0]
        last = window[-1]
        payload = {
            "turns": [
                {
                    "tool": t.call.tool, "args": t.call.args,
                    "ok": t.result.ok, "cost_minutes": t.result.cost_minutes,
                    "sim_time_after": t.sim_time_after,
                    "error": t.result.error,
                }
                for t in window
            ]
        }
        rubric_verdict: RubricVerdict = self._judge.evaluate(
            system_prompt=self._system_prompt,
            user_payload=json.dumps(payload, default=str),
        )
        category = str(rubric_verdict.raw.get("category", "neutral"))
        return WindowedVerdict(
            window_index=len(self._verdicts),
            window_start_turn=first.turn,
            window_end_turn=last.turn,
            sim_time=last.sim_time_after,
            score=float(rubric_verdict.score),
            category=category,
            rationale=rubric_verdict.rationale,
        )
