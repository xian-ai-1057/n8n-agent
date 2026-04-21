"""Smoke test: every Pydantic model imports and one valid instance constructs.

Implements the D0-2 Acceptance Criterion: "every class in D0-2 is importable
and constructible with minimal valid inputs".
"""

from __future__ import annotations

from app.models import (
    AgentState,
    BuiltNode,
    ChatRequest,
    ChatResponse,
    Connection,
    ConnectionType,
    NodeCandidate,
    NodeCatalogEntry,
    NodeDefinition,
    NodeParameter,
    StepIntent,
    StepPlan,
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
    WorkflowDraft,
)


def test_enums_have_expected_values() -> None:
    assert StepIntent.TRIGGER.value == "trigger"
    assert ConnectionType.MAIN.value == "main"
    assert ValidationSeverity.ERROR.value == "error"


def test_catalog_entry_minimal() -> None:
    e = NodeCatalogEntry(
        type="n8n-nodes-base.slack",
        display_name="Slack",
        category="Communication",
        description="Send messages in Slack.",
    )
    assert e.default_type_version is None


def test_node_parameter_and_definition() -> None:
    p = NodeParameter(name="url", type="string", required=True)
    d = NodeDefinition(
        type="n8n-nodes-base.httpRequest",
        display_name="HTTP Request",
        description="Make HTTP requests.",
        category="Core Nodes",
        type_version=4.2,
        parameters=[p],
    )
    assert d.outputs == ["main"]


def test_step_plan_and_candidate() -> None:
    sp = StepPlan(
        step_id="step_1",
        description="Trigger manually.",
        intent=StepIntent.TRIGGER,
        candidate_node_types=["n8n-nodes-base.manualTrigger"],
        reason="User asked for a manual start.",
    )
    nd = NodeDefinition(
        type="n8n-nodes-base.manualTrigger",
        display_name="Manual Trigger",
        description="Manual trigger.",
        category="Trigger",
        type_version=1,
    )
    nc = NodeCandidate(step_id=sp.step_id, chosen_type=nd.type, definition=nd)
    assert nc.definition is not None


def test_built_node_and_connection_and_draft() -> None:
    n1 = BuiltNode(
        name="Trigger",
        type="n8n-nodes-base.manualTrigger",
        type_version=1,
        position=[240.0, 300.0],
    )
    n2 = BuiltNode(
        name="Set",
        type="n8n-nodes-base.set",
        type_version=3.4,
        position=[460.0, 300.0],
    )
    c = Connection(source_name=n1.name, target_name=n2.name)
    d = WorkflowDraft(name="Hello", nodes=[n1, n2], connections=[c])
    assert d.settings == {"executionOrder": "v1"}
    assert c.type == ConnectionType.MAIN


def test_validation() -> None:
    issue = ValidationIssue(
        rule_id="V-NODE-001",
        severity=ValidationSeverity.ERROR,
        message="Missing url",
        node_name="HTTP",
        path="nodes[0].parameters.url",
    )
    rep = ValidationReport.from_issues([issue])
    assert rep.ok is False
    assert len(rep.errors) == 1


def test_agent_state_defaults() -> None:
    s = AgentState(user_message="hi")
    assert s.retry_count == 0
    assert s.plan == []


def test_api_models() -> None:
    req = ChatRequest(message="build me a workflow")
    resp = ChatResponse(ok=True, workflow_url="http://localhost:5678/workflow/abc")
    assert req.message.startswith("build")
    assert resp.retry_count == 0
