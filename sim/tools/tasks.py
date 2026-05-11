"""`tasks.*` tool operations.

Task effort is first-class: `tasks.log_work` decrements `remaining_effort_seconds`
*and* costs the agent that much sim-time. A task with positive remaining effort
cannot be moved to Done — agents can't will work done.

Dependencies are tracked but not enforced as hard blockers (a Blocked task can
still be moved to In Progress; the eval grader checks whether the agent
respected them in practice).
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from sim.scheduler import Scheduler
from sim.store import Task, TaskComment, World
from sim.tools.base import ToolOp
from sim.tools.costs import (
    COST_TASKS_ADD_DEPENDENCY,
    COST_TASKS_ASSIGN,
    COST_TASKS_COMMENT,
    COST_TASKS_CREATE,
    COST_TASKS_GET,
    COST_TASKS_LIST,
    COST_TASKS_UPDATE_STATUS,
    cost_log_work,
)
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


# ---------------------------------------------------------------------------
# tasks.list
# ---------------------------------------------------------------------------


class TasksListArgs(BaseModel):
    project: str | None = None
    status: str | None = None
    assignee_id: str | None = None


def tasks_list(world: World, scheduler: Scheduler, args: TasksListArgs, caller_id: str) -> dict[str, Any]:
    out = []
    for t in sorted(world.tasks.values(), key=lambda t: t.id):
        if args.project and t.project != args.project:
            continue
        if args.status and t.status != args.status:
            continue
        if args.assignee_id and t.assignee_id != args.assignee_id:
            continue
        out.append(t.model_dump())
    return {"tasks": out}


# ---------------------------------------------------------------------------
# tasks.get
# ---------------------------------------------------------------------------


class TasksGetArgs(BaseModel):
    task_id: str


def tasks_get(world: World, scheduler: Scheduler, args: TasksGetArgs, caller_id: str) -> dict[str, Any]:
    task = world.tasks.get(args.task_id)
    if task is None:
        raise ToolError(suggest_id("task", args.task_id, world.tasks.keys()))
    return task.model_dump()


# ---------------------------------------------------------------------------
# tasks.create
# ---------------------------------------------------------------------------


class TasksCreateArgs(BaseModel):
    task_id: str
    project: str
    title: str
    description: str = ""
    assignee_id: str | None = None
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    estimated_effort_seconds: int | None = None
    deadline_sim_time: int | None = None
    depends_on: list[str] = Field(default_factory=list)


def tasks_create(world: World, scheduler: Scheduler, args: TasksCreateArgs, caller_id: str) -> dict[str, Any]:
    if args.task_id in world.tasks:
        raise ToolError(f"task already exists: {args.task_id}")
    if args.assignee_id and args.assignee_id not in world.people:
        raise ToolError(suggest_id("person (assignee)", args.assignee_id, world.people.keys()))
    for dep in args.depends_on:
        if dep not in world.tasks:
            raise ToolError(suggest_id("task (depends_on)", dep, world.tasks.keys()))
    task = Task(
        id=args.task_id, project=args.project, title=args.title,
        description=args.description, assignee_id=args.assignee_id,
        reporter_id=caller_id, priority=args.priority,
        estimated_effort_seconds=args.estimated_effort_seconds,
        remaining_effort_seconds=args.estimated_effort_seconds,
        deadline_sim_time=args.deadline_sim_time,
        depends_on=list(args.depends_on), status="Backlog",
    )
    world.add_task(task)
    return task.model_dump()


# ---------------------------------------------------------------------------
# tasks.update_status
# ---------------------------------------------------------------------------


class TasksUpdateStatusArgs(BaseModel):
    task_id: str
    status: Literal["Backlog", "Todo", "In Progress", "Blocked", "In Review", "Done"]


def tasks_update_status(world: World, scheduler: Scheduler, args: TasksUpdateStatusArgs, caller_id: str) -> dict[str, Any]:
    task = world.tasks.get(args.task_id)
    if task is None:
        raise ToolError(suggest_id("task", args.task_id, world.tasks.keys()))
    if args.status == "Done":
        remaining = task.remaining_effort_seconds
        if remaining is not None and remaining > 0:
            raise ToolError(
                f"cannot mark Done: {remaining}s of remaining effort still logged",
            )
    world.update_task_status(args.task_id, args.status)
    return task.model_dump()


# ---------------------------------------------------------------------------
# tasks.assign
# ---------------------------------------------------------------------------


class TasksAssignArgs(BaseModel):
    task_id: str
    assignee_id: str | None


def tasks_assign(world: World, scheduler: Scheduler, args: TasksAssignArgs, caller_id: str) -> dict[str, Any]:
    if args.task_id not in world.tasks:
        raise ToolError(suggest_id("task", args.task_id, world.tasks.keys()))
    if args.assignee_id and args.assignee_id not in world.people:
        raise ToolError(suggest_id("person (assignee)", args.assignee_id, world.people.keys()))
    world.assign_task(args.task_id, args.assignee_id)
    return world.tasks[args.task_id].model_dump()


# ---------------------------------------------------------------------------
# tasks.log_work
# ---------------------------------------------------------------------------


class TasksLogWorkArgs(BaseModel):
    task_id: str
    seconds: int = Field(gt=0)


def tasks_log_work(world: World, scheduler: Scheduler, args: TasksLogWorkArgs, caller_id: str) -> dict[str, Any]:
    task = world.tasks.get(args.task_id)
    if task is None:
        raise ToolError(suggest_id("task", args.task_id, world.tasks.keys()))
    if task.remaining_effort_seconds is None:
        raise ToolError(f"task has no remaining_effort_seconds, cannot log work")
    world.log_work(args.task_id, args.seconds)
    return task.model_dump()


# ---------------------------------------------------------------------------
# tasks.add_dependency
# ---------------------------------------------------------------------------


class TasksAddDependencyArgs(BaseModel):
    task_id: str
    depends_on_task_id: str


def tasks_add_dependency(world: World, scheduler: Scheduler, args: TasksAddDependencyArgs, caller_id: str) -> dict[str, Any]:
    if args.task_id not in world.tasks:
        raise ToolError(suggest_id("task", args.task_id, world.tasks.keys()))
    if args.depends_on_task_id not in world.tasks:
        raise ToolError(suggest_id("task (dependency)", args.depends_on_task_id, world.tasks.keys()))
    if args.depends_on_task_id == args.task_id:
        raise ToolError("task cannot depend on itself")
    task = world.tasks[args.task_id]
    if args.depends_on_task_id in task.depends_on:
        raise ToolError(f"dependency already present")
    task.depends_on.append(args.depends_on_task_id)
    world._emit(
        "task_dependency_added",
        {"task_id": args.task_id, "depends_on": args.depends_on_task_id},
    )
    return task.model_dump()


# ---------------------------------------------------------------------------
# tasks.comment
# ---------------------------------------------------------------------------


class TasksCommentArgs(BaseModel):
    task_id: str
    body: str


def tasks_comment(world: World, scheduler: Scheduler, args: TasksCommentArgs, caller_id: str) -> dict[str, Any]:
    task = world.tasks.get(args.task_id)
    if task is None:
        raise ToolError(suggest_id("task", args.task_id, world.tasks.keys()))
    if not args.body.strip():
        raise ToolError("comment body cannot be empty")
    comment = TaskComment(author_id=caller_id, body=args.body, sim_time=scheduler.sim_time)
    task.comments.append(comment)
    world._emit(
        "task_commented",
        {"task_id": args.task_id, "author_id": caller_id, "sim_time": scheduler.sim_time},
    )
    return task.model_dump()


# ---------------------------------------------------------------------------
# Op bundle
# ---------------------------------------------------------------------------


def tasks_ops() -> list[ToolOp]:
    return [
        ToolOp("tasks.list", TasksListArgs, COST_TASKS_LIST, tasks_list,
               description="List tasks, optionally filtered by project, status, or assignee."),
        ToolOp("tasks.get", TasksGetArgs, COST_TASKS_GET, tasks_get,
               description="Get the full details of a single task by id."),
        ToolOp("tasks.create", TasksCreateArgs, COST_TASKS_CREATE, tasks_create,
               description="Create a new task. You become the reporter. Optionally set assignee, priority, effort, deadline, dependencies."),
        ToolOp("tasks.update_status", TasksUpdateStatusArgs, COST_TASKS_UPDATE_STATUS, tasks_update_status,
               description="Move a task to a new status. Statuses: Backlog, Todo, In Progress, Blocked, In Review, Done. Tasks with positive remaining effort cannot be marked Done."),
        ToolOp("tasks.assign", TasksAssignArgs, COST_TASKS_ASSIGN, tasks_assign,
               description="Assign (or unassign with null) a task to a person."),
        ToolOp("tasks.log_work", TasksLogWorkArgs, cost_log_work, tasks_log_work,
               description="Log work in seconds on a task. Decrements `remaining_effort_seconds` and costs that many sim-minutes."),
        ToolOp("tasks.add_dependency", TasksAddDependencyArgs, COST_TASKS_ADD_DEPENDENCY, tasks_add_dependency,
               description="Add a dependency so one task blocks another. No self-references; no duplicates."),
        ToolOp("tasks.comment", TasksCommentArgs, COST_TASKS_COMMENT, tasks_comment,
               description="Add a comment to a task — useful for status updates and decisions."),
    ]
