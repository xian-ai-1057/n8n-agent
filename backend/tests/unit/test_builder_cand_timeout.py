"""Tests for builder candidate selection (B-CAND-01/02) and timeout routing
(B-TIMEOUT-01).

# C1-1:B-CAND-01  — batch retriever query (single Chroma round-trip)
# C1-1:B-CAND-02  — per-step fallback / exhaustion logic
# C1-1:B-TIMEOUT-01 — BuilderTimeoutError raised on LLM timeout; graph routes
#                      to give_up
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from app.agent.builder import (
    BuilderTimeoutError,
    _collect_candidates,
    _enforce_candidate_types,
    build_nodes,
)
from app.agent.graph import _after_build
from app.agent.llm import LLMTimeoutError
from app.models.agent_state import AgentState
from app.models.catalog import NodeDefinition
from app.models.enums import StepIntent
from app.models.planning import StepPlan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_def(t: str) -> NodeDefinition:
    return NodeDefinition(
        type=t,
        display_name=t,
        description="",
        category="Core",
        type_version=1.0,
        parameters=[],
    )


def _make_step(step_id: str, candidate_types: list[str]) -> StepPlan:
    return StepPlan(
        step_id=step_id,
        description="test",
        intent=StepIntent.TRANSFORM,
        candidate_node_types=candidate_types,
        reason="test",
    )


@dataclass
class _StepLike:
    """Lightweight stand-in for StepPlan — allows empty candidate_node_types,
    which Pydantic's min_length=1 validator would reject on a real StepPlan."""

    step_id: str
    candidate_node_types: list[str] = field(default_factory=list)


class _FakeRetriever:
    """Retriever stub for _collect_candidates tests."""

    def __init__(self, detail_map: dict[str, NodeDefinition | None]) -> None:
        self._detail_map = detail_map
        self.batch_calls: list[list[str]] = []

    def get_definitions_by_types(self, types: list[str]) -> dict[str, NodeDefinition | None]:
        self.batch_calls.append(list(types))
        return {t: self._detail_map.get(t, None) for t in types}

    # The following are part of RetrieverProtocol but not needed for these tests.
    def search_discovery(self, query: str, k: int = 8):
        return []

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        return self._detail_map.get(node_type)

    def search_detailed(self, query: str, k: int = 4):
        return []


# ---------------------------------------------------------------------------
# C1-1:B-CAND-01 / C1-1:B-CAND-02 — _collect_candidates tests
# ---------------------------------------------------------------------------


def test_b_cand_01_first_candidate_chosen():
    """B-CAND-01/02: first candidate with a detail is chosen; no diagnostic messages."""
    retriever = _FakeRetriever({"typeA": _make_def("typeA"), "typeB": _make_def("typeB")})
    plan = [_make_step("s1", ["typeA", "typeB"])]

    candidates, messages = _collect_candidates(plan, retriever)

    assert len(candidates) == 1
    assert candidates[0].chosen_type == "typeA"
    assert candidates[0].definition is not None
    assert candidates[0].definition.type == "typeA"
    assert messages == []


def test_b_cand_02_fallback_to_second():
    """B-CAND-02: when first candidate has no detail, falls back to second."""
    retriever = _FakeRetriever({"typeA": None, "typeB": _make_def("typeB")})
    plan = [_make_step("s1", ["typeA", "typeB"])]

    candidates, messages = _collect_candidates(plan, retriever)

    assert len(candidates) == 1
    assert candidates[0].chosen_type == "typeB"
    assert candidates[0].definition is not None
    # One diagnostic message with "fallback:" prefix
    assert len(messages) == 1
    assert "fallback:" in messages[0]["content"]


def test_b_cand_02_all_candidates_exhausted():
    """B-CAND-02: when all candidates lack detail, use first type with empty shell."""
    retriever = _FakeRetriever({"typeA": None, "typeB": None})
    plan = [_make_step("s1", ["typeA", "typeB"])]

    candidates, messages = _collect_candidates(plan, retriever)

    assert len(candidates) == 1
    assert candidates[0].chosen_type == "typeA"  # falls back to first
    assert candidates[0].definition is None      # empty shell
    # One diagnostic message with "fallback_exhausted:" prefix
    assert len(messages) == 1
    assert "fallback_exhausted:" in messages[0]["content"]


def test_b_cand_02_empty_candidates_skipped():
    """B-CAND-02: steps with empty candidate_node_types are skipped entirely."""
    # Use _StepLike to bypass StepPlan min_length=1 validation.
    retriever = _FakeRetriever({})
    empty_step = _StepLike(step_id="s1", candidate_node_types=[])

    candidates, messages = _collect_candidates([empty_step], retriever)  # type: ignore[arg-type]

    assert candidates == []
    assert messages == []


def test_b_cand_01_batch_query_called_once():
    """B-CAND-01: two steps with 3 distinct types → get_definitions_by_types called exactly once."""
    retriever = _FakeRetriever({
        "typeA": _make_def("typeA"),
        "typeB": _make_def("typeB"),
        "typeC": _make_def("typeC"),
    })
    plan = [
        _make_step("s1", ["typeA", "typeB"]),
        _make_step("s2", ["typeC"]),
    ]

    candidates, _ = _collect_candidates(plan, retriever)

    # Single batch call, not one per step
    assert len(retriever.batch_calls) == 1
    # All 3 distinct types were queried
    assert set(retriever.batch_calls[0]) == {"typeA", "typeB", "typeC"}
    assert len(candidates) == 2


# ---------------------------------------------------------------------------
# C1-1:B-TIMEOUT-01 — build_nodes raises BuilderTimeoutError on LLM timeout
# ---------------------------------------------------------------------------


