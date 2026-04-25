# C1-1:HITL-SHIP-01 — run_cli regression: existing one-shot CLI mode must be unchanged

"""Regression tests asserting that run_cli() one-shot (non-HITL) behaviour is
preserved after the HITL / chat-layer additions.

Requirement (from C1-1:HITL-SHIP-01):
  run_cli MUST remain non-HITL (hitl_enabled=False) so the existing eval
  harness, smoke tests, and any direct CLI usage continue working without
  session management or interrupts.

Scenarios
---------
  test_run_cli_dry_run_workflow_id_none
    run_cli('...', deploy=False) → state.workflow_id is None and
    state.workflow_url is None (no deploy attempted).

  test_run_cli_dry_run_messages_contain_dry_run
    The messages list must contain at least one entry with 'dry_run' in the
    content, confirming the _dry_run_deploy node ran.

  test_run_cli_does_not_use_hitl_checkpointer
    After run_cli the MemorySaver checkpointer singleton must NOT have been
    created (i.e. hitl_enabled=False is respected throughout).

  test_run_cli_plan_auto_approved
    Non-HITL path must auto-approve the plan (plan_approved=True in final
    state), meaning the graph ran through await_plan_approval without an
    interrupt.

  test_run_cli_returns_agent_state
    Return type of run_cli() is always AgentState (not a dict or any other
    type).

  test_run_cli_with_mock_retriever_and_llm
    Full path: mock retriever + mock LLM → plan→build→assemble→validate runs
    and produces a non-None draft.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from app.agent import builder as builder_mod
from app.agent import planner as planner_mod
from app.agent.builder import BuilderOutput
from app.agent.graph import reset_hitl_checkpointer, run_cli
from app.agent.planner import PlannerOutput
from app.models.agent_state import AgentState
from app.models.catalog import NodeCatalogEntry, NodeDefinition
from app.models.enums import StepIntent
from app.models.planning import StepPlan
from app.models.workflow import BuiltNode, Connection


# ---------------------------------------------------------------------------
# Fake retriever (same pattern as test_graph_wiring.py)
# ---------------------------------------------------------------------------


class _FakeRetriever:
    def search_discovery(self, query: str, k: int = 8) -> list[NodeCatalogEntry]:
        return [
            NodeCatalogEntry(
                type="n8n-nodes-base.scheduleTrigger",
                display_name="Schedule Trigger",
                category="Core",
                description="Schedule",
            ),
            NodeCatalogEntry(
                type="n8n-nodes-base.set",
                display_name="Set",
                category="Core",
                description="Set fields",
            ),
        ]

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        if node_type == "n8n-nodes-base.scheduleTrigger":
            return NodeDefinition(
                type=node_type,
                display_name="Schedule Trigger",
                description="",
                category="Core",
                type_version=1.0,
                parameters=[],
            )
        if node_type == "n8n-nodes-base.set":
            return NodeDefinition(
                type=node_type,
                display_name="Set",
                description="",
                category="Core",
                type_version=3.4,
                parameters=[],
            )
        return None

    def get_definitions_by_types(self, types: list[str]) -> dict:
        return {t: self.get_detail(t) for t in types}

    def search_detailed(self, query: str, k: int = 4) -> list[NodeDefinition]:
        return []


# ---------------------------------------------------------------------------
# Fake LLM callables
# ---------------------------------------------------------------------------


class _FakePlannerLLM:
    def __init__(self, output: PlannerOutput):
        self._out = output

    def invoke(self, prompt: str) -> PlannerOutput:
        return self._out


class _FakeBuilderLLM:
    def __init__(self, outputs: list[BuilderOutput]):
        self._outputs = list(outputs)

    def invoke(self, prompt: str) -> BuilderOutput:
        if self._outputs:
            return self._outputs.pop(0)
        raise RuntimeError("FakeBuilderLLM exhausted")


# ---------------------------------------------------------------------------
# Canned plan / build outputs
# ---------------------------------------------------------------------------


def _valid_plan() -> PlannerOutput:
    return PlannerOutput(
        steps=[
            StepPlan(
                step_id="step_1",
                description="Schedule trigger",
                intent=StepIntent.TRIGGER,
                candidate_node_types=["n8n-nodes-base.scheduleTrigger"],
                reason="starts the flow",
            ),
            StepPlan(
                step_id="step_2",
                description="Set output field",
                intent=StepIntent.TRANSFORM,
                candidate_node_types=["n8n-nodes-base.set"],
                reason="set field",
            ),
        ]
    )


def _valid_builder_output() -> BuilderOutput:
    t_id = str(uuid4())
    s_id = str(uuid4())
    return BuilderOutput(
        nodes=[
            BuiltNode(
                id=t_id,
                name="Schedule Trigger",
                type="n8n-nodes-base.scheduleTrigger",
                type_version=1.0,
                position=[0, 0],
                parameters={},
                step_id="step_1",
            ),
            BuiltNode(
                id=s_id,
                name="Set",
                type="n8n-nodes-base.set",
                type_version=3.4,
                position=[0, 0],
                parameters={},
                step_id="step_2",
            ),
        ],
        connections=[Connection(source_name="Schedule Trigger", target_name="Set")],
    )


# ---------------------------------------------------------------------------
# Shared fixture: wire fake LLMs into planner / builder
# ---------------------------------------------------------------------------


@pytest.fixture
def _wire_llms(monkeypatch):
    """Install _FakePlannerLLM + _FakeBuilderLLM."""

    def _install(planner_out: PlannerOutput, builder_outs: list[BuilderOutput]):
        monkeypatch.setattr(
            planner_mod, "get_llm", lambda schema, **kw: _FakePlannerLLM(planner_out)
        )
        monkeypatch.setattr(
            builder_mod, "get_llm", lambda schema, **kw: _FakeBuilderLLM(builder_outs)
        )

    return _install


@pytest.fixture(autouse=True)
def _reset_checkpointer():
    """Ensure HITL MemorySaver is clean before each test."""
    reset_hitl_checkpointer()
    yield
    reset_hitl_checkpointer()


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


def test_run_cli_dry_run_workflow_id_none(_wire_llms):
    """run_cli(deploy=False) → workflow_id and workflow_url are None (no deploy)."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    state = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    assert isinstance(state, AgentState)
    assert state.workflow_id is None, "dry_run must not populate workflow_id"
    assert state.workflow_url is None, "dry_run must not populate workflow_url"


