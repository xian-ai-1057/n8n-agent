"""LangGraph wiring tests (Phase 2-B).

Strategy: monkeypatch the LLM-bound structured-output callables in
`app.agent.planner` and `app.agent.builder` so no network is needed.
We assert three paths:

1. Happy path — validator passes first try, deployer short-circuits.
2. One-retry path — validator fails once, builder retry (via fix prompt) fixes it.
3. Max-retry path — validator never passes; graph ends in give_up.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent import builder as builder_mod
from app.agent import planner as planner_mod
from app.agent.builder import BuilderOutput
from app.agent.graph import build_graph
from app.agent.planner import PlannerOutput
from app.models.agent_state import AgentState
from app.models.catalog import NodeCatalogEntry, NodeDefinition
from app.models.enums import StepIntent
from app.models.planning import StepPlan
from app.models.workflow import BuiltNode, Connection


# --------------------------------------------------------------------------
# Fake retriever
# --------------------------------------------------------------------------


class _FakeRetriever:
    def search_discovery(self, query: str, k: int = 8) -> list[NodeCatalogEntry]:
        return [
            NodeCatalogEntry(
                type="n8n-nodes-base.scheduleTrigger",
                display_name="Schedule Trigger",
                category="Core",
                description="Start manually",
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

    def search_detailed(self, query: str, k: int = 4) -> list[NodeDefinition]:
        return []


# --------------------------------------------------------------------------
# Fakes for LLM callables
# --------------------------------------------------------------------------


class _FakePlannerLLM:
    def __init__(self, output: PlannerOutput):
        self._out = output

    def invoke(self, prompt: str) -> PlannerOutput:
        return self._out


class _FakeBuilderLLM:
    """Returns queued outputs in order, one per call."""

    def __init__(self, outputs: list[BuilderOutput]):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, prompt: str) -> BuilderOutput:
        self.calls += 1
        if not self._outputs:
            raise RuntimeError("FakeBuilderLLM exhausted")
        return self._outputs.pop(0)


# --------------------------------------------------------------------------
# Canned outputs
# --------------------------------------------------------------------------


def _valid_plan() -> PlannerOutput:
    return PlannerOutput(
        steps=[
            StepPlan(
                step_id="step_1",
                description="手動觸發",
                intent=StepIntent.TRIGGER,
                candidate_node_types=["n8n-nodes-base.scheduleTrigger"],
                reason="manual trigger suits.",
            ),
            StepPlan(
                step_id="step_2",
                description="設定欄位",
                intent=StepIntent.TRANSFORM,
                candidate_node_types=["n8n-nodes-base.set"],
                reason="set field.",
            ),
        ]
    )


def _valid_builder_output() -> BuilderOutput:
    manual_id = str(uuid4())
    set_id = str(uuid4())
    return BuilderOutput(
        nodes=[
            BuiltNode(
                id=manual_id,
                name="Schedule Trigger",
                type="n8n-nodes-base.scheduleTrigger",
                type_version=1.0,
                position=[0, 0],
                parameters={},
            ),
            BuiltNode(
                id=set_id,
                name="Set",
                type="n8n-nodes-base.set",
                type_version=3.4,
                position=[0, 0],
                parameters={},
            ),
        ],
        connections=[
            Connection(source_name="Schedule Trigger", target_name="Set")
        ],
    )


def _invalid_builder_output() -> BuilderOutput:
    """Missing trigger → validator V-TRIG-001 fails."""
    set_id = str(uuid4())
    return BuilderOutput(
        nodes=[
            BuiltNode(
                id=set_id,
                name="Set",
                type="n8n-nodes-base.set",
                type_version=3.4,
                position=[0, 0],
                parameters={},
            ),
        ],
        connections=[],
    )


# --------------------------------------------------------------------------
# Patching helper
# --------------------------------------------------------------------------


@pytest.fixture
def patch_llms(monkeypatch):
    """Returns a function you call with (planner_llm, builder_llm) to install."""

    def _install(planner_llm, builder_llm):
        monkeypatch.setattr(
            planner_mod, "get_llm", lambda schema, **kw: planner_llm
        )
        monkeypatch.setattr(
            builder_mod, "get_llm", lambda schema, **kw: builder_llm
        )

    return _install


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_happy_path_validates_and_deploys_dry_run(patch_llms):
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="手動觸發並設欄位"))
    state = AgentState.model_validate(raw)

    assert state.error is None
    assert state.validation is not None
    assert state.validation.ok
    assert state.retry_count == 0
    assert state.draft is not None
    assert {n.name for n in state.draft.nodes} == {"Schedule Trigger", "Set"}
    assert builder_llm.calls == 1


def test_one_retry_path_fixes_and_passes(patch_llms):
    planner_llm = _FakePlannerLLM(_valid_plan())
    # First build is invalid (no trigger); fix build returns valid.
    builder_llm = _FakeBuilderLLM(
        [_invalid_builder_output(), _valid_builder_output()]
    )
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="retry scenario"))
    state = AgentState.model_validate(raw)

    assert state.error is None
    assert state.validation is not None
    assert state.validation.ok
    assert state.retry_count == 1
    assert builder_llm.calls == 2


def test_max_retry_exhausted_returns_give_up(patch_llms):
    planner_llm = _FakePlannerLLM(_valid_plan())
    # Always return invalid — builder called 1 + MAX_RETRIES=2 times.
    builder_llm = _FakeBuilderLLM(
        [_invalid_builder_output() for _ in range(5)]
    )
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="never passes"))
    state = AgentState.model_validate(raw)

    assert state.validation is not None
    assert not state.validation.ok
    assert state.retry_count == 2
    assert state.error is not None
    assert "validator failed" in state.error
    assert builder_llm.calls == 3  # initial + 2 retries
