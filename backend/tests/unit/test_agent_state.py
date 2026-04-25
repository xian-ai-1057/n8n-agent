"""Unit tests for AgentState HITL fields (C1-1:HITL-SHIP-02).

Verifies that the two new fields added in HITL-SHIP-02 have correct
defaults and accept valid values.
"""

from __future__ import annotations

import pytest

from app.models.agent_state import AgentState


# ---------------------------------------------------------------------------
# Default value tests
# ---------------------------------------------------------------------------


def test_hitl_ship02_session_id_default_none() -> None:
    """session_id defaults to None (run_cli / non-HITL path)."""
    state = AgentState(user_message="hello")
    assert state.session_id is None


def test_hitl_ship02_plan_approved_default_false() -> None:
    """plan_approved defaults to False before await_plan_approval runs."""
    state = AgentState(user_message="build me a workflow")
    assert state.plan_approved is False


# ---------------------------------------------------------------------------
# Field assignment tests
# ---------------------------------------------------------------------------


def test_hitl_ship02_session_id_accepts_string() -> None:
    """session_id accepts a valid string (HITL mode thread id)."""
    state = AgentState(user_message="hi", session_id="sess_abc12345")
    assert state.session_id == "sess_abc12345"


def test_hitl_ship02_plan_approved_can_be_set_true() -> None:
    """plan_approved can be set True (simulating post-approval state)."""
    state = AgentState(user_message="build", plan_approved=True)
    assert state.plan_approved is True


def test_hitl_ship02_existing_fields_unaffected() -> None:
    """New fields do not break any existing AgentState defaults."""
    state = AgentState(user_message="test")
    assert state.retry_count == 0
    assert state.plan == []
    assert state.built_nodes == []
    assert state.error is None
    assert state.draft is None
    assert state.workflow_id is None
    assert state.workflow_url is None


def test_hitl_ship02_both_fields_together() -> None:
    """Both HITL fields can be set simultaneously."""
    state = AgentState(
        user_message="sync slack to sheets",
        session_id="session-001-test",
        plan_approved=True,
    )
    assert state.session_id == "session-001-test"
    assert state.plan_approved is True
