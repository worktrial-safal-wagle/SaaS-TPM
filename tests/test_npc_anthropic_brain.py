"""Tests for sim.npc.anthropic_brain.AnthropicNPCBrain.

The Anthropic SDK is mocked — these tests verify the brain's contract:
- it constructs system prompts that include persona + knowledge,
- it formats the user message with the trigger and context excerpt,
- it asks the SDK to force tool-use with the npc_act schema,
- it parses tool_use blocks into NpcBrainOutput correctly,
- it stays silent (empty output, captured rationale) on API errors and on
  malformed responses, without raising.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sim.npc.anthropic_brain import AnthropicNPCBrain, NPC_ACT_TOOL
from sim.npc.brain import NpcBrainContext, NpcBrainOutput


# ---------------------------------------------------------------------------
# Stub Anthropic client
# ---------------------------------------------------------------------------


class _StubMessages:
    """Stand-in for `anthropic.Anthropic().messages` — captures last call args
    and returns a programmable response."""

    def __init__(self, response: Any = None, raise_exc: Exception | None = None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class _StubClient:
    def __init__(self, response: Any = None, raise_exc: Exception | None = None):
        self.messages = _StubMessages(response=response, raise_exc=raise_exc)


def _make_response(tool_input: dict[str, Any]) -> Any:
    """A response object with one tool_use content block named 'npc_act'."""
    block = SimpleNamespace(type="tool_use", name="npc_act", input=tool_input)
    return SimpleNamespace(content=[block])


def _context(**overrides: Any) -> NpcBrainContext:
    base = dict(
        npc_id="person.kai",
        persona_role="Senior Backend Engineer",
        persona_notes="Kai is a senior backend eng, owns v2.4.",
        knowledge={"migration_flake": "events table needs index on workspace_id"},
        trigger_kind="message_inserted",
        trigger_payload={
            "message_id": "msg.1",
            "channel_id": "dm.kai__tpm",
            "sender_id": "person.tpm",
            "body": "Quick check — can you repro the migration timeout?",
            "mentions": [],
        },
        context_excerpt="(no prior messages)",
    )
    base.update(overrides)
    return NpcBrainContext(**base)


# ---------------------------------------------------------------------------
# Contract: prompts include persona + knowledge + trigger
# ---------------------------------------------------------------------------


def test_system_prompt_includes_persona_role_notes_and_knowledge():
    client = _StubClient(response=_make_response({
        "tool_calls": [], "rationale": "ack", "speech": None,
    }))
    brain = AnthropicNPCBrain(client=client)
    brain.respond(_context())
    kwargs = client.messages.last_kwargs
    system_text = kwargs["system"][0]["text"]
    assert "Senior Backend Engineer" in system_text
    assert "Kai is a senior backend eng" in system_text
    assert "migration_flake" in system_text
    assert "events table needs index on workspace_id" in system_text


def test_user_message_includes_trigger_kind_and_payload():
    client = _StubClient(response=_make_response({
        "tool_calls": [], "rationale": "ack", "speech": None,
    }))
    brain = AnthropicNPCBrain(client=client)
    brain.respond(_context())
    kwargs = client.messages.last_kwargs
    user_text = kwargs["messages"][0]["content"]
    assert "message_inserted" in user_text
    assert "Quick check" in user_text  # the trigger body
    assert "(no prior messages)" in user_text  # the excerpt


def test_uses_forced_tool_use_with_npc_act_schema():
    client = _StubClient(response=_make_response({
        "tool_calls": [], "rationale": "ack", "speech": None,
    }))
    brain = AnthropicNPCBrain(client=client)
    brain.respond(_context())
    kwargs = client.messages.last_kwargs
    assert kwargs["tools"] == [NPC_ACT_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "npc_act"}
    assert kwargs["temperature"] == 0


# ---------------------------------------------------------------------------
# Parsing: tool_use blocks become NpcBrainOutput
# ---------------------------------------------------------------------------


def test_parses_tool_calls_from_tool_use_response():
    client = _StubClient(response=_make_response({
        "tool_calls": [
            {"tool": "chat.dm", "args": {
                "recipient_id": "person.tpm",
                "body": "Yes — running it locally now. Will report back in 90 min.",
            }},
        ],
        "speech": None,
        "rationale": "Direct DM ask from TPM warrants a one-line ack with ETA.",
    }))
    brain = AnthropicNPCBrain(client=client)
    out = brain.respond(_context())
    assert isinstance(out, NpcBrainOutput)
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].tool == "chat.dm"
    assert out.tool_calls[0].args["recipient_id"] == "person.tpm"
    assert "ETA" in out.rationale


def test_empty_tool_calls_means_silent_npc():
    """Empty tool_calls is the 'ignore' response — no tools dispatched."""
    client = _StubClient(response=_make_response({
        "tool_calls": [],
        "speech": None,
        "rationale": "Not addressed to me — staying out.",
    }))
    brain = AnthropicNPCBrain(client=client)
    out = brain.respond(_context())
    assert out.tool_calls == []
    assert out.speech is None
    assert "Not addressed" in out.rationale


def test_parses_meeting_speech_when_present():
    client = _StubClient(response=_make_response({
        "tool_calls": [],
        "speech": "Smoke tests are still flaky on the events migration; "
                  "I want one more pass before we cut the release branch.",
        "rationale": "Meeting turn — share the risk.",
    }))
    brain = AnthropicNPCBrain(client=client)
    out = brain.respond(_context(trigger_kind="meeting_turn"))
    assert out.speech is not None
    assert "Smoke tests" in out.speech


def test_skips_malformed_tool_call_entries():
    """Junk entries are filtered, good ones kept — never crash on bad shape."""
    client = _StubClient(response=_make_response({
        "tool_calls": [
            {"tool": "chat.dm", "args": {"recipient_id": "person.tpm", "body": "ok"}},
            "not a dict",
            {"tool": "chat.send"},  # missing args
            {"args": {"channel_id": "x"}},  # missing tool
            {"tool": 42, "args": {}},  # wrong type
        ],
        "rationale": "partial",
    }))
    brain = AnthropicNPCBrain(client=client)
    out = brain.respond(_context())
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].tool == "chat.dm"


# ---------------------------------------------------------------------------
# Failure modes: never raise, always return a NpcBrainOutput
# ---------------------------------------------------------------------------


def test_api_error_returns_silent_output_with_rationale():
    """API error after exhausted retries returns silent output with marker."""
    client = _StubClient(raise_exc=RuntimeError("simulated 429"))
    # max_attempts=1 means no retries — keeps the test fast.
    brain = AnthropicNPCBrain(client=client, max_attempts=1)
    out = brain.respond(_context())
    assert out.tool_calls == []
    assert out.speech is None
    assert "brain_api_error" in out.rationale


def test_api_error_retries_on_transient_failures():
    """Brain retries on transient errors and succeeds on the second attempt."""
    class _FlakyMessages:
        def __init__(self):
            self.call_count = 0

        def create(self, **kwargs):
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("transient 429")
            return _make_response({
                "tool_calls": [{"tool": "chat.dm", "args": {
                    "recipient_id": "person.tpm", "body": "got it"}}],
                "rationale": "ack after retry",
            })

    class _FlakyClient:
        def __init__(self):
            self.messages = _FlakyMessages()

    client = _FlakyClient()
    # Short backoff so the test isn't slow.
    brain = AnthropicNPCBrain(
        client=client, max_attempts=3, backoff_base_seconds=0.001,
    )
    out = brain.respond(_context())
    assert client.messages.call_count == 2
    assert len(out.tool_calls) == 1
    assert "ack after retry" in out.rationale


def test_no_tool_use_block_returns_silent_output():
    """If the model somehow returns a text-only response, stay silent."""
    text_block = SimpleNamespace(type="text", text="here's some prose")
    response = SimpleNamespace(content=[text_block])
    client = _StubClient(response=response)
    brain = AnthropicNPCBrain(client=client)
    out = brain.respond(_context())
    assert out.tool_calls == []
    assert "no_tool_use" in out.rationale


def test_wrong_tool_name_returns_silent_output():
    """If the model invokes a different tool than npc_act, stay silent."""
    block = SimpleNamespace(type="tool_use", name="some_other_tool", input={"foo": "bar"})
    response = SimpleNamespace(content=[block])
    client = _StubClient(response=response)
    brain = AnthropicNPCBrain(client=client)
    out = brain.respond(_context())
    assert out.tool_calls == []


# ---------------------------------------------------------------------------
# Defaults / construction
# ---------------------------------------------------------------------------


def test_default_construction_requires_anthropic_api_key(monkeypatch):
    """Without ANTHROPIC_API_KEY, real-client construction raises."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicNPCBrain()