def test_run_cli_dry_run_messages_contain_dry_run(_wire_llms):
    """run_cli(deploy=False) final messages must contain 'dry_run' content."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    state = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    all_content = " ".join(m.get("content", "") for m in state.messages)
    assert "dry_run" in all_content, (
        "Expected 'dry_run' in messages when deploy=False, "
        f"but messages were: {state.messages}"
    )


def test_run_cli_does_not_use_hitl_checkpointer(_wire_llms):
    """run_cli must NOT create the HITL MemorySaver singleton (hitl_enabled=False)."""
    import app.agent.graph as graph_mod

    _wire_llms(_valid_plan(), [_valid_builder_output()])

    # Ensure singleton starts as None
    graph_mod._HITL_CHECKPOINTER = None

    run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    # Non-HITL path must not instantiate MemorySaver
    assert graph_mod._HITL_CHECKPOINTER is None, (
        "run_cli must not create _HITL_CHECKPOINTER (hitl_enabled=False)"
    )


def test_run_cli_plan_auto_approved(_wire_llms):
    """Non-HITL path must auto-approve the plan without interrupt."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    state = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    # plan_approved must be True: await_plan_approval auto-approved (no HITL gate)
    assert state.plan_approved is True, (
        "run_cli must auto-approve the plan (hitl_enabled=False, no interrupt)"
    )


def test_run_cli_returns_agent_state(_wire_llms):
    """run_cli() return type must always be AgentState, never a raw dict."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    result = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    assert isinstance(result, AgentState), (
        f"run_cli must return AgentState, got {type(result)}"
    )


def test_run_cli_with_mock_retriever_and_llm_produces_draft(_wire_llms):
    """Full path mock: plan→build→assemble→validate produces a non-None draft."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    state = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    assert state.draft is not None, "run_cli must produce a workflow draft"
    assert state.error is None, f"run_cli must not error; got: {state.error}"
    node_names = {n.name for n in state.draft.nodes}
    assert "Schedule Trigger" in node_names
    assert "Set" in node_names


def test_run_cli_plan_populated(_wire_llms):
    """run_cli must populate state.plan with the planner's steps."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    state = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    assert len(state.plan) == 2
    step_ids = {s.step_id for s in state.plan}
    assert "step_1" in step_ids
    assert "step_2" in step_ids


def test_run_cli_session_id_remains_none(_wire_llms):
    """run_cli must not assign a session_id (that belongs to HITL mode only)."""
    _wire_llms(_valid_plan(), [_valid_builder_output()])

    state = run_cli(
        "手動觸發並設欄位",
        deploy=False,
        retriever=_FakeRetriever(),
    )

    assert state.session_id is None, (
        "run_cli must not set session_id; HITL session management is not active"
    )
