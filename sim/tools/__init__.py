from sim.tools.base import ToolCall, ToolOp, ToolResult
from sim.tools.registry import ToolError, ToolRegistry

__all__ = ["ToolCall", "ToolError", "ToolOp", "ToolResult", "ToolRegistry"]


def all_ops() -> list[ToolOp]:
    """The full tool surface, in a single bundle."""
    from sim.tools.abandon import abandon_ops
    from sim.tools.calendar import calendar_ops
    from sim.tools.chat import chat_ops
    from sim.tools.directory import directory_ops
    from sim.tools.docs import docs_ops
    from sim.tools.email import email_ops
    from sim.tools.idle import idle_ops
    from sim.tools.meetings import meetings_ops
    from sim.tools.notifications import notifications_ops
    from sim.tools.tasks import tasks_ops
    from sim.tools.wait import wait_ops

    ops: list[ToolOp] = []
    for fn in (chat_ops, email_ops, calendar_ops, tasks_ops, docs_ops,
               meetings_ops, directory_ops, notifications_ops, wait_ops,
               idle_ops, abandon_ops):
        ops.extend(fn())
    return ops
