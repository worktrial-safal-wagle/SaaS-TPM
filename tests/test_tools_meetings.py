from __future__ import annotations

from sim.npc.brain import BrainCache, NpcBrainContext, NpcBrainOutput, StubBrain
from sim.npc.meetings import MeetingRunner
from sim.scheduler import Scheduler
from sim.store import CalendarEvent, Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.meetings import configure_meetings, meetings_ops


def _setup_meeting_world() -> tuple[World, Scheduler, ToolRegistry, MeetingRunner]:
    world = World(scenario_id="t", seed=0)
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.alice", display_name="Alice", role="eng"))
    world.add_person(Person(id="person.bob", display_name="Bob", role="design"))
    world.add_calendar_event(CalendarEvent(
        id="cal.launch", title="Launch sync",
        start_sim_time=60, end_sim_time=90,
        organizer_id="person.tpm",
        attendees=["person.tpm", "person.alice", "person.bob"],
        agenda="Talk about the v2.4 launch.",
    ))

    def brain_fn(ctx: NpcBrainContext) -> NpcBrainOutput:
        if ctx.trigger_kind == "meeting_turn":
            return NpcBrainOutput(speech=f"{ctx.npc_id} says hi")
        return NpcBrainOutput()

    runner = MeetingRunner(world, scheduler, StubBrain(brain_fn), BrainCache())
    configure_meetings(runner)

    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(meetings_ops())
    return world, scheduler, reg, runner


def test_meetings_attend_runs_transcript_and_advances_clock():
    world, scheduler, reg, runner = _setup_meeting_world()
    scheduler.advance(60)  # at the start time
    result = reg.dispatch(ToolCall(
        tool="meetings.attend", args={"event_id": "cal.launch"},
    ))
    assert result.ok
    assert result.cost_minutes == 30  # 90 - 60
    assert scheduler.sim_time == 90
    transcript = result.result["transcript"]
    speakers = [t["speaker_id"] for t in transcript["turns"]]
    assert speakers == ["person.alice", "person.bob"]
    assert transcript["synthesized"] is False
    assert world.calendar["cal.launch"].attended_by_agent is True


def test_meetings_attend_before_start_waits_until_start():
    world, scheduler, reg, runner = _setup_meeting_world()
    # sim_time=0 at call, start=60. attend should advance to start and then through end.
    result = reg.dispatch(ToolCall(
        tool="meetings.attend", args={"event_id": "cal.launch"},
    ))
    assert result.ok
    assert scheduler.sim_time == 90
    # Cost includes both the wait and the meeting duration
    assert result.cost_minutes == 90


def test_meetings_attend_non_attendee_rejected():
    world, scheduler, reg, runner = _setup_meeting_world()
    # Strip TPM from attendees
    world.calendar["cal.launch"].attendees = ["person.alice", "person.bob"]
    result = reg.dispatch(ToolCall(
        tool="meetings.attend", args={"event_id": "cal.launch"},
    ))
    assert result.ok is False
    assert "not an attendee" in result.error


def test_meetings_get_transcript_after_synthesized_run():
    world, scheduler, reg, runner = _setup_meeting_world()
    # Run a synthesized transcript directly (simulating agent skipping)
    runner.run_meeting("cal.launch", synthesized=True)
    result = reg.dispatch(ToolCall(
        tool="meetings.get_transcript", args={"event_id": "cal.launch"},
    ))
    assert result.ok
    assert result.result["synthesized"] is True


def test_meetings_replayable_transcript_via_brain_cache():
    """Same brain cache + same event_id → identical transcript content."""
    world, scheduler, reg, runner = _setup_meeting_world()
    scheduler.advance(60)
    reg.dispatch(ToolCall(tool="meetings.attend", args={"event_id": "cal.launch"}))
    transcript1 = [(t.speaker_id, t.body) for t in world.meeting_transcripts["cal.launch"].turns]

    # Reset world but reuse brain cache, run again
    world2 = World(scenario_id="t", seed=0)
    scheduler2 = Scheduler()
    world2.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world2.add_person(Person(id="person.alice", display_name="Alice", role="eng"))
    world2.add_person(Person(id="person.bob", display_name="Bob", role="design"))
    world2.add_calendar_event(CalendarEvent(
        id="cal.launch", title="Launch sync",
        start_sim_time=60, end_sim_time=90,
        organizer_id="person.tpm",
        attendees=["person.tpm", "person.alice", "person.bob"],
        agenda="Talk about the v2.4 launch.",
    ))

    def brain_fn(ctx):
        if ctx.trigger_kind == "meeting_turn":
            return NpcBrainOutput(speech=f"DIFFERENT-{ctx.npc_id}")  # different from above
        return NpcBrainOutput()

    runner2 = MeetingRunner(world2, scheduler2, StubBrain(brain_fn), runner.brain_cache)
    runner2.run_meeting("cal.launch", synthesized=False)
    transcript2 = [(t.speaker_id, t.body) for t in world2.meeting_transcripts["cal.launch"].turns]
    # Despite the different brain, the cache yields identical content
    assert transcript1 == transcript2
