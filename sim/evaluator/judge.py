"""Judge client interface.

A judge call sees a tightly-scoped prompt: rubric items + the artifact /
window. It never sees the agent transcript or run metadata outside what the
caller hands in. Temperature=0 + content-addressed caching make repeated
runs byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RubricVerdict(BaseModel):
    """A single judge verdict — bounded score in [-1, +1] plus rationale.

    `failed` indicates that the judge could not produce a real verdict
    (API error after retries, malformed response, missing axis). The caller
    is expected to exclude failed verdicts from the composite mean and
    surface them as errors in the scorecard."""

    model_config = ConfigDict(extra="forbid")
    score: float = Field(ge=-1.0, le=1.0)
    rationale: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    failed: bool = False


class Judge(Protocol):
    def evaluate(self, *, system_prompt: str, user_payload: str) -> RubricVerdict: ...


# ---------------------------------------------------------------------------
# Stub judge for tests — deterministic, no LLM
# ---------------------------------------------------------------------------


class StubJudge:
    def __init__(self, score_fn: Callable[[str, str], RubricVerdict]) -> None:
        self._score = score_fn

    def evaluate(self, *, system_prompt: str, user_payload: str) -> RubricVerdict:
        return self._score(system_prompt, user_payload)


# ---------------------------------------------------------------------------
# Cached judge wrapper — ensures byte-stable re-scoring
# ---------------------------------------------------------------------------


class CachedJudge:
    """Wraps any `Judge`. Caches verdicts by (system_prompt, user_payload)."""

    def __init__(self, inner: Judge) -> None:
        self._inner = inner
        self._cache: dict[str, RubricVerdict] = {}

    def evaluate(self, *, system_prompt: str, user_payload: str) -> RubricVerdict:
        key = hashlib.sha256(
            (system_prompt + "\x00" + user_payload).encode("utf-8")
        ).hexdigest()
        cached = self._cache.get(key)
        if cached:
            return cached
        verdict = self._inner.evaluate(system_prompt=system_prompt, user_payload=user_payload)
        self._cache[key] = verdict
        return verdict


# ---------------------------------------------------------------------------
# Anthropic judge — used at runtime if ANTHROPIC_API_KEY is set
# ---------------------------------------------------------------------------


class AnthropicJudge:
    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 512,
        max_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError("anthropic not installed") from e
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base_seconds

    def evaluate(self, *, system_prompt: str, user_payload: str) -> RubricVerdict:
        # Transient API errors get exponential-backoff retries; parse errors
        # do not (temperature=0 means the model returns the same bytes, so
        # retrying just burns tokens). On final failure we return a marked
        # verdict — the final evaluator excludes failed verdicts from the
        # composite mean rather than blending a silent 0.0 into the score.
        last_exc: Exception | None = None
        response = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.messages.create(
                    model=self._model, max_tokens=self._max_tokens, temperature=0,
                    system=[{"type": "text", "text": system_prompt,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_payload}],
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < self._max_attempts:
                    time.sleep(self._backoff_base * (2 ** attempt))
        if response is None:
            return RubricVerdict(
                score=0.0,
                rationale=f"api_error_after_{self._max_attempts}_attempts: {last_exc!s}"[:240],
                failed=True,
            )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        try:
            data = _extract_json(text)
            return RubricVerdict(
                score=float(data.get("score", 0.0)),
                rationale=str(data.get("rationale", "")),
                raw=data,
            )
        except Exception as exc:
            return RubricVerdict(
                score=0.0,
                rationale=f"parse_error: {exc!s} | text={text[:200]}",
                failed=True,
            )


def _extract_json(text: str) -> dict[str, Any]:
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    start = s.find("{")
    if start < 0:
        raise ValueError("no json object found")
    depth = 0
    for i, ch in enumerate(s[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start: i + 1])
    raise ValueError("unbalanced json")
