"""Anthropic-backed reference agent.

Uses the Anthropic SDK's **structured tool-calling** API. The agent does not
parse JSON out of free-form prose — it receives a `tool_use` content block
from the model and returns it as a `ToolCall`. The model can only emit names
from the tool list we hand it, which eliminates name-hallucination.

The system prompt + tool list are stable, so prompt caching on them is
effective. Only the briefing is volatile.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from sim.agent.briefing import Briefing, render_briefing
from sim.tools import ToolCall
from sim.tools.base import from_sdk_name


DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are the Technical Program Manager at Lumen Analytics, a ~80-person SaaS
analytics company. It is your first week on the job. You have a single
work week to land the v2.4 launch, untangle two open audit-log decisions,
and decide a pipeline RFC.

You will receive a **Briefing** at the start of each turn. You may take
exactly one action per turn — pick the most leveraged thing.

Principles, in order:

1. **Act, don't loop.** If the briefing's "Your recent actions + results"
   section shows you read an artifact, you have it. Do something with
   that knowledge — send a message, update a task, write a doc. Reading
   the same thing again is scored negatively and accomplishes nothing.
2. **Use real IDs only.** IDs are slugs like `channel.engineering`,
   `thread.welcome`, `task.V24-5`, `doc.audit_prd`. They are NOT
   natural-language names. NEVER guess an id — list first (`chat.list`,
   `email.list`, `tasks.list`, `docs.list`, `directory.list`), then use
   the exact id from the listing. If an unknown-id error gives you a
   "did you mean" suggestion, USE that suggestion verbatim.
3. **Move projects forward with minimum noise.** Volume of messages is
   not a measure of value; an unnecessary message is a small negative.
4. Discover hidden information before acting (read DMs carefully).
5. Consult the engineer of record before committing to dates.
6. Stakeholder SLAs matter: 2h to acknowledge customer escalations, 4h to
   the CEO on direct asks.
7. Hygiene: keep the task board honest. Don't claim Done without effort.

If you see a ⚠️ Stall warning in the briefing, you have been reading
without advancing time. STOP reading. Take a write action this turn
(`chat.send`, `chat.dm`, `email.send`, `tasks.update_status`,
`tasks.comment`, `docs.edit`, `docs.comment`) OR call
`idle__until` to skip ahead and the driver will repoll you later.

Invoke exactly one of the tools provided. The available tool names and
their input schemas are listed in the `tools` parameter.
"""


# ---------------------------------------------------------------------------
# Model client protocol
# ---------------------------------------------------------------------------


class ModelClient(Protocol):
    def call_with_tools(
        self, *, system: str, user_message: str, tools: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """Return `(tool_name, tool_input)` from the model's tool_use block.

        Raise `RuntimeError` if the model didn't emit a tool_use block.
        """
        ...


# ---------------------------------------------------------------------------
# Anthropic client implementation
# ---------------------------------------------------------------------------


class AnthropicModelClient:
    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 1024) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError("anthropic package not installed; install with [anthropic]") from e
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def call_with_tools(
        self, *, system: str, user_message: str, tools: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
            ],
            tools=tools,
            tool_choice={"type": "any"},  # force a tool call, no free prose
            messages=[{"role": "user", "content": user_message}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                # SDK gives us .name and .input directly
                return block.name, dict(block.input or {})
        text = "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
        )
        raise RuntimeError(
            f"model returned no tool_use; stop_reason={response.stop_reason!r}; text={text!r}"
        )


# ---------------------------------------------------------------------------
# Reference agent
# ---------------------------------------------------------------------------


class ReferenceAgent:
    """LLM-driven agent using the Anthropic SDK's structured tools.

    Construct with the list of `tool_specs` produced by
    `ToolRegistry.tool_specs()` — these are the only tools the model can
    emit. SDK-name translation happens at the boundary (`chat.send` ↔
    `chat__send`); the registry accepts both forms.
    """

    def __init__(
        self,
        client: ModelClient | None = None,
        tool_specs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.client = client or AnthropicModelClient()
        if tool_specs is None:
            raise ValueError("tool_specs is required (pass ToolRegistry.tool_specs())")
        self.tool_specs = tool_specs

    def decide(self, briefing: Briefing) -> ToolCall:
        rendered = render_briefing(briefing)
        name, args = self.client.call_with_tools(
            system=SYSTEM_PROMPT,
            user_message=rendered,
            tools=self.tool_specs,
        )
        # Translate `chat__send` → `chat.send`; registry also accepts SDK form.
        return ToolCall(tool=from_sdk_name(name), args=args)
