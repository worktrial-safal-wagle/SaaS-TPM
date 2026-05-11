from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class EventHandler(Protocol):
    def __call__(self, event: "Event", scheduler: "SchedulerLike") -> None: ...


class SchedulerLike(Protocol):
    sim_time: int

    def schedule(self, fire_at: int, kind: str, payload: dict[str, Any], handler: EventHandler | None = None) -> "Event": ...


@dataclass(order=True)
class Event:
    """A single scheduled event.

    Ordering is `(fire_at, event_id)` so the priority queue is fully deterministic:
    ties on `fire_at` resolve by insertion order (monotonic counter), never randomly.
    Replayability depends on this invariant.
    """

    fire_at: int
    event_id: int
    kind: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)
    handler: EventHandler | None = field(compare=False, default=None)
