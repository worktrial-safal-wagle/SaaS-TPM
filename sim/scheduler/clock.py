from __future__ import annotations

import heapq
from typing import Any, Iterable

from sim.scheduler.event import Event, EventHandler


class Scheduler:
    """Discrete-event scheduler.

    sim_time is an int in minutes since scenario start. The only ways it advances:
      - `advance(minutes)` is called explicitly (by a tool or by the agent's wait)
      - `advance_to(target)` is called
    In both cases, all events with `fire_at <= sim_time` drain in order.

    Event handlers may schedule further events; those that fall inside the advance
    window drain in the same call. Order is `(fire_at, event_id)` — fully deterministic.
    """

    def __init__(self, start_time: int = 0) -> None:
        self.sim_time: int = start_time
        self._heap: list[Event] = []
        self._next_event_id: int = 0
        self._fallback_handlers: dict[str, EventHandler] = {}

    def register_handler(self, kind: str, handler: EventHandler) -> None:
        """Register a default handler for events of a given kind.

        Event-specific handlers (passed to `schedule`) override these.
        """
        self._fallback_handlers[kind] = handler

    def schedule(
        self,
        fire_at: int,
        kind: str,
        payload: dict[str, Any] | None = None,
        handler: EventHandler | None = None,
    ) -> Event:
        if fire_at < self.sim_time:
            raise ValueError(
                f"cannot schedule event in the past: fire_at={fire_at} < sim_time={self.sim_time}"
            )
        event = Event(
            fire_at=fire_at,
            event_id=self._next_event_id,
            kind=kind,
            payload=payload or {},
            handler=handler,
        )
        self._next_event_id += 1
        heapq.heappush(self._heap, event)
        return event

    def peek_next_fire_at(self) -> int | None:
        return self._heap[0].fire_at if self._heap else None

    def pending(self) -> Iterable[Event]:
        return tuple(sorted(self._heap))

    def next_matching_fire_at(self, kinds: set[str]) -> int | None:
        """Earliest `fire_at` among pending events whose `kind` is in `kinds`.

        Used by `wait.for_next_event` to advance to the next agent-visible signal.
        """
        candidates = [e.fire_at for e in self._heap if e.kind in kinds]
        return min(candidates) if candidates else None

    def advance(self, minutes: int) -> list[Event]:
        if minutes < 0:
            raise ValueError(f"advance minutes must be non-negative, got {minutes}")
        return self.advance_to(self.sim_time + minutes)

    def _drain_due(self) -> list[Event]:
        """Drain any events with fire_at <= sim_time without advancing the clock."""
        return self.advance_to(self.sim_time)

    def advance_to(self, target: int) -> list[Event]:
        if target < self.sim_time:
            raise ValueError(
                f"cannot advance backwards: target={target} < sim_time={self.sim_time}"
            )
        fired: list[Event] = []
        while self._heap and self._heap[0].fire_at <= target:
            event = heapq.heappop(self._heap)
            # Step sim_time to the event's fire_at so the handler observes the right time.
            # Handlers may schedule new events; if they fall in [fire_at, target] they are
            # picked up by this same loop in priority order.
            self.sim_time = event.fire_at
            fired.append(event)
            handler = event.handler or self._fallback_handlers.get(event.kind)
            if handler is not None:
                handler(event, self)
        self.sim_time = target
        return fired


Clock = Scheduler
