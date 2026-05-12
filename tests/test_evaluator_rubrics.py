"""Unit tests for the artifact locator helpers in sim.evaluator.rubrics."""

from __future__ import annotations

from typing import Any

from sim.evaluator.rubrics import (
    _locate_decision_signal_body,
    _locate_email_thread_body,
)


def _world(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "messages": [], "channels": [], "emails": [], "email_threads": [],
        "tasks": [], "docs": [], "people": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# email_thread locator — latest beats first
# ---------------------------------------------------------------------------


def test_email_thread_locator_returns_latest_not_first():
    """When the agent sends multiple emails to the same thread, the locator
    grades the LATEST send — earlier drafts shouldn't outrank a final."""
    world = _world(
        email_threads=[{"id": "t.1", "subject": "Launch readiness"}],
        emails=[
            {"thread_id": "t.1", "sender_id": "person.tpm",
             "to": ["person.alex"], "cc": [], "sim_time": 100,
             "body": "draft — risks tbd"},
            {"thread_id": "t.1", "sender_id": "person.tpm",
             "to": ["person.alex"], "cc": [], "sim_time": 3000,
             "body": "FINAL: launch Wed, rollback DRI Kai, risk migration flake"},
        ],
    )
    body = _locate_email_thread_body(world, {
        "thread_subject_contains": "launch",
        "sender_id": "person.tpm",
        "recipient_id": "person.alex",
    })
    assert body is not None
    assert "FINAL" in body
    assert "draft" not in body


def test_email_thread_locator_returns_none_when_no_match():
    world = _world(
        email_threads=[{"id": "t.1", "subject": "unrelated thread"}],
        emails=[{"thread_id": "t.1", "sender_id": "person.tpm",
                 "to": ["person.alex"], "cc": [], "sim_time": 100, "body": "x"}],
    )
    body = _locate_email_thread_body(world, {
        "thread_subject_contains": "launch",
        "sender_id": "person.tpm",
        "recipient_id": "person.alex",
    })
    assert body is None


# ---------------------------------------------------------------------------
# decision_signal locator — normalized fuzzy keyword match
# ---------------------------------------------------------------------------


def test_decision_signal_locator_matches_keyword_with_hyphen_against_body_without():
    """Body 'we'll use ISO8601 timestamps' should match keyword 'ISO-8601'."""
    world = _world(
        messages=[{"sender_id": "person.tpm", "channel_id": "channel.eng",
                   "body": "decision: we'll use ISO8601 timestamps in the API",
                   "sim_time": 200, "mentions": []}],
    )
    body = _locate_decision_signal_body(world, {
        "keywords_any": ["ISO-8601"],
        "author_id": "person.tpm",
    })
    assert body is not None
    assert "ISO8601" in body


def test_decision_signal_locator_matches_keyword_without_hyphen_against_body_with():
    """The reverse direction: keyword 'ISO8601' matches body 'ISO-8601'."""
    world = _world(
        messages=[{"sender_id": "person.tpm", "channel_id": "channel.eng",
                   "body": "after discussion, ISO-8601 it is",
                   "sim_time": 200, "mentions": []}],
    )
    body = _locate_decision_signal_body(world, {
        "keywords_any": ["ISO8601"],
        "author_id": "person.tpm",
    })
    assert body is not None
    assert "ISO-8601" in body


def test_decision_signal_locator_respects_author_filter():
    """author_id gate: only matching-author bodies are surfaced."""
    world = _world(
        messages=[
            {"sender_id": "person.kai", "channel_id": "channel.eng",
             "body": "we should use ISO-8601",
             "sim_time": 100, "mentions": []},
            {"sender_id": "person.tpm", "channel_id": "channel.eng",
             "body": "agreed, ISO-8601",
             "sim_time": 200, "mentions": []},
        ],
    )
    body = _locate_decision_signal_body(world, {
        "keywords_any": ["ISO-8601"],
        "author_id": "person.tpm",
    })
    assert body is not None
    assert "agreed" in body
    assert "we should use" not in body


def test_decision_signal_locator_no_match_returns_none():
    world = _world(
        messages=[{"sender_id": "person.tpm", "channel_id": "channel.eng",
                   "body": "let's talk about something else",
                   "sim_time": 200, "mentions": []}],
    )
    body = _locate_decision_signal_body(world, {
        "keywords_any": ["ISO-8601", "UTC"],
        "author_id": "person.tpm",
    })
    assert body is None
