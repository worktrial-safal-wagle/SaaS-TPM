"""Meeting transcript generator.

Drives an NPC-on-NPC conversation for a calendar event. Used both when the
agent attends live (`meetings.attend`) and when the agent skips (the runtime
synthesizes the transcript from agendas + personas so the world still moves).

Speaker order is alphabetical by `person_id` for determinism. Each speaker
gets one turn per round; default is one round per attendee, with a configurable
`rounds` parameter.

Brain cache key for each turn is
`meeting.<event_id>.r<round>.<npc_id>` so replay is bit-stable.
"""

from __future__ import annotations

from sim.npc.brain import BrainCache, NpcBrain, NpcBrainContext, NpcBrainOutput
from sim.scheduler import Scheduler
from sim.store import MeetingTranscript, TranscriptTurn, World


class MeetingRunner:
    def __init__(
        self,
        world: World,
        scheduler: Scheduler,
        brain: NpcBrain,
        brain_cache: BrainCache,
    ) -> None:
        self.world = world
        self.scheduler = scheduler
        self.brain = brain
        self.brain_cache = brain_cache

    def run_meeting(self, event_id: str, *, synthesized: bool, rounds: int = 1) -> MeetingTranscript:
        evt = self.world.calendar.get(event_id)
        if evt is None:
            raise ValueError(f"unknown calendar event: {event_id}")
        if event_id in self.world.meeting_transcripts:
            return self.world.meeting_transcripts[event_id]

        # Speaker order: non-agent attendees, alphabetical. The agent isn't
        # an NPC; their participation is implicit (they "heard" the meeting).
        speakers = sorted(
            p for p in evt.attendees if p != self.world.agent_id
        )

        turns: list[TranscriptTurn] = []
        for r in range(rounds):
            for npc_id in speakers:
                npc = self.world.get_person(npc_id)
                if npc is None:
                    continue
                turn_event_id = f"meeting.{event_id}.r{r}.{npc_id}"
                context = NpcBrainContext(
                    npc_id=npc_id,
                    persona_role=npc.role,
                    persona_notes=npc.persona_notes,
                    knowledge=dict(npc.knowledge),
                    trigger_kind="meeting_turn",
                    trigger_payload={
                        "event_id": event_id,
                        "agenda": evt.agenda,
                        "title": evt.title,
                        "round": r,
                        "synthesized": synthesized,
                        "prior_turns": [
                            {"speaker_id": t.speaker_id, "body": t.body} for t in turns
                        ],
                    },
                    context_excerpt=evt.agenda,
                )
                output: NpcBrainOutput = self.brain_cache.get_or_compute(
                    self.world.scenario_id, self.world.seed, turn_event_id,
                    lambda c=context: self.brain.respond(c),
                )
                if output.speech:
                    turns.append(TranscriptTurn(
                        speaker_id=npc_id, body=output.speech,
                        sim_time=evt.start_sim_time,
                    ))

        transcript = MeetingTranscript(
            meeting_id=event_id,
            turns=turns,
            generated_at=self.scheduler.sim_time,
            synthesized=synthesized,
        )
        self.world.add_meeting_transcript(transcript)
        return transcript
