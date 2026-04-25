"""Unit tests for completeness_check node (C1-1 §2.4a B-COMP-01..04, Errors §C).

Traceability: C1-1:B-COMP-05 — all 6 unit scenarios (#1–#6) implemented here.
Scenarios #7 (graph wiring) and #8 (assembler drops step_id) live in
test_graph_wiring.py and test_assembler.py respectively.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.agent.completeness import (
    _build_skeleton,
    _make_completeness_check_node,
    completeness_check_step,
)
from app.models.agent_state import AgentState
from app.models.catalog import NodeDefinition
from app.models.enums import StepIntent
from app.models.planning import NodeCandidate, StepPlan
from app.models.workflow import BuiltNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(step_id: str, chosen_type: str = "n8n-nodes-base.set") -> StepPlan:
    return StepPlan(
        step_id=step_id,
        description=f"Step {step_id}",
        intent=StepIntent.TRANSFORM,
        candidate_node_types=[chosen_type],
        reason="test",
    )


def _candidate(
    step_id: str,
    chosen_type: str = "n8n-nodes-base.set",
    type_version: float | None = None,
) -> NodeCandidate:
    defn = None
    if type_version is not None:
        defn = NodeDefinition(
            type=chosen_type,
            display_name=chosen_type,
            description="",
            category="Core",
            type_version=type_version,
            parameters=[],
        )
    return NodeCandidate(step_id=step_id, chosen_type=chosen_type, definition=defn)


def _built_node(step_id: str | None, type_: str = "n8n-nodes-base.set") -> BuiltNode:
    return BuiltNode(
        id=str(uuid4()),
        name=f"Node_{step_id}",
        type=type_,
        typeVersion=1.0,
        position=[0.0, 0.0],
        step_id=step_id,
    )


class _MockRetriever:
    """Minimal retriever mock; get_definitions_by_types returns controlled dict."""

    def __init__(
        self,
        defs: dict[str, NodeDefinition | None] | None = None,
        raise_exc: Exception | None = None,
    ):
        self._defs = defs or {}
        self._raise_exc = raise_exc
        self.calls: list[list[str]] = []

    def search_discovery(self, query: str, k: int = 8) -> list:
        return []

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        return self._defs.get(node_type)

    def search_detailed(self, query: str, k: int = 4) -> list:
        return []

    def get_definitions_by_types(
        self, types: list[str]
    ) -> dict[str, NodeDefinition | None]:
        self.calls.append(list(types))
        if self._raise_exc is not None:
            raise self._raise_exc
        return {t: self._defs.get(t) for t in types}


def _state(**kwargs: Any) -> AgentState:
    defaults: dict[str, Any] = {
        "user_message": "test",
        "plan": [],
        "candidates": [],
        "built_nodes": [],
        "messages": [],
    }
    defaults.update(kwargs)
    return AgentState(**defaults)


# ---------------------------------------------------------------------------
# B-COMP-01: fast path
# ---------------------------------------------------------------------------


def test_b_comp_01_noop_when_all_covered():
    """B-COMP-01 / B-COMP-05 #1: all plan steps covered → return {} no-op."""
    plan = [_step("step_1"), _step("step_2")]
    nodes = [_built_node("step_1"), _built_node("step_2")]
    retriever = _MockRetriever()
    state = _state(plan=plan, built_nodes=nodes)

    result = completeness_check_step(state, retriever)

    assert result == {}
    assert retriever.calls == []  # RAG not touched


def test_b_comp_01_noop_on_empty_plan():
    """B-COMP-01: empty plan → return {}."""
    state = _state(plan=[])
    result = completeness_check_step(state, _MockRetriever())
    assert result == {}


# ---------------------------------------------------------------------------
# B-COMP-02: step_id=None covers nothing
# ---------------------------------------------------------------------------


