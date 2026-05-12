from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Notification, Person, World
from sim.tools import ToolCall, ToolRegistry, all_ops
from sim.tools.directory import directory_ops
from sim.tools.notifications import notifications_ops


def _world():
    w = World()
    s = Scheduler()
    w.add_person(Person(id="person.tpm", display_name="TPM", role="tpm",
                        team="exec", is_agent=True))
    w.add_person(Person(id="person.maya", display_name="Maya", role="engineer",
                        team="eng", manager_id="person.tpm"))
    w.add_person(Person(id="person.bob", display_name="Bob", role="designer",
                        team="design"))
    return w, s


def test_directory_list_all():
    world, scheduler = _world()
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(directory_ops())
    result = reg.dispatch(ToolCall(tool="directory.list", args={}))
    assert {p["id"] for p in result.result["people"]} == {
        "person.tpm", "person.maya", "person.bob",
    }


def test_directory_list_filtered_by_team():
    world, scheduler = _world()
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(directory_ops())
    result = reg.dispatch(ToolCall(
        tool="directory.list", args={"team": "eng"},
    ))
    assert [p["id"] for p in result.result["people"]] == ["person.maya"]


def test_directory_get_unknown_person():
    world, scheduler = _world()
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(directory_ops())
    result = reg.dispatch(ToolCall(
        tool="directory.get", args={"person_id": "person.ghost"},
    ))
    assert result.ok is False


def test_notifications_list_only_for_caller():
    world, scheduler = _world()
    world.add_notification(Notification(
        id="notif.1", recipient_id="person.tpm", kind="x",
        payload={}, created_at=10,
    ))
    world.add_notification(Notification(
        id="notif.2", recipient_id="person.maya", kind="x",
        payload={}, created_at=20,
    ))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(notifications_ops())
    result = reg.dispatch(ToolCall(tool="notifications.list", args={}))
    assert [n["id"] for n in result.result["notifications"]] == ["notif.1"]


def test_notifications_mark_read():
    world, scheduler = _world()
    world.add_notification(Notification(
        id="notif.1", recipient_id="person.tpm", kind="x",
        payload={}, created_at=10,
    ))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(notifications_ops())
    reg.dispatch(ToolCall(
        tool="notifications.mark_read", args={"notification_id": "notif.1"},
    ))
    listed = reg.dispatch(ToolCall(
        tool="notifications.list", args={"only_unseen": True},
    ))
    assert listed.result["notifications"] == []


def test_all_ops_bundle_has_expected_tools():
    ops = all_ops()
    names = {op.name for op in ops}
    # Spot-check one op per namespace
    for required in [
        "chat.send", "chat.dm", "chat.read", "chat.list", "chat.mark_read",
        "email.send", "email.read", "email.list",
        "calendar.list", "calendar.get", "calendar.rsvp", "calendar.create",
        "tasks.list", "tasks.create", "tasks.update_status", "tasks.log_work",
        "tasks.assign", "tasks.add_dependency", "tasks.comment",
        "docs.create", "docs.read", "docs.edit", "docs.list", "docs.comment",
        "meetings.attend", "meetings.get_transcript",
        "directory.list", "directory.get",
        "notifications.list", "notifications.mark_read",
        "wait.until",
        "idle.until", "abandon.current",
    ]:
        assert required in names, f"missing tool: {required}"


def test_registry_tool_specs_returns_sdk_compatible_specs():
    """Every op produces a spec whose name matches Anthropic's tool-name regex."""
    import re
    world, scheduler = _world()
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(all_ops())
    specs = reg.tool_specs()
    assert len(specs) >= 30
    pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
    for spec in specs:
        assert pattern.match(spec["name"]), f"invalid SDK name: {spec['name']!r}"
        assert spec["description"], f"missing description for {spec['name']}"
        assert spec["input_schema"]["type"] == "object"
        # Round-trip name translation
        from sim.tools.base import from_sdk_name, sdk_name
        assert sdk_name(from_sdk_name(spec["name"])) == spec["name"]


def test_registry_dispatch_accepts_sdk_name():
    """The registry should accept both `chat.send` and `chat__send`."""
    world, scheduler = _world()
    world.add_channel(__import__("sim.store", fromlist=["Channel"]).Channel(
        id="channel.general", name="general", members=["person.tpm"],
    ))
    reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    reg.register_all(all_ops())
    result = reg.dispatch(ToolCall(
        tool="chat__send",
        args={"channel_id": "channel.general", "body": "via sdk name"},
    ))
    assert result.ok, result.error
