"""Close-match suggestions for unknown-id error messages.

When the agent guesses an id from a natural-language name (e.g. `"general"`
instead of `"channel.general"`, or `"thread.legal_audit_retention"` instead
of `"thread.legal_review"`), we want the error to suggest the closest real
ids so it can self-correct in one turn instead of looping six.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable


def suggest_id(
    label: str, bad_id: str, valid_ids: Iterable[str],
    *, max_suggestions: int = 3, cutoff: float = 0.4,
) -> str:
    """Build a structured error message: 'unknown <label>: X; did you mean: [...]?'.

    If no close match passes the similarity cutoff, falls back to a small
    sample of available ids so the agent has *something* concrete to use.
    """
    valid = sorted(valid_ids)
    matches = difflib.get_close_matches(bad_id, valid, n=max_suggestions, cutoff=cutoff)
    if matches:
        return (
            f"unknown {label}: {bad_id!r}; "
            f"did you mean one of {matches}? "
            f"({len(valid)} {label}s exist)"
        )
    sample = valid[: max_suggestions + 2]
    return (
        f"unknown {label}: {bad_id!r}; "
        f"no close match in {len(valid)} {label}s; "
        f"sample of available: {sample}"
    )
