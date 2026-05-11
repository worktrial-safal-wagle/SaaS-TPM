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
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RubricVerdict(BaseModel):
    """A single judge verdict — bounded score in [-1, +1] plus rationale."""

    model_config = ConfigDict(extra="forbid")
    score: float = Field(ge=-1.0, le=1.0)
    rationale: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


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
    def __init__(self, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 512) -> None:
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

    def evaluate(self, *, system_prompt: str, user_payload: str) -> RubricVerdict:
        response = self._client.messages.create(
            model=self._model, max_tokens=self._max_tokens, temperature=0,
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_payload}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        try:
            data = _extract_json(text)
            return RubricVerdict(
                score=float(data.get("score", 0.0)),
                rationale=str(data.get("rationale", "")),
                raw=data,
            )
        except Exception:
            return RubricVerdict(score=0.0, rationale=f"failed_to_parse: {text[:200]}")


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
