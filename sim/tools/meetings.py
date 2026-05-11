"""`meetings.*` tool operations.

`meetings.attend` is the only tool whose declared cost equals the meeting's
real duration. From the agent's perspective the call is atomic — it returns
with the transcript and `sim_time` advanced to the meeting's `end_sim_time`.

The handler does the work; declared cost is 0 because the handler advances
the scheduler directly to `end_sim_time` (like `wait.*`).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sim.npc.meetings import MeetingRunner
from sim.scheduler import Scheduler
from sim.store import World
from sim.tools.base import ToolOp
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


class _MeetingsToolContext:
    """Process-level container so the meetings tools can access the
    MeetingRunner without it being a global. Wired up at registration time."""

    runner: MeetingRunner | None = None


_meetings_ctx = _MeetingsToolContext()


def configure_meetings(runner: MeetingRunner) -> None:
    """Bind the MeetingRunner used by the meeting tool ops."""
    _meetings_ctx.runner = runner


class MeetingsAttendArgs(BaseModel):
    event_id: str


def meetings_attend(world: World, scheduler: Scheduler, args: MeetingsAttendArgs, caller_id: str) -> dict[str, Any]:
    runner = _meetings_ctx.runner
    if runner is None:
        raise ToolError("meetings runner not configured")
    evt = world.calendar.get(args.event_id)
    if evt is None:
        raise ToolError(suggest_id("event", args.event_id, world.calendar.keys()))
    if caller_id not in evt.attendees:
        raise ToolError(f"not an attendee of: {args.event_id}")
    if scheduler.sim_time > evt.end_sim_time:
        raise ToolError(f"meeting already ended at sim_time={evt.end_sim_time}")
    if scheduler.sim_time < evt.start_sim_time:
        # Wait until the meeting starts before attending — model the gap as a
        # waiting cost so we don't quietly time-travel forward.
        scheduler.advance(evt.start_sim_time - scheduler.sim_time)
    evt.attended_by_agent = True
    transcript = runner.run_meeting(args.event_id, synthesized=False)
    # Commit the full meeting duration as elapsed sim_time
    if scheduler.sim_time < evt.end_sim_time:
        scheduler.advance(evt.end_sim_time - scheduler.sim_time)
    return {
        "event_id": args.event_id, "attended": True,
        "duration_minutes": evt.end_sim_time - evt.start_sim_time,
        "transcript": transcript.model_dump(),
    }


class MeetingsGetTranscriptArgs(BaseModel):
    event_id: str


def meetings_get_transcript(world: World, scheduler: Scheduler, args: MeetingsGetTranscriptArgs, caller_id: str) -> dict[str, Any]:
    transcript = world.meeting_transcripts.get(args.event_id)
    if transcript is None:
        raise ToolError(f"no transcript yet for: {args.event_id}")
    return transcript.model_dump()


def meetings_ops() -> list[ToolOp]:
    return [
        ToolOp("meetings.attend", MeetingsAttendArgs, 0, meetings_attend,
               description="Attend a calendar event live. Atomic: advances sim_time through the meeting's full duration and records the transcript from NPC speaker turns."),
        ToolOp("meetings.get_transcript", MeetingsGetTranscriptArgs, 0, meetings_get_transcript,
               description="Read the transcript of a meeting that has ended."),
    ]
