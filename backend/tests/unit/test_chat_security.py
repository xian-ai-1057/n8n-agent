# C1-9:CHAT-SEC-01 - REDACT_TRACE + session_id pattern validation

"""Tests for CHAT-SEC-01: session_id pattern validation and REDACT_TRACE behaviour.

Covers:
- test_session_id_traversal_rejected: path traversal chars → reject
- test_session_id_too_short_rejected: < 8 chars → reject
- test_session_id_special_chars_rejected: special chars → reject
- test_redact_trace_clears_tool_calls: REDACT_TRACE=1 → response.tool_calls == []
- test_redact_trace_default_off: default → tool_calls preserved

Plus extended validation cases and session store pattern enforcement.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.chat.session_store import SessionStore, _validate_session_id
from app.chat.dispatcher import dispatch_chat_turn
from app.chat import session_store as ss_module
from app.chat.dispatcher import ChatTurnResult
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_store(**kwargs) -> SessionStore:
    return SessionStore(**kwargs)


# ---------------------------------------------------------------------------
# CHAT-SEC-01: _validate_session_id pattern enforcement
# ---------------------------------------------------------------------------


def test_chat_sec_01_session_id_traversal_rejected():
    """Path traversal string must be rejected by _validate_session_id."""
    with pytest.raises(ValueError, match="invalid session_id"):
        _validate_session_id("../../etc/passwd")


def test_chat_sec_01_session_id_too_short_rejected():
    """session_id shorter than 8 chars must raise ValueError."""
    with pytest.raises(ValueError):
        _validate_session_id("abc")


def test_chat_sec_01_session_id_exact_min_length_ok():
    """session_id exactly 8 chars must pass validation."""
    _validate_session_id("abcdefgh")  # should not raise


def test_chat_sec_01_session_id_exact_max_length_ok():
    """session_id exactly 64 chars must pass validation."""
    _validate_session_id("a" * 64)  # should not raise


def test_chat_sec_01_session_id_over_max_length_rejected():
    """session_id longer than 64 chars must be rejected."""
    with pytest.raises(ValueError):
        _validate_session_id("a" * 65)


def test_chat_sec_01_session_id_special_chars_rejected():
    """session_id with chars outside [A-Za-z0-9_-] must raise ValueError."""
    for bad_sid in ("abc!@#$%^&*()", "abc def ghi", "abc/def/ghi", "sid=bad"):
        with pytest.raises(ValueError, match="invalid session_id"):
            _validate_session_id(bad_sid)


def test_chat_sec_01_session_id_valid_cases():
    """Valid session_ids (alphanumeric + _ + -) must pass without raising."""
    valid_cases = [
        "abc12345",            # min length exactly 8, all lowercase
        "ABC12345",            # uppercase
        "a-b_c-d_e-f1234",    # hyphens and underscores
        "a" * 32,              # 32 chars
        "a" * 64,              # max 64 chars
        "Sess_001-XYZ",        # mixed
    ]
    for sid in valid_cases:
        _validate_session_id(sid)  # must not raise


def test_chat_sec_01_store_create_rejects_invalid_id():
    """SessionStore.create(invalid_id) must raise ValueError."""
    store = _fresh_store()
    with pytest.raises(ValueError, match="invalid session_id"):
        store.create("ab")  # too short


def test_chat_sec_01_store_create_rejects_path_traversal():
    """SessionStore.create(path_traversal) must raise ValueError."""
    store = _fresh_store()
    with pytest.raises(ValueError):
        store.create("../../../../etc/passwd_12")


def test_chat_sec_01_dispatcher_rejects_short_session_id():
    """dispatch_chat_turn with short explicit session_id raises ValueError."""
    with pytest.raises(ValueError):
        dispatch_chat_turn("ab", "hello")


def test_chat_sec_01_dispatcher_rejects_traversal_session_id():
    """dispatch_chat_turn with path traversal session_id raises ValueError."""
    with pytest.raises(ValueError):
        dispatch_chat_turn("../../etc/passwd_a", "hello")


# ---------------------------------------------------------------------------
# CHAT-SEC-01: REDACT_TRACE mode — API level
# ---------------------------------------------------------------------------


def _ok_turn(**kw) -> ChatTurnResult:
    base = {
        "session_id": "abcdef12xy",
        "assistant_text": "Hi!",
        "tool_calls": [{"name": "build_workflow", "args_summary": "keys=[user_request]", "status": "awaiting_plan_approval"}],
        "status": "chat",
    }
    base.update(kw)
    return ChatTurnResult(**base)


def test_chat_sec_01_redact_trace_clears_tool_calls(monkeypatch):
    """REDACT_TRACE=1: POST /chat response.tool_calls must be [] even when tool ran."""
    monkeypatch.setenv("REDACT_TRACE", "1")

    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(status="awaiting_plan_approval"),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "build me a workflow"})

    assert r.status_code == 200
    body = r.json()
    assert body["tool_calls"] == [], "REDACT_TRACE=1 must clear tool_calls"


def test_chat_sec_01_redact_trace_default_off_preserves_tool_calls(monkeypatch):
    """REDACT_TRACE not set (default=0): tool_calls must be preserved in response."""
    monkeypatch.delenv("REDACT_TRACE", raising=False)

    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(status="awaiting_plan_approval"),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "build me a workflow"})

    assert r.status_code == 200
    body = r.json()
    # REDACT_TRACE off → tool_calls are included
    assert len(body["tool_calls"]) >= 1


def test_chat_sec_01_redact_trace_explicit_zero_preserves_tool_calls(monkeypatch):
    """REDACT_TRACE=0 explicitly keeps tool_calls in the response."""
    monkeypatch.setenv("REDACT_TRACE", "0")

    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(status="awaiting_plan_approval"),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "build me a workflow"})

    body = r.json()
    # tool_calls should be present (not redacted)
    assert len(body["tool_calls"]) >= 1


# ---------------------------------------------------------------------------
# CHAT-SEC-01: session_id pattern in API endpoint (test via /chat POST)
# ---------------------------------------------------------------------------


def test_chat_sec_01_api_rejects_invalid_session_id_pattern():
    """POST /chat with session_id not matching pattern → 422 from Pydantic."""
    with TestClient(app) as client:
        r = client.post(
            "/chat",
            json={"message": "hi", "session_id": "../../etc/passwd"},
        )
    assert r.status_code == 422


def test_chat_sec_01_api_rejects_too_short_session_id():
    """POST /chat with < 8 char session_id → 422 from Pydantic."""
    with TestClient(app) as client:
        r = client.post(
            "/chat",
            json={"message": "hi", "session_id": "abc"},
        )
    assert r.status_code == 422
