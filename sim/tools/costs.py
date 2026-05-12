"""Sim-time costs for tool operations.

Costs are *declared upfront*. The agent cannot opt out, cannot reduce them by
batching, and cannot pretend a long action was free. This is what makes
"spam everyone" actually burn the morning — and what makes "read the same
inbox 30 times" actually consume the day.

Costs are integer minutes. For variable-cost tools (docs.create, email.send,
log_work), a function takes parsed args and returns the cost; for fixed-cost
tools, the value is a plain int.

**Reads have real cost.** Originally all read/list ops were 0, which made
sim_time uncoupled from cognitive effort: an agent could spend 50 turns
scanning the inbox without "spending" any of its workday. That broke the
spec's simulated-time premise — max_turns became the binding budget instead
of sim_time. A real TPM scanning their inbox takes 30s–2min per artifact;
that's now reflected here. The agent is still free to read, but reading is
now a real time choice with a real opportunity cost.
"""

from __future__ import annotations

import math
from typing import Any, Callable

# Kinds the wait-for-next-event tool should advance to. NPC runtime and other
# event producers tag scheduler events with these kinds so the agent can
# "wait until something happens to me."
AGENT_VISIBLE_EVENT_KINDS: set[str] = {
    "chat_to_agent",
    "email_to_agent",
    "calendar_event_start",
    "agent_heartbeat",
}

# Fixed costs (minutes)
COST_CHAT_SEND = 1
COST_CHAT_DM = 1
COST_CHAT_READ = 1   # scanning a small batch of messages
COST_CHAT_LIST = 1   # glancing at the channel list
COST_CHAT_MARK_READ = 0   # one click — basically free

COST_TASKS_LIST = 1   # scanning the board
COST_TASKS_GET = 1    # opening a task
COST_TASKS_CREATE = 2
COST_TASKS_UPDATE_STATUS = 1
COST_TASKS_ASSIGN = 1
COST_TASKS_ADD_DEPENDENCY = 1
COST_TASKS_COMMENT = 1

# Email costs
COST_EMAIL_LIST = 1
COST_EMAIL_READ = 2   # reading an email thoroughly takes longer than a chat
COST_EMAIL_SEND_BASE = 3

# Calendar
COST_CAL_LIST = 1
COST_CAL_GET = 1
COST_CAL_RSVP = 1
COST_CAL_CREATE = 2

# Docs
COST_DOC_LIST = 1
COST_DOC_READ = 5   # reading a doc takes attention (longer than a chat)
COST_DOC_COMMENT = 2

# Directory / notifications
COST_DIR_LIST = 1
COST_DIR_GET = 1
COST_DIR_PRESENCE = 1  # peek at someone's busy state — same as a directory lookup
COST_NOTIF_LIST = 1
COST_NOTIF_MARK_READ = 0   # one click


def cost_email_send(args: Any) -> int:
    """Email cost = base (3 min) + 1 min per 300 chars beyond the first 300."""
    body = getattr(args, "body", "") or ""
    overflow = max(0, len(body) - 300)
    return COST_EMAIL_SEND_BASE + math.ceil(overflow / 300)


def cost_doc_create(args: Any) -> int:
    """A doc takes 15 min base + 1 min per 100 chars of body, capped at 90 min."""
    body = getattr(args, "body", "") or ""
    return min(90, 15 + math.ceil(len(body) / 100))


def cost_doc_edit(args: Any) -> int:
    """An edit takes 5 min base + 1 min per 100 chars of body, capped at 60 min."""
    body = getattr(args, "body", "") or ""
    return min(60, 5 + math.ceil(len(body) / 100))


def cost_meeting_attend(args: Any) -> int:
    """Will be replaced at dispatch time with the meeting's actual duration —
    this initial estimate is a placeholder. The handler advances the scheduler
    by the real duration and the registry reports it as `cost_minutes`."""
    return 0


def cost_chat_send_by_length(args: Any) -> int:
    """A long message costs more — roughly 1 minute per 200 chars of body, min 1."""
    body = getattr(args, "body", "") or ""
    return max(COST_CHAT_SEND, math.ceil(len(body) / 200))


def cost_log_work(args: Any) -> int:
    """Logging N seconds of work *is* spending N seconds — convert to minutes, min 1."""
    seconds = getattr(args, "seconds", 0)
    return max(1, math.ceil(seconds / 60))