def test_b_comp_02_built_node_without_step_id_covers_nothing():
    """B-COMP-02 / B-COMP-05 #6: BuiltNode.step_id is None → covers no step."""
    plan = [_step("step_1")]
    # node has no step_id — should not be treated as covering step_1
    nodes = [_built_node(None)]
    candidates = [_candidate("step_1", type_version=2.0)]
    retriever = _MockRetriever(
        {
            "n8n-nodes-base.set": NodeDefinition(
                type="n8n-nodes-base.set",
                display_name="Set",
                description="",
                category="Core",
                type_version=2.0,
                parameters=[],
            )
        }
    )
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    result = completeness_check_step(state, retriever)

    assert "built_nodes" in result
    assert len(result["built_nodes"]) == 2  # original node + injected skeleton


# ---------------------------------------------------------------------------
# B-COMP-03: skeleton injection
# ---------------------------------------------------------------------------


def test_b_comp_03_single_missing_step_injects_skeleton():
    """B-COMP-03 / B-COMP-05 #2: one missing step → skeleton injected with correct fields."""
    plan = [_step("step_1"), _step("step_2"), _step("step_3")]
    nodes = [_built_node("step_1"), _built_node("step_3")]
    rag_def = NodeDefinition(
        type="n8n-nodes-base.set",
        display_name="Set",
        description="",
        category="Core",
        type_version=3.4,
        parameters=[],
    )
    candidates = [
        _candidate("step_1"),
        _candidate("step_2", type_version=None),  # no definition on candidate
        _candidate("step_3"),
    ]
    retriever = _MockRetriever({"n8n-nodes-base.set": rag_def})
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    result = completeness_check_step(state, retriever)

    assert "built_nodes" in result
    new_nodes: list[BuiltNode] = result["built_nodes"]
    assert len(new_nodes) == 3

    # find the injected skeleton
    skeleton = next(n for n in new_nodes if n.step_id == "step_2")
    assert skeleton.type == "n8n-nodes-base.set"
    assert skeleton.type_version == 3.4  # from RAG
    assert skeleton.parameters == {
        "_completeness_injected": "TODO: fill required parameters for this node"
    }
    assert skeleton.position == [0.0, 0.0]
    assert skeleton.name == "Missing step step_2"

    # diagnostic message
    messages = result["messages"]
    comp_msgs = [m for m in messages if m["role"] == "completeness"]
    assert any(
        "injected skeleton for missing step step_2" in m["content"] for m in comp_msgs
    )


def test_b_comp_03_multiple_missing_steps():
    """B-COMP-03 / B-COMP-05 #3: multiple missing steps → all injected, messages count matches."""
    plan = [_step(f"step_{i}") for i in range(1, 5)]
    nodes = [_built_node("step_1")]
    candidates = [_candidate(f"step_{i}") for i in range(1, 5)]
    retriever = _MockRetriever(
        {
            "n8n-nodes-base.set": NodeDefinition(
                type="n8n-nodes-base.set",
                display_name="Set",
                description="",
                category="Core",
                type_version=1.0,
                parameters=[],
            )
        }
    )
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    result = completeness_check_step(state, retriever)

    assert len(result["built_nodes"]) == 4  # 1 original + 3 skeletons
    comp_msgs = [m for m in result["messages"] if m["role"] == "completeness"]
    # 3 injected messages (no "no RAG detail" because RAG returns valid def)
    inject_msgs = [m for m in comp_msgs if "injected skeleton" in m["content"]]
    assert len(inject_msgs) == 3


def test_b_comp_03_no_candidate_skips_step():
    """B-COMP-03 / B-COMP-05 #5: missing step with no candidate → skip, not injected."""
    plan = [_step("step_1"), _step("step_2")]
    nodes = [_built_node("step_1")]
    # NO candidate for step_2
    candidates = [_candidate("step_1")]
    retriever = _MockRetriever()
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    result = completeness_check_step(state, retriever)

    # built_nodes should NOT have a new skeleton for step_2; original list unchanged
    result_nodes = result.get("built_nodes", nodes)
    assert len(result_nodes) == len(nodes)
    messages = result.get("messages", [])
    skip_msgs = [m for m in messages if "skip missing step step_2" in m.get("content", "")]
    assert len(skip_msgs) == 1


