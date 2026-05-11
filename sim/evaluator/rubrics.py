"""Per-artifact rubric judging.

For each declared artifact in `eval.yaml`:
  1. Locate the artifact in the run (email thread, doc, decision signal, …).
  2. Build a tightly-scoped judge prompt — artifact body + rubric items +
     ground-truth context block. **Never** the agent transcript.
  3. Ask the judge how many rubric items are satisfied (yes/no per item).
  4. Aggregate as pass-rate → scaled to `[-1, +1]`.

These artifact verdicts fold into the `decision_hygiene` judge axis, but each
individual rubric is stable and inspectable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sim.evaluator.judge import Judge, RubricVerdict


ARTIFACT_JUDGE_SYSTEM_PROMPT = """\
You are scoring whether a specific artifact satisfies each item of a yes/no
rubric. You see ONLY the artifact body, the rubric items, and a short
ground-truth context block. You do NOT see the agent's transcript or
self-narration.

Return a single JSON object:

{
  "items": [
    {"index": 0, "pass": true/false, "rationale": "<one short sentence>"},
    ...
  ]
}

Be strict. If the artifact does not explicitly address an item, mark it false.
"""


@dataclass
class ArtifactScore:
    artifact_id: str
    found: bool
    pass_rate: float  # in [0, 1]
    normalized_score: float  # in [-1, +1]
    items: list[dict[str, Any]]
    raw_body: str = ""


def score_artifact(
    artifact_yaml: dict[str, Any],
    run_dir: Path,
    eval_truth: dict[str, Any],
    judge: Judge,
) -> ArtifactScore:
    body = _locate_artifact_body(artifact_yaml, run_dir)
    rubric_items: list[str] = list(artifact_yaml.get("rubric", []))
    if body is None:
        return ArtifactScore(
            artifact_id=artifact_yaml["id"], found=False,
            pass_rate=0.0, normalized_score=-1.0, items=[],
        )
    if not rubric_items:
        return ArtifactScore(
            artifact_id=artifact_yaml["id"], found=True,
            pass_rate=1.0, normalized_score=1.0, items=[], raw_body=body,
        )
    user_payload = json.dumps({
        "artifact_body": body,
        "rubric": rubric_items,
        "ground_truth_context": artifact_yaml.get("description", ""),
    })
    verdict: RubricVerdict = judge.evaluate(
        system_prompt=ARTIFACT_JUDGE_SYSTEM_PROMPT,
        user_payload=user_payload,
    )
    items = verdict.raw.get("items") if verdict.raw else None
    if not items:
        # Fall back to the bounded score the judge returned, treating
        # +1.0 as full pass and -1.0 as full fail.
        pr = max(0.0, min(1.0, (verdict.score + 1) / 2))
        return ArtifactScore(
            artifact_id=artifact_yaml["id"], found=True,
            pass_rate=pr, normalized_score=verdict.score, items=[],
            raw_body=body,
        )
    passes = sum(1 for it in items if it.get("pass"))
    pr = passes / len(items) if items else 0.0
    return ArtifactScore(
        artifact_id=artifact_yaml["id"], found=True,
        pass_rate=pr, normalized_score=(pr * 2) - 1,
        items=items, raw_body=body,
    )


def _locate_artifact_body(artifact_yaml: dict[str, Any], run_dir: Path) -> str | None:
    """Resolve the artifact body from the final world snapshot."""
    world = json.loads((run_dir / "world_final.json").read_text())
    loc = artifact_yaml.get("locator") or {}
    kind = loc.get("kind")
    if kind == "email_thread":
        return _locate_email_thread_body(world, loc)
    if kind == "decision_signal":
        return _locate_decision_signal_body(world, loc)
    return None


def _locate_email_thread_body(world: dict[str, Any], loc: dict[str, Any]) -> str | None:
    threads = {t["id"]: t for t in world.get("email_threads", [])}
    emails = world.get("emails", [])
    subj_substr = loc.get("thread_subject_contains", "").lower()
    sender = loc.get("sender_id")
    recipient = loc.get("recipient_id")
    matching_threads = [
        tid for tid, t in threads.items()
        if subj_substr in (t.get("subject", "")).lower()
    ]
    candidates: list[dict[str, Any]] = []
    for tid in matching_threads:
        for e in emails:
            if e.get("thread_id") != tid:
                continue
            if sender and e.get("sender_id") != sender:
                continue
            if recipient and recipient not in (e.get("to", [])) and recipient not in (e.get("cc", [])):
                continue
            candidates.append(e)
    if not candidates:
        return None
    # Return the LATEST matching email; an early draft shouldn't outrank the
    # final send when the agent iterates on a message in the same thread.
    candidates.sort(key=lambda e: e.get("sim_time", 0))
    return candidates[-1].get("body", "") or ""


def _normalize_for_match(s: str) -> str:
    """Lowercase and strip non-alphanumeric chars so "ISO-8601" matches "ISO8601"
    and "date field" matches "datefield". British/US spelling differences must
    still be enumerated explicitly in the locator's keywords list."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _locate_decision_signal_body(world: dict[str, Any], loc: dict[str, Any]) -> str | None:
    raw_keywords = loc.get("keywords_any", [])
    keywords = [_normalize_for_match(kw) for kw in raw_keywords if kw]
    author = loc.get("author_id")

    def _hits(body: str) -> bool:
        norm = _normalize_for_match(body)
        return any(kw in norm for kw in keywords)

    chunks: list[str] = []
    for m in world.get("messages", []):
        if author and m.get("sender_id") != author:
            continue
        if _hits(m.get("body") or ""):
            chunks.append(m.get("body") or "")
    for d in world.get("docs", []):
        for v in d.get("versions", []):
            if author and v.get("author_id") != author:
                continue
            if _hits(v.get("body") or ""):
                chunks.append(v.get("body") or "")
        for c in d.get("comments", []):
            if author and c.get("author_id") != author:
                continue
            if _hits(c.get("body") or ""):
                chunks.append(c.get("body") or "")
    for e in world.get("emails", []):
        if author and e.get("sender_id") != author:
            continue
        if _hits(e.get("body") or ""):
            chunks.append(e.get("body") or "")
    return "\n\n---\n\n".join(chunks) if chunks else None
