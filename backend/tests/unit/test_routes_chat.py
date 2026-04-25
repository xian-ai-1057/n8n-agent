"""Unit tests for the rewritten POST /chat handler (C1-9:CHAT-API-01).

Mocks the dispatcher so we exercise only the FastAPI plumbing: request schema,
response shape, timeout handling, REDACT_TRACE pass-through, session_id
resolution.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.chat.dispatcher import ChatTurnResult
from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_turn(**kw):
    base = {
        "session_id": "abcdef12_xy",
        "assistant_text": "Hi!",
        "tool_calls": [],
        "status": "chat",
    }
    base.update(kw)
    return ChatTurnResult(**base)


# ---------------------------------------------------------------------------
# Request body validation
# ---------------------------------------------------------------------------


def test_chat_missing_message_returns_422():
    with TestClient(app) as client:
        r = client.post("/chat", json={})
    assert r.status_code == 422


def test_chat_invalid_session_id_pattern_returns_422():
    """Pydantic catches the pattern at request-validation time → 422."""
    with TestClient(app) as client:
        r = client.post(
            "/chat", json={"message": "hi", "session_id": "../etc/passwd"}
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Happy paths through the dispatcher
# ---------------------------------------------------------------------------


def test_chat_response_pure_chat_status():
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(assistant_text="hello back"),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "chat"
    assert body["assistant_text"] == "hello back"
    assert body["session_id"] == "abcdef12_xy"
    assert body["tool_calls"] == []
    assert body["workflow_url"] is None
    assert body["ok"] is True


def test_chat_response_includes_session_id_when_client_provides_one():
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(session_id="user_test_001"),
    ) as mock:
        with TestClient(app) as client:
            r = client.post(
                "/chat", json={"message": "hi", "session_id": "user_test_001"}
            )
    assert r.status_code == 200
    assert r.json()["session_id"] == "user_test_001"
    # The dispatcher receives the same session_id we sent.
    args, kwargs = mock.call_args
    assert args[0] == "user_test_001"
    assert args[1] == "hi"


def test_chat_response_awaiting_status_carries_plan():
    plan = [{"step_id": "s1", "description": "trigger"}]
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(
            status="awaiting_plan_approval",
            assistant_text="here is the plan",
            tool_calls=[
                {"name": "build_workflow", "status": "awaiting_plan_approval", "latency_ms": 12}
            ],
            plan=plan,
        ),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "build me a wf"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "awaiting_plan_approval"
    assert body["plan"] == plan
    assert body["tool_calls"][0]["name"] == "build_workflow"
    assert body["ok"] is True


def test_chat_response_deployed_status_includes_workflow_url():
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(
            status="deployed",
            assistant_text="deployed!",
            workflow_url="https://n8n/w/1",
            workflow_id="w1",
        ),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "go"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "deployed"
    assert body["workflow_url"] == "https://n8n/w/1"
    assert body["workflow_id"] == "w1"


def test_chat_response_rejected_status_is_200():
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(status="rejected", assistant_text="Plan rejected."),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "no"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "rejected"
    assert body["ok"] is True


def test_chat_response_error_status_is_200():
    # C1-9:CHAT-API-01 — dispatcher status="error" still returns HTTP 200;
    # the semantic error is expressed via status + ok=False in the body.
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(
            status="error",
            assistant_text="Sorry, something went wrong.",
            error_message="building_timeout",
        ),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "oops"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "error"
    assert body["ok"] is False
    assert body["error_message"] == "building_timeout"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_chat_timeout_returns_504():
    async def _slow(*args, **kwargs):  # noqa: ARG001
        await asyncio.sleep(10)

    with patch(
        "app.api.routes.asyncio.wait_for",
        side_effect=TimeoutError("bound exceeded"),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 504
    body = r.json()
    assert "timeout" in body["error_message"]


# ---------------------------------------------------------------------------
# REDACT_TRACE
# ---------------------------------------------------------------------------


def test_chat_redact_trace_clears_tool_calls(monkeypatch):
    monkeypatch.setenv("REDACT_TRACE", "1")
    with patch(
        "app.api.routes.dispatch_chat_turn",
        return_value=_ok_turn(
            status="awaiting_plan_approval",
            tool_calls=[{"name": "build_workflow", "args_summary": "keys=[user_request]"}],
        ),
    ):
        with TestClient(app) as client:
            r = client.post("/chat", json={"message": "build"})
    assert r.status_code == 200
    assert r.json()["tool_calls"] == []