def test_b_timeout_01_build_nodes_raises_on_llm_timeout():
    """B-TIMEOUT-01: build_nodes raises BuilderTimeoutError when invoke_with_timeout
    raises LLMTimeoutError."""
    retriever = _FakeRetriever({"typeA": _make_def("typeA")})
    state = AgentState(user_message="test", plan=[_make_step("s1", ["typeA"])])

    with patch("app.agent.builder.get_llm", return_value=MagicMock()):
        with patch(
            "app.agent.builder.invoke_with_timeout",
            side_effect=LLMTimeoutError("stalled"),
        ):
            with pytest.raises(BuilderTimeoutError):
                build_nodes(state, retriever)


def test_b_timeout_01_build_nodes_returns_error_dict_on_generic_exception():
    """B-TIMEOUT-01: build_nodes returns a dict with error=building_failed: when
    invoke_with_timeout raises a generic exception (no exception propagated)."""
    retriever = _FakeRetriever({"typeA": _make_def("typeA")})
    state = AgentState(user_message="test", plan=[_make_step("s1", ["typeA"])])

    with patch("app.agent.builder.get_llm", return_value=MagicMock()):
        with patch(
            "app.agent.builder.invoke_with_timeout",
            side_effect=RuntimeError("boom"),
        ):
            result = build_nodes(state, retriever)

    assert "error" in result
    assert result["error"].startswith("building_failed:")


# ---------------------------------------------------------------------------
# C1-1:B-TIMEOUT-01 — _after_build routing
# ---------------------------------------------------------------------------


def test_b_timeout_01_after_build_routes_give_up_on_building_timeout():
    """B-TIMEOUT-01: building_timeout: prefix → give_up."""
    state = AgentState(user_message="x", error="building_timeout: stage=builder cause=stalled")
    assert _after_build(state) == "give_up"


def test_b_timeout_01_after_build_routes_give_up_on_building_failed():
    """B-TIMEOUT-01: building_failed: prefix → give_up."""
    state = AgentState(user_message="x", error="building_failed: some error")
    assert _after_build(state) == "give_up"


def test_b_timeout_01_after_build_routes_assemble_on_no_error():
    """B-TIMEOUT-01: no error → completeness_check (C1-1:B-COMP-01)."""
    state = AgentState(user_message="x", error=None)
    assert _after_build(state) == "completeness_check"  # C1-1:B-COMP-01


def test_b_timeout_01_after_build_routes_assemble_on_other_error():
    """B-TIMEOUT-01: unrelated error prefix does NOT trigger give_up → completeness_check (C1-1:B-COMP-01)."""
    state = AgentState(user_message="x", error="some_other_error: details")
    assert _after_build(state) == "completeness_check"  # C1-1:B-COMP-01


# ---------------------------------------------------------------------------
# _enforce_candidate_types: C1-1:B-CAND-03
# ---------------------------------------------------------------------------


def _make_built_node(node_type: str, type_version: float = 1.0):
    from app.models.workflow import BuiltNode
    return BuiltNode(name="n", type=node_type, typeVersion=type_version, position=[0, 0])


def test_b_cand_03_corrects_hallucinated_type():
    """_enforce_candidate_types replaces a type not in the candidate list."""
    from app.models.planning import NodeCandidate
    step = _make_step("s1", ["@n8n/n8n-nodes-langchain.chat"])
    cand = NodeCandidate(step_id="s1", chosen_type="@n8n/n8n-nodes-langchain.chat", definition=None)
    node = _make_built_node("n8n-nodes-base.httpTrigger")  # LLM hallucinated this

    corrected = _enforce_candidate_types([node], [step], [cand])

    assert corrected[0].type == "@n8n/n8n-nodes-langchain.chat"


def test_b_cand_03_preserves_correct_type():
    """_enforce_candidate_types does not touch a node whose type is already in the list."""
    from app.models.planning import NodeCandidate
    step = _make_step("s1", ["n8n-nodes-base.manualTrigger"])
    cand = NodeCandidate(step_id="s1", chosen_type="n8n-nodes-base.manualTrigger", definition=None)
    node = _make_built_node("n8n-nodes-base.manualTrigger")

    corrected = _enforce_candidate_types([node], [step], [cand])

    assert corrected[0].type == "n8n-nodes-base.manualTrigger"


def test_b_cand_03_uses_definition_type_version_when_available():
    """When definition is present, corrected node uses definition.type_version."""
    from app.models.planning import NodeCandidate
    defn = _make_def("@n8n/n8n-nodes-langchain.agent")
    defn = defn.model_copy(update={"type_version": 1.7})
    step = _make_step("s1", ["@n8n/n8n-nodes-langchain.agent"])
    cand = NodeCandidate(
        step_id="s1", chosen_type="@n8n/n8n-nodes-langchain.agent", definition=defn
    )
    node = _make_built_node("n8n-nodes-base.httpRequest", type_version=1.0)

    corrected = _enforce_candidate_types([node], [step], [cand])

    assert corrected[0].type == "@n8n/n8n-nodes-langchain.agent"
    assert corrected[0].type_version == 1.7


def test_b_cand_03_extra_nodes_pass_through():
    """Nodes beyond plan length are passed through unchanged."""
    from app.models.planning import NodeCandidate
    step = _make_step("s1", ["n8n-nodes-base.set"])
    cand = NodeCandidate(step_id="s1", chosen_type="n8n-nodes-base.set", definition=None)
    node1 = _make_built_node("n8n-nodes-base.set")
    node2 = _make_built_node("n8n-nodes-base.noOp")  # beyond plan

    corrected = _enforce_candidate_types([node1, node2], [step], [cand])

    assert corrected[1].type == "n8n-nodes-base.noOp"