# ---------------------------------------------------------------------------
# B-COMP-04: RAG miss → type_version=1.0
# ---------------------------------------------------------------------------


def test_b_comp_04_rag_miss_uses_type_version_10():
    """B-COMP-04 / B-COMP-05 #4: RAG returns None → type_version=1.0, diagnostic emitted."""
    plan = [_step("step_2", "x.unknown")]
    nodes = []
    candidates = [_candidate("step_2", "x.unknown")]
    retriever = _MockRetriever({"x.unknown": None})
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    result = completeness_check_step(state, retriever)

    assert "built_nodes" in result
    skeletons = result["built_nodes"]
    assert len(skeletons) == 1
    assert skeletons[0].type_version == 1.0

    messages = result["messages"]
    rag_miss_msgs = [m for m in messages if "no RAG detail" in m.get("content", "")]
    assert len(rag_miss_msgs) == 1
    assert "typeVersion=1.0" in rag_miss_msgs[0]["content"]


def test_b_comp_04_rag_raises_treated_as_all_none():
    """B-COMP-04 Errors §C: retriever raises → treat all as None, continue gracefully."""
    plan = [_step("step_1", "some.type")]
    nodes = []
    candidates = [_candidate("step_1", "some.type")]
    retriever = _MockRetriever(raise_exc=RuntimeError("chroma is down"))
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    # Must not raise
    result = completeness_check_step(state, retriever)

    # Skeleton still injected with type_version=1.0
    assert "built_nodes" in result
    assert result["built_nodes"][0].type_version == 1.0


# ---------------------------------------------------------------------------
# Errors §C: factory exception handler
# ---------------------------------------------------------------------------


def test_errors_c_factory_wraps_exception():
    """Errors §C: _make_completeness_check_node catches exceptions and writes state.error."""

    class _BrokenRetriever:
        def search_discovery(self, q, k=8): return []
        def get_detail(self, t): return None
        def search_detailed(self, q, k=4): return []
        def get_definitions_by_types(self, types): raise ValueError("unexpected boom")

    plan = [_step("step_1", "n8n-nodes-base.set")]
    nodes = []
    candidates = [_candidate("step_1", "n8n-nodes-base.set")]
    state = _state(plan=plan, built_nodes=nodes, candidates=candidates)

    # Patch completeness_check_step to raise
    import app.agent.completeness as comp_mod
    original = comp_mod.completeness_check_step

    def _raise(state, retriever):
        raise RuntimeError("pydantic boom")

    comp_mod.completeness_check_step = _raise
    try:
        node_fn = _make_completeness_check_node(_BrokenRetriever())
        result = node_fn(state)
    finally:
        comp_mod.completeness_check_step = original

    assert "error" in result
    assert result["error"].startswith("completeness_failed:")
    err_msgs = [
        m for m in result["messages"] if "completeness_failed:" in m.get("content", "")
    ]
    assert len(err_msgs) == 1


# ---------------------------------------------------------------------------
# _build_skeleton helper
# ---------------------------------------------------------------------------


def test_build_skeleton_returns_none_for_none_candidate():
    """B-COMP-03: _build_skeleton(step, None) → None."""
    step = _step("step_x")
    assert _build_skeleton(step, None) is None


def test_build_skeleton_returns_builtnode_with_expected_fields():
    """B-COMP-03: _build_skeleton returns BuiltNode with correct structural fields."""
    step = _step("step_x", "n8n-nodes-base.httpRequest")
    cand = _candidate("step_x", "n8n-nodes-base.httpRequest")
    skeleton = _build_skeleton(step, cand)

    assert skeleton is not None
    assert skeleton.step_id == "step_x"
    assert skeleton.type == "n8n-nodes-base.httpRequest"
    assert skeleton.name == "Missing step step_x"
    assert skeleton.parameters == {
        "_completeness_injected": "TODO: fill required parameters for this node"
    }
    assert skeleton.position == [0.0, 0.0]
