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


def test_docs_create_missing_body_error_names_field():
    """A docs.create with missing `body` must surface the field name in the error.

    Real Sonnet runs got stuck looping the same incomplete call 10+ times because
    the error was just `"Field required"` with no field name. Recovering from a
    tool-arg error requires knowing which arg was wrong.
    """
    world, scheduler, tpm_reg, _ = _setup()
    result = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec"},  # body missing
    ))
    assert not result.ok
    assert "body" in result.error
    assert "Field required" in result.error
    assert "type=missing" in result.error


def test_docs_create_multi_field_error_reports_all():
    world, scheduler, tpm_reg, _ = _setup()
    result = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={},  # missing doc_id, title, body
    ))
    assert not result.ok
    assert "doc_id" in result.error
    assert "title" in result.error
    assert "body" in result.error


# ---------------------------------------------------------------------------
# Failure-cost policy: handler-level (ToolError) failures cost the *declared*
# sim-time, not the 1-min fallback. This makes failure-loop bugs surface in
# the sim-time budget rather than getting silently amortized to nothing.
# ---------------------------------------------------------------------------


def test_docs_create_duplicate_id_costs_declared_amount_on_failure():
    """A docs.create that fails because `doc_id` is already taken still costs
    the full declared docs.create cost (~15+ min). A 10-call failure loop on
    docs.create burns 150+ sim-min — that's the point of charging declared
    cost on failure (vs the old 1-min flat fee that hid the bug)."""
    from sim.tools.costs import cost_doc_create

    world, scheduler, tpm_reg, _ = _setup()
    # Seed an existing doc so the second create collides.
    first = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec", "body": "v1 body here"},
    ))
    assert first.ok
    start = scheduler.sim_time

    # Use a body for which the declared cost is well above the 1-min fallback,
    # so we can distinguish the new behaviour from the old.
    body = "x" * 600  # 15 + ceil(600/100) = 21 min
    expected_cost = cost_doc_create(type("A", (), {"body": body})())
    assert expected_cost == 21  # sanity-check the formula

    result = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Dup", "body": body},
    ))
    assert result.ok is False
    assert "already exists" in result.error
    # Declared cost, NOT the 1-min fallback.
    assert result.cost_minutes == expected_cost
    assert scheduler.sim_time == start + expected_cost


def test_docs_create_acl_failure_costs_declared_amount():
    """A docs.edit blocked by acl_edit still costs the action's declared
    sim-time on failure (Sonnet's docs.create failure loop equivalent)."""
    from sim.tools.costs import cost_doc_edit

    world, scheduler, tpm_reg, alice_reg = _setup()
    tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.x", "title": "x", "body": "v1",
              "acl_view": ["person.tpm", "person.alice"],
              "acl_edit": ["person.tpm"]},  # alice can view, not edit
    ))
    start = scheduler.sim_time

    new_body = "y" * 500  # 5 + ceil(500/100) = 10 min
    expected_cost = cost_doc_edit(type("A", (), {"body": new_body})())
    assert expected_cost == 10

    edit = alice_reg.dispatch(ToolCall(
        tool="docs.edit",
        args={"doc_id": "doc.x", "body": new_body},
    ))
    assert edit.ok is False
    assert "not authorized" in edit.error
    assert edit.cost_minutes == expected_cost
    assert scheduler.sim_time == start + expected_cost


def test_docs_create_validation_error_costs_fallback_one_minute():
    """A docs.create that fails pydantic validation (missing required field)
    falls back to FAILURE_COST_MINUTES because there's no parsed `args` to
    pass to `cost_for`. The agent's "attempt" was so malformed they didn't
    even know what they were attempting; charging the full declared cost
    would be unfair."""
    from sim.tools.registry import FAILURE_COST_MINUTES

    world, scheduler, tpm_reg, _ = _setup()
    start = scheduler.sim_time
    result = tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec"},  # body missing
    ))
    assert result.ok is False
    assert "body" in result.error  # the existing field-name behaviour
    assert result.cost_minutes == FAILURE_COST_MINUTES
    assert FAILURE_COST_MINUTES == 1
    assert scheduler.sim_time == start + FAILURE_COST_MINUTES


def test_docs_create_failure_loop_burns_full_declared_cost():
    """The flagship regression case: a 10-call docs.create failure loop that
    used to burn 10 sim-min now burns ~210 sim-min, so it shows up in eval
    as the budget problem it is."""
    from sim.tools.costs import cost_doc_create

    world, scheduler, tpm_reg, _ = _setup()
    # Seed the conflict so every subsequent create fails on duplicate.
    body = "x" * 600
    tpm_reg.dispatch(ToolCall(
        tool="docs.create",
        args={"doc_id": "doc.spec", "title": "Spec", "body": body},
    ))
    per_call = cost_doc_create(type("A", (), {"body": body})())  # 21 min
    after_first = scheduler.sim_time

    failures = 0
    for _ in range(10):
        r = tpm_reg.dispatch(ToolCall(
            tool="docs.create",
            args={"doc_id": "doc.spec", "title": "Spec", "body": body},
        ))
        assert r.ok is False
        failures += 1
    # Each failure costs `per_call` minutes, not 1.
    assert scheduler.sim_time == after_first + 10 * per_call
    assert failures == 10
