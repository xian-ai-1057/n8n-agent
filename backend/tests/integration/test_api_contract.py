"""FastAPI contract tests (Implements C1-5 acceptance criteria).

The external services are fully mocked — no network / no Chroma / no
OpenAI-compat endpoint / no n8n are touched.

- `run_cli` is monkeypatched to return a canned `AgentState`.
- The health probes are monkeypatched to avoid hitting real services.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import routes as routes_mod
from app.main import create_app
from app.models.agent_state import AgentState
from app.models.enums import ValidationSeverity
from app.models.validation import ValidationIssue, ValidationReport
from app.models.workflow import BuiltNode, Connection, WorkflowDraft


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Make sure health checks do not touch external services by default.
    async def _fake_openai(_settings: Any) -> dict[str, Any]:
        return {"ok": True, "latency_ms": 5}

    async def _fake_n8n(_settings: Any) -> dict[str, Any]:
        return {"ok": True, "latency_ms": 7}

    async def _fake_chroma(_settings: Any) -> dict[str, Any]:
        return {"ok": True, "latency_ms": 4, "detail": "discovery=417,detailed=30"}

    monkeypatch.setattr(routes_mod, "_check_openai", _fake_openai)
    monkeypatch.setattr(routes_mod, "_check_n8n", _fake_n8n)
    monkeypatch.setattr(routes_mod, "_check_chroma", _fake_chroma)

    app = create_app()
    return TestClient(app)


def _canned_ok_state(*, deploy: bool) -> AgentState:
    """AgentState as if plan→build→assemble→validate→deploy all succeeded."""
    node = BuiltNode(
        id="11111111-1111-1111-1111-111111111111",
        name="Manual Trigger",
        type="n8n-nodes-base.manualTrigger",
        typeVersion=1,
        position=[0, 0],
        parameters={},
    )
    node2 = BuiltNode(
        id="22222222-2222-2222-2222-222222222222",
        name="Set",
        type="n8n-nodes-base.set",
        typeVersion=1,
        position=[240, 0],
        parameters={"values": {"string": [{"name": "hello", "value": "world"}]}},
    )
    draft = WorkflowDraft(
        name="Canned",
        nodes=[node, node2],
        connections=[
            Connection(source_name="Manual Trigger", target_name="Set"),
        ],
    )
    return AgentState(
        user_message="test",
        draft=draft,
        validation=ValidationReport(ok=True),
        workflow_id="wf-123" if deploy else None,
        workflow_url="http://localhost:5678/workflow/wf-123" if deploy else None,
    )


# ----------------------------------------------------------------------
# /
# ----------------------------------------------------------------------


def test_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "n8n-workflow-builder"


# ----------------------------------------------------------------------
# /health
# ----------------------------------------------------------------------


def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"ok", "openai", "n8n", "chroma", "checks"}
    assert body["ok"] is True
    assert body["openai"] is True
    assert body["n8n"] is True
    assert body["chroma"] is True


def test_health_partial_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_ok(_settings: Any) -> dict[str, Any]:
        return {"ok": True, "latency_ms": 1}

    async def _fake_down(_settings: Any) -> dict[str, Any]:
        return {"ok": False, "detail": "no api key"}

    monkeypatch.setattr(routes_mod, "_check_openai", _fake_ok)
    monkeypatch.setattr(routes_mod, "_check_n8n", _fake_down)
    monkeypatch.setattr(routes_mod, "_check_chroma", _fake_ok)

    app = create_app()
    c = TestClient(app)
    body = c.get("/health").json()
    assert body["ok"] is False
    assert body["n8n"] is False
    assert body["openai"] is True


# ----------------------------------------------------------------------
# /chat success
# ----------------------------------------------------------------------


def test_chat_success_deploy(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    # Force "N8N_API_KEY" set so deploy path is taken.
    from app import config as config_mod

    config_mod.get_settings.cache_clear()
    monkeypatch.setattr(
        config_mod.Settings.model_fields["n8n_api_key"],
        "default",
        "testing-key",
        raising=False,
    )

    def _fake_run_cli(user_input: str, *, deploy: bool = False, retriever: Any = None) -> AgentState:
        return _canned_ok_state(deploy=deploy)

    monkeypatch.setattr(routes_mod, "run_cli", _fake_run_cli)

    r = client.post("/chat", json={"message": "hello"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["workflow_json"] is not None
    assert "nodes" in body["workflow_json"]
    assert len(body["workflow_json"]["nodes"]) == 2
    # connections become a map on the wire.
    assert isinstance(body["workflow_json"]["connections"], dict)
    config_mod.get_settings.cache_clear()


def test_chat_success_no_deploy(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    # No N8N_API_KEY → workflow_url should be None but workflow_json present.
    from app import config as config_mod

    config_mod.get_settings.cache_clear()
    monkeypatch.setenv("N8N_API_KEY", "")

    def _fake_run_cli(user_input: str, *, deploy: bool = False, retriever: Any = None) -> AgentState:
        # Even though deploy=False, return state with validation ok and no url.
        return _canned_ok_state(deploy=False)

    monkeypatch.setattr(routes_mod, "run_cli", _fake_run_cli)

    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_json"] is not None
    assert body["workflow_url"] is None
    config_mod.get_settings.cache_clear()


# ----------------------------------------------------------------------
# /chat validation failure (422 + errors)
# ----------------------------------------------------------------------


def test_chat_validator_failure(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    draft = WorkflowDraft(
        name="Broken",
        nodes=[
            BuiltNode(
                id="11111111-1111-1111-1111-111111111111",
                name="HTTP",
                type="n8n-nodes-base.httpRequest",
                typeVersion=4,
                position=[0, 0],
                parameters={},
            )
        ],
        connections=[],
    )
    state = AgentState(
        user_message="x",
        draft=draft,
        validation=ValidationReport(
            ok=False,
            errors=[
                ValidationIssue(
                    rule_id="V-NODE-003",
                    severity=ValidationSeverity.ERROR,
                    message="url is required",
                    node_name="HTTP",
                    path="nodes[0].parameters.url",
                ),
            ],
        ),
        retry_count=2,
        error="validator failed after 2 retries; 1 errors",
    )

    def _fake_run_cli(user_input: str, *, deploy: bool = False, retriever: Any = None) -> AgentState:
        return state

    monkeypatch.setattr(routes_mod, "run_cli", _fake_run_cli)

    r = client.post("/chat", json={"message": "bad"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["retry_count"] == 2
    assert body["errors"] and body["errors"][0]["rule_id"] == "V-NODE-003"
    assert body["workflow_json"] is not None  # last attempt still surfaced


# ----------------------------------------------------------------------
# /chat raises → 500
# ----------------------------------------------------------------------


def test_chat_internal_error(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    def _boom(*args: Any, **kwargs: Any) -> AgentState:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(routes_mod, "run_cli", _boom)

    r = client.post("/chat", json={"message": "oops"})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["error_message"] == "internal error"


# ----------------------------------------------------------------------
# /chat timeout → 504
# ----------------------------------------------------------------------


def test_chat_timeout(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    # Slash the timeout to keep the test fast. `get_settings` is @lru_cache'd,
    # so the /chat handler reads from the same instance we patch here.
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "chat_request_timeout_sec", 0.5
    )

    def _slow(*args: Any, **kwargs: Any) -> AgentState:
        time.sleep(3)
        return _canned_ok_state(deploy=False)

    monkeypatch.setattr(routes_mod, "run_cli", _slow)

    r = client.post("/chat", json={"message": "will time out"})
    assert r.status_code == 504
    body = r.json()
    assert body["ok"] is False
    assert "timeout" in body["error_message"].lower()


# ----------------------------------------------------------------------
# /chat schema enforcement → 422 from FastAPI itself
# ----------------------------------------------------------------------


def test_chat_schema_enforced(client: TestClient) -> None:
    r = client.post("/chat", json={})
    assert r.status_code == 422  # missing message

    r = client.post("/chat", json={"message": ""})
    assert r.status_code == 422  # min_length=1
