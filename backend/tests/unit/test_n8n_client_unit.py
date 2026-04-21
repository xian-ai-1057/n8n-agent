"""Unit tests for N8nClient (C1-3).

Uses `httpx.MockTransport` to assert the request the client would send —
headers, URL, stripped read-only fields, default settings, node sanitising.
No network. No fixtures depend on a running n8n.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.models.enums import ConnectionType
from app.models.workflow import BuiltNode, Connection, WorkflowDraft
from app.n8n.client import N8nClient
from app.n8n.errors import (
    N8nAuthError,
    N8nBadRequestError,
    N8nNotFoundError,
    N8nServerError,
    N8nUnavailable,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid4())


def _make_client(handler) -> N8nClient:
    transport = httpx.MockTransport(handler)
    return N8nClient(
        base_url="http://n8n.local",
        api_key="test-key",
        transport=transport,
    )


def _minimal_draft() -> WorkflowDraft:
    return WorkflowDraft(
        name="Unit Test WF",
        nodes=[
            BuiltNode(
                id=_uuid(),
                name="Manual",
                type="n8n-nodes-base.manualTrigger",
                typeVersion=1,
                position=[240, 300],
                parameters={},
            ),
            BuiltNode(
                id=_uuid(),
                name="Set",
                type="n8n-nodes-base.set",
                typeVersion=3.4,
                position=[460, 300],
                parameters={},
            ),
        ],
        connections=[
            Connection(
                source_name="Manual",
                target_name="Set",
                type=ConnectionType.MAIN,
            )
        ],
    )


# ----------------------------------------------------------------------
# Request shape assertions
# ----------------------------------------------------------------------


def test_create_workflow_sends_correct_headers_and_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            201, json={"id": "wf-123", "name": captured["body"]["name"]}
        )

    client = _make_client(handler)
    result = client.create_workflow(_minimal_draft())

    assert captured["method"] == "POST"
    assert captured["url"] == "http://n8n.local/api/v1/workflows"
    assert captured["headers"]["x-n8n-api-key"] == "test-key"
    assert result.id == "wf-123"
    assert result.url == "http://n8n.local/workflow/wf-123"
    assert result.name == "Unit Test WF"


def test_create_workflow_strips_readonly_fields():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "wf-1", "name": captured["body"]["name"]})

    client = _make_client(handler)
    polluted = {
        "name": "Test",
        "id": "should-be-stripped",
        "active": True,
        "createdAt": "2026-01-01",
        "updatedAt": "2026-01-01",
        "versionId": "abc",
        "triggerCount": 0,
        "shared": [],
        "activeVersion": 1,
        "tags": [{"name": "x"}],
        "pinData": {},
        "nodes": [
            {
                "id": _uuid(),
                "name": "Manual",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            }
        ],
        "connections": {},
        "settings": {"executionOrder": "v1"},
    }
    client.create_workflow(polluted)

    body = captured["body"]
    for ro in [
        "id",
        "active",
        "createdAt",
        "updatedAt",
        "versionId",
        "triggerCount",
        "shared",
        "activeVersion",
        "tags",
        "pinData",
    ]:
        assert ro not in body, f"read-only field {ro} should have been stripped"
    assert set(body.keys()) == {"name", "nodes", "connections", "settings"}


def test_create_workflow_defaults_settings_when_missing():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "wf-2", "name": captured["body"]["name"]})

    client = _make_client(handler)
    payload = {
        "name": "NoSettings",
        "nodes": [
            {
                "id": _uuid(),
                "name": "Manual",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            }
        ],
        "connections": {},
    }
    client.create_workflow(payload)

    assert captured["body"]["settings"] == {"executionOrder": "v1"}


def test_create_workflow_migrates_continue_on_fail_to_on_error():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "wf-3", "name": captured["body"]["name"]})

    client = _make_client(handler)
    payload = {
        "name": "MigrationTest",
        "nodes": [
            {
                "id": _uuid(),
                "name": "Manual",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
                "continueOnFail": True,
            }
        ],
        "connections": {},
        "settings": {"executionOrder": "v1"},
    }
    client.create_workflow(payload)

    node = captured["body"]["nodes"][0]
    assert "continueOnFail" not in node
    assert node["onError"] == "continueRegularOutput"


def test_connections_draft_list_converts_to_n8n_map():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "wf-4", "name": captured["body"]["name"]})

    client = _make_client(handler)
    client.create_workflow(_minimal_draft())

    conns = captured["body"]["connections"]
    assert "Manual" in conns
    assert conns["Manual"]["main"] == [
        [{"node": "Set", "type": "main", "index": 0}]
    ]


# ----------------------------------------------------------------------
# Error mapping
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,exc_cls",
    [
        (400, N8nBadRequestError),
        (401, N8nAuthError),
        (403, N8nAuthError),
        (404, N8nNotFoundError),
        (500, N8nServerError),
        (502, N8nServerError),
    ],
)
def test_http_status_mapping(status, exc_cls):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": f"boom {status}"})

    client = _make_client(handler)
    with pytest.raises(exc_cls) as ei:
        client.get_workflow("does-not-matter")

    if status == 400:
        assert "boom" in ei.value.detail


def test_connection_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _make_client(handler)
    with pytest.raises(N8nUnavailable):
        client.get_workflow("x")


# ----------------------------------------------------------------------
# health()
# ----------------------------------------------------------------------


def test_health_returns_true_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = _make_client(handler)
    assert client.health() is True


def test_health_returns_false_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "no auth"})

    client = _make_client(handler)
    assert client.health() is False
