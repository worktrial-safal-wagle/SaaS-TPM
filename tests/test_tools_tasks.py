from __future__ import annotations

import pytest

from sim.scheduler import Scheduler
from sim.store import Person, Task, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.tasks import tasks_ops


def _world() -> tuple[World, Scheduler, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.maya", display_name="Maya", role="engineer"))
    registry = ToolRegistry(world, scheduler, caller_id="person.tpm")
    registry.register_all(tasks_ops())
    return world, scheduler, registry


def test_tasks_create_costs_two_minutes_and_persists():
    world, scheduler, registry = _world()
    result = registry.dispatch(ToolCall(
        tool="tasks.create",
        args={"task_id": "task.PROJ-1", "project": "PROJ", "title": "Fix bug",
              "estimated_effort_seconds": 1800},
    ))
    assert result.ok
    assert result.cost_minutes == 2
    assert scheduler.sim_time == 2
    assert "task.PROJ-1" in world.tasks
    assert world.tasks["task.PROJ-1"].remaining_effort_seconds == 1800


def test_tasks_create_with_unknown_assignee_rejected():
    world, scheduler, registry = _world()
    result = registry.dispatch(ToolCall(
        tool="tasks.create",
        args={"task_id": "task.X", "project": "p", "title": "t",
              "assignee_id": "person.ghost"},
    ))
    assert result.ok is False
    assert "unknown person" in result.error
    assert "person.ghost" in result.error


def test_tasks_create_with_unknown_dependency_rejected():
    world, scheduler, registry = _world()
    result = registry.dispatch(ToolCall(
        tool="tasks.create",
        args={"task_id": "task.X", "project": "p", "title": "t",
              "depends_on": ["task.ghost"]},
    ))
    assert result.ok is False


def test_tasks_update_status_to_done_requires_zero_effort():
    world, scheduler, registry = _world()
    world.add_task(Task(
        id="task.X", project="p", title="t", status="In Progress",
        estimated_effort_seconds=3600, remaining_effort_seconds=600,
    ))
    result = registry.dispatch(ToolCall(
        tool="tasks.update_status",
        args={"task_id": "task.X", "status": "Done"},
    ))
    assert result.ok is False
    assert "remaining effort" in result.error


def test_tasks_update_status_done_allowed_when_no_effort_estimate():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.X", project="p", title="t", status="In Progress"))
    result = registry.dispatch(ToolCall(
        tool="tasks.update_status",
        args={"task_id": "task.X", "status": "Done"},
    ))
    assert result.ok
    assert world.tasks["task.X"].status == "Done"


def test_tasks_log_work_decrements_effort_and_advances_sim_time():
    world, scheduler, registry = _world()
    world.add_task(Task(
        id="task.X", project="p", title="t",
        estimated_effort_seconds=3600, remaining_effort_seconds=3600,
    ))
    result = registry.dispatch(ToolCall(
        tool="tasks.log_work",
        args={"task_id": "task.X", "seconds": 1800},  # 30 min
    ))
    assert result.ok
    assert result.cost_minutes == 30  # 1800s / 60 = 30 min
    assert scheduler.sim_time == 30
    assert world.tasks["task.X"].remaining_effort_seconds == 1800


def test_tasks_log_work_without_effort_set_rejected():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.X", project="p", title="t"))
    result = registry.dispatch(ToolCall(
        tool="tasks.log_work",
        args={"task_id": "task.X", "seconds": 100},
    ))
    assert result.ok is False


def test_tasks_log_work_then_done_allowed():
    world, scheduler, registry = _world()
    world.add_task(Task(
        id="task.X", project="p", title="t", status="In Progress",
        estimated_effort_seconds=1800, remaining_effort_seconds=1800,
    ))
    # Log all the work
    registry.dispatch(ToolCall(tool="tasks.log_work",
                               args={"task_id": "task.X", "seconds": 1800}))
    # Now Done should be allowed
    result = registry.dispatch(ToolCall(
        tool="tasks.update_status",
        args={"task_id": "task.X", "status": "Done"},
    ))
    assert result.ok
    assert world.tasks["task.X"].status == "Done"


def test_tasks_assign_and_reassign():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.X", project="p", title="t"))
    result = registry.dispatch(ToolCall(
        tool="tasks.assign",
        args={"task_id": "task.X", "assignee_id": "person.maya"},
    ))
    assert result.ok
    assert world.tasks["task.X"].assignee_id == "person.maya"
    # Unassign
    result = registry.dispatch(ToolCall(
        tool="tasks.assign",
        args={"task_id": "task.X", "assignee_id": None},
    ))
    assert result.ok
    assert world.tasks["task.X"].assignee_id is None


def test_tasks_list_filters():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.A", project="P1", title="a", status="Todo",
                        assignee_id="person.maya"))
    world.add_task(Task(id="task.B", project="P2", title="b", status="Done"))
    world.add_task(Task(id="task.C", project="P1", title="c", status="Done"))
    result = registry.dispatch(ToolCall(
        tool="tasks.list", args={"project": "P1"},
    ))
    assert {t["id"] for t in result.result["tasks"]} == {"task.A", "task.C"}

    result = registry.dispatch(ToolCall(
        tool="tasks.list", args={"status": "Done"},
    ))
    assert {t["id"] for t in result.result["tasks"]} == {"task.B", "task.C"}

    result = registry.dispatch(ToolCall(
        tool="tasks.list", args={"assignee_id": "person.maya"},
    ))
    assert {t["id"] for t in result.result["tasks"]} == {"task.A"}


def test_tasks_add_dependency_rejects_self_reference():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.X", project="p", title="t"))
    result = registry.dispatch(ToolCall(
        tool="tasks.add_dependency",
        args={"task_id": "task.X", "depends_on_task_id": "task.X"},
    ))
    assert result.ok is False
    assert "cannot depend on itself" in result.error


def test_tasks_add_dependency_rejects_duplicate():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.X", project="p", title="t"))
    world.add_task(Task(id="task.Y", project="p", title="t"))
    registry.dispatch(ToolCall(tool="tasks.add_dependency",
                               args={"task_id": "task.X", "depends_on_task_id": "task.Y"}))
    result = registry.dispatch(ToolCall(
        tool="tasks.add_dependency",
        args={"task_id": "task.X", "depends_on_task_id": "task.Y"},
    ))
    assert result.ok is False
    assert "already present" in result.error


def test_tasks_comment_appends_with_caller_as_author():
    world, scheduler, registry = _world()
    world.add_task(Task(id="task.X", project="p", title="t"))
    registry.dispatch(ToolCall(
        tool="tasks.comment",
        args={"task_id": "task.X", "body": "looks blocked"},
    ))
    comments = world.tasks["task.X"].comments
    assert len(comments) == 1
    assert comments[0].author_id == "person.tpm"
    assert comments[0].body == "looks blocked"