# ---------------------------------------------------------------------------
# Tick-poll path: render the accumulated triggers list to the brain
# ---------------------------------------------------------------------------


def test_user_message_renders_accumulated_triggers_inbox():
    """When the runtime supplies multiple triggers (tick-poll), the user
    prompt switches to the inbox template and renders all of them in
    most-recent-first order."""
    from sim.npc.brain import NpcTrigger

    triggers = [
        NpcTrigger(
            kind="message_inserted",
            payload={"message_id": "msg.3", "channel_id": "channel.eng",
                     "sender_id": "person.tpm", "body": "freshest ping"},
            sim_time=20,
        ),
        NpcTrigger(
            kind="message_inserted",
            payload={"message_id": "msg.2", "channel_id": "channel.eng",
                     "sender_id": "person.dani", "body": "older ping"},
            sim_time=10,
        ),
    ]
    client = _StubClient(response=_make_response({
        "tool_calls": [], "rationale": "ack", "speech": None,
    }))
    brain = AnthropicNPCBrain(client=client)
    ctx = NpcBrainContext(
        npc_id="person.kai",
        persona_role="Senior Backend Engineer",
        triggers=triggers,
        context_excerpt="(no prior messages)",
    )
    brain.respond(ctx)
    user_text = client.messages.last_kwargs["messages"][0]["content"]
    # The poll prompt header should be present.
    assert "TICK POLL" in user_text
    assert "INBOX" in user_text
    # Both triggers should be in the rendered inbox.
    assert "freshest ping" in user_text
    assert "older ping" in user_text
    # Most-recent first: "freshest ping" comes before "older ping".
    assert user_text.index("freshest ping") < user_text.index("older ping")


def test_user_message_uses_legacy_template_for_meeting_turn():
    """Meeting-turn invocations populate the single-trigger fields and leave
    `triggers` empty; the brain falls back to the legacy template."""
    client = _StubClient(response=_make_response({
        "tool_calls": [], "rationale": "ack", "speech": "I'll prep notes.",
    }))
    brain = AnthropicNPCBrain(client=client)
    ctx = NpcBrainContext(
        npc_id="person.kai",
        persona_role="Senior Backend Engineer",
        trigger_kind="meeting_turn",
        trigger_payload={"event_id": "cal.standup", "agenda": "standup"},
        context_excerpt="standup",
    )
    brain.respond(ctx)
    user_text = client.messages.last_kwargs["messages"][0]["content"]
    assert "TRIGGER KIND: meeting_turn" in user_text
    assert "TICK POLL" not in user_text
