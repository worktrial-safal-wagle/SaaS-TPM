"""Sonnet-backed NPC brain.

Implements the `NpcBrain` Protocol via the Anthropic SDK using a single
forced tool-use call per trigger.

Design decisions worth being explicit about:

- **Stateless per call.** The brain receives a fresh `NpcBrainContext` for
  every trigger. It has no memory of prior conversations between calls —
  the conversation lives in the `World` aggregate, which the runtime reads
  fresh and packs into `context_excerpt`. We don't duplicate world state
  into brain state.
- **Forced tool-use schema.** Output is constrained by Anthropic tool-use
  to a single `npc_act` tool with a typed `tool_calls` list, optional
  `speech`, and `rationale`. No JSON-parsing fragility.
- **Persona-visibility-limited.** The brain sees only the persona, the
  hidden knowledge dict, the trigger, and the runtime-built
  `context_excerpt`. It does NOT see the agent's briefing, the full world
  snapshot, or other NPCs' knowledge.
- **Silent on failure.** API errors and parse failures return an empty
  `NpcBrainOutput` (NPC stays silent) with a rationale string capturing
  the failure mode. Doesn't crash the run; rare silence is realistic and
  contained.
- **Determinism is the BrainCache's job, not the brain's.** Same
  `(scenario_id, seed, event_id)` → same cached output across replays.
  Within one process, `temperature=0` keeps the LLM itself deterministic.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sim.npc.brain import NpcBrain, NpcBrainContext, NpcBrainOutput
from sim.tools.base import ToolCall


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


SYSTEM_PROMPT_TEMPLATE = """\
You are a coworker at Lumen Analytics, a small SaaS company. You are NOT the
TPM. The TPM agent is coordinating projects this week. You react to triggers
(messages, emails, meeting turns) as your character would.

Your role: {role}

Your persona:
{persona_notes}

Hidden knowledge you have (others may not):
{knowledge_block}

Behavior rules:
- React as a real {role} would. One or two concrete sentences. No essays,
  no meta-commentary, no out-of-character narration.
- Match the medium: reply to a DM with chat.dm; reply to an @-mention with
  chat.send in the same channel; reply to an email with email.send.
- "Ignore" is a valid response. If the trigger doesn't warrant a reaction,
  return an empty tool_calls list. You don't have to act every time.
- Don't role-play omniscience. You only know what's in your persona, your
  hidden knowledge, and what's visible in the trigger + relevant context
  below.
- Don't try to be the TPM. They are driving this week; you are reacting.
- Don't fabricate facts you wouldn't actually have. If you don't know, say
  so plainly.

You will respond by calling the `npc_act` tool exactly once. Use empty
tool_calls to stay silent. Use `speech` only when trigger_kind is
"meeting_turn" — otherwise leave it null. `rationale` is one short sentence
on why you reacted (or didn't).
"""


USER_MESSAGE_TEMPLATE = """\
TRIGGER KIND: {trigger_kind}

TRIGGER PAYLOAD:
{payload_json}

RELEVANT CONTEXT FROM YOUR VIEW OF THE WORLD:
{context_excerpt}
"""


NPC_ACT_TOOL: dict[str, Any] = {
    "name": "npc_act",
    "description": (
        "Record your reaction to the trigger. Set tool_calls to the list of "
        "tool calls you want to make (use [] to stay silent). Set speech only "
        "when trigger_kind is 'meeting_turn'. rationale is one short sentence "
        "explaining your reaction."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_calls": {
                "type": "array",
                "description": (
                    "Ordered list of tool calls to perform. Each item is "
                    "{tool: <dotted name like 'chat.send'>, args: <object>}. "
                    "Use [] to stay silent."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "args": {"type": "object"},
                    },
                    "required": ["tool", "args"],
                },
            },
            "speech": {
                "type": ["string", "null"],
                "description": "Spoken line for a meeting turn. Null otherwise.",
            },
            "rationale": {
                "type": "string",
                "description": "One short sentence on why this reaction.",
            },
        },
        "required": ["tool_calls", "rationale"],
    },
}


DEFAULT_MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# The brain
# ---------------------------------------------------------------------------


class AnthropicNPCBrain:
    """`NpcBrain` impl backed by an Anthropic Sonnet 4.6 call per trigger.

    Pass `client=...` for tests; otherwise the brain constructs its own
    Anthropic client from `ANTHROPIC_API_KEY`.
    """

    def __init__(
        self,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2048,
    ) -> None:
        if client is None:
            try:
                import anthropic  # type: ignore
            except ImportError as e:
                raise RuntimeError("anthropic not installed") from e
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def respond(self, context: NpcBrainContext) -> NpcBrainOutput:
        system_prompt = self._build_system_prompt(context)
        user_message = self._build_user_message(context)
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0,
                system=[{
                    "type": "text", "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_message}],
                tools=[NPC_ACT_TOOL],
                tool_choice={"type": "tool", "name": "npc_act"},
            )
        except Exception as exc:
            return NpcBrainOutput(rationale=f"brain_api_error: {exc!s}"[:240])

        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            if getattr(block, "name", "") != "npc_act":
                continue
            return self._parse_tool_use(block.input or {})
        return NpcBrainOutput(rationale="brain_no_tool_use_in_response")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_system_prompt(self, context: NpcBrainContext) -> str:
        if context.knowledge:
            knowledge_block = "\n".join(
                f"- {k}: {v}" for k, v in context.knowledge.items()
            )
        else:
            knowledge_block = "(no privileged knowledge beyond what's visible to everyone)"
        return SYSTEM_PROMPT_TEMPLATE.format(
            role=context.persona_role or "coworker",
            persona_notes=context.persona_notes or "(no specific persona notes)",
            knowledge_block=knowledge_block,
        )

    def _build_user_message(self, context: NpcBrainContext) -> str:
        # Cap the payload JSON to keep tokens bounded on pathological inputs
        # — long tasks/docs etc. The context_excerpt is already capped by
        # the runtime.
        payload_json = json.dumps(context.trigger_payload, indent=2, default=str)
        if len(payload_json) > 4000:
            payload_json = payload_json[:4000] + "\n... (truncated)"
        return USER_MESSAGE_TEMPLATE.format(
            trigger_kind=context.trigger_kind,
            payload_json=payload_json,
            context_excerpt=context.context_excerpt or "(no additional context)",
        )

    def _parse_tool_use(self, data: dict[str, Any]) -> NpcBrainOutput:
        raw_calls = data.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for tc in raw_calls:
            if not isinstance(tc, dict):
                continue
            tool = tc.get("tool")
            args = tc.get("args")
            if not isinstance(tool, str) or not isinstance(args, dict):
                continue
            tool_calls.append(ToolCall(tool=tool, args=args))
        speech = data.get("speech")
        if speech is not None and not isinstance(speech, str):
            speech = None
        rationale = data.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = ""
        return NpcBrainOutput(
            tool_calls=tool_calls,
            speech=speech,
            rationale=rationale[:240],
        )
