# C1-5:A-MSG-01 - ChatRequest.message max_length 2000 → 8000
# C1-5:A-RESP-01 - ChatResponse.plan field (list[dict] | None)

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.api import ChatRequest, ChatResponse


# ---------------------------------------------------------------------------
# A-MSG-01: message field max_length = 8000
# ---------------------------------------------------------------------------


def test_a_msg_01_accepts_8000_chars() -> None:
    """Exactly 8000 characters must be accepted (new limit)."""
    req = ChatRequest(message="x" * 8000)
    assert len(req.message) == 8000


def test_a_msg_01_rejects_8001_chars() -> None:
    """8001 characters must be rejected as too long."""
    with pytest.raises(ValidationError):
        ChatRequest(message="x" * 8001)


def test_a_msg_01_still_rejects_empty() -> None:
    """Empty string must still be rejected (min_length=1 unchanged)."""
    with pytest.raises(ValidationError):
        ChatRequest(message="")


def test_a_msg_01_accepts_boundary_below_old_limit() -> None:
    """2000 characters (old limit) must still be accepted."""
    req = ChatRequest(message="a" * 2000)
    assert len(req.message) == 2000


def test_a_msg_01_accepts_boundary_above_old_limit() -> None:
    """2001 characters — previously rejected, now accepted under new limit."""
    req = ChatRequest(message="b" * 2001)
    assert len(req.message) == 2001


# ---------------------------------------------------------------------------
# A-RESP-01: ChatResponse.plan field
# ---------------------------------------------------------------------------


def test_a_resp_01_plan_field_exists() -> None:
    """ChatResponse must have a plan attribute defaulting to None."""
    resp = ChatResponse(ok=True)
    assert hasattr(resp, "plan")
    assert resp.plan == []  # always-present list, never null (spec A-RESP-01)


def test_a_resp_01_plan_serializes_as_list() -> None:
    """plan=[dict] must round-trip through model correctly."""
    payload = [{"step_id": "s1", "description": "test"}]
    resp = ChatResponse(ok=True, plan=payload)
    dumped = resp.model_dump(mode="json")
    assert isinstance(dumped["plan"], list)
    assert len(dumped["plan"]) == 1
    assert dumped["plan"][0]["step_id"] == "s1"


def test_a_resp_01_plan_none_is_valid() -> None:
    """Omitting plan kwarg must produce plan=[] (always-present, never null — spec A-RESP-01)."""
    resp = ChatResponse(ok=True)
    assert resp.plan == []


def test_a_resp_01_backward_compat() -> None:
    """ChatResponse(ok=True) without plan kwarg must be valid; plan defaults to empty list."""
    resp = ChatResponse(ok=True)
    assert resp.ok is True
    assert resp.plan == []
    # existing fields still work
    assert resp.retry_count == 0
    assert resp.errors == []
