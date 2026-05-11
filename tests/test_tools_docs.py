from __future__ import annotations

from sim.scheduler import Scheduler
from sim.store import Doc, DocVersion, Person, World
from sim.tools import ToolCall, ToolRegistry
from sim.tools.docs import docs_ops


def _setup(acl: list[str] | None = None) -> tuple[World, Scheduler, ToolRegistry, ToolRegistry]:
    world = World()
    scheduler = Scheduler()
    world.add_person(Person(id="person.tpm", display_name="TPM", role="tpm", is_agent=True))
    world.add_person(Person(id="person.alice", display_name="Alice", role="eng"))
    tpm_reg = ToolRegistry(world, scheduler, caller_id="person.tpm")
    tpm_reg.register_all(docs_ops())
    alice_reg = ToolRegistry(world, scheduler, caller_id="person.alice")
    alice_reg.register_all(docs_ops())
    return world, scheduler, tpm_reg, alice_reg


def test_docs_create_then_read_returns_v1():
    world, scheduler, tpm_reg, _ = _setup()
    result = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec", "body": "v1 body"},
    ))
    assert result.ok
    read = tpm_reg.dispatch(ToolCall(tool="docs.read", args={"doc_id": "doc.spec"}))
    assert read.ok
    assert read.result["body"] == "v1 body"
    assert read.result["version"] == 1


def test_docs_edit_creates_new_version():
    world, scheduler, tpm_reg, _ = _setup()
    tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec", "body": "v1"},
    ))
    edit = tpm_reg.dispatch(ToolCall(
        tool="docs.edit",
        args={"doc_id": "doc.spec", "body": "v2"},
    ))
    assert edit.ok
    assert edit.result["version"] == 2
    # Latest read returns v2
    latest = tpm_reg.dispatch(ToolCall(tool="docs.read", args={"doc_id": "doc.spec"}))
    assert latest.result["body"] == "v2"
    assert latest.result["version"] == 2
    # Explicit v1 still readable
    v1 = tpm_reg.dispatch(ToolCall(
        tool="docs.read", args={"doc_id": "doc.spec", "version": 1},
    ))
    assert v1.result["body"] == "v1"


def test_docs_create_cost_scales_with_body_length():
    world, scheduler, tpm_reg, _ = _setup()
    short = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.s", "title": "s", "body": "hi"},
    ))
    assert short.cost_minutes == 16  # 15 base + ceil(2/100)=1
    long_body = "x" * 5000
    big = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.l", "title": "l", "body": long_body},
    ))
    # 15 + ceil(5000/100)=50, capped at 90
    assert big.cost_minutes == 65


def test_docs_acl_blocks_unauthorized_view():
    world, scheduler, tpm_reg, alice_reg = _setup()
    tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.private", "title": "private", "body": "secret",
              "acl_view": ["person.tpm"]},
    ))
    result = alice_reg.dispatch(ToolCall(tool="docs.read", args={"doc_id": "doc.private"}))
    assert result.ok is False
    assert "not authorized" in result.error


def test_docs_acl_edit_distinct_from_view():
    world, scheduler, tpm_reg, alice_reg = _setup()
    tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.x", "title": "x", "body": "x",
              "acl_view": ["person.tpm", "person.alice"],
              "acl_edit": ["person.tpm"]},
    ))
    # Alice can read but not edit
    read = alice_reg.dispatch(ToolCall(tool="docs.read", args={"doc_id": "doc.x"}))
    assert read.ok
    edit = alice_reg.dispatch(ToolCall(
        tool="docs.edit", args={"doc_id": "doc.x", "body": "tampered"},
    ))
    assert edit.ok is False


def test_docs_comment_appends_and_records_author():
    world, scheduler, tpm_reg, _ = _setup()
    tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec", "body": "v1"},
    ))
    tpm_reg.dispatch(ToolCall(
        tool="docs.comment",
        args={"doc_id": "doc.spec", "body": "needs clarification"},
    ))
    doc = world.docs["doc.spec"]
    assert len(doc.comments) == 1
    assert doc.comments[0].author_id == "person.tpm"
    assert doc.comments[0].body == "needs clarification"
