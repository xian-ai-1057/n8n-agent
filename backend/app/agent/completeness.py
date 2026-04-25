"""completeness_check node — inject skeleton BuiltNode for plan steps missing from built_nodes.

Implements C1-1 §2.4a rules B-COMP-01 through B-COMP-04 and Errors §C.

Rules summary:
  B-COMP-01: this node sits between build and assemble; fix_build bypasses it.
  B-COMP-02: pairing is done by BuiltNode.step_id; None step_id covers nothing.
  B-COMP-03: skeleton injection rules (fields, skip-on-no-candidate, diagnostic messages).
  B-COMP-04: RAG batch query; if batch raises → catch + treat as all-None; if type absent →
             type_version=1.0 (candidate.definition NOT consulted); emit "no RAG detail" diagnostic.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models.agent_state import AgentState
from ..models.planning import NodeCandidate, StepPlan
from ..models.workflow import BuiltNode
from .retriever_protocol import RetrieverProtocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skeleton construction
# ---------------------------------------------------------------------------


# C1-1:B-COMP-03
def _build_skeleton(step: StepPlan, candidate: NodeCandidate | None) -> BuiltNode | None:
    """Return None iff candidate is None (→ skip logic per B-COMP-03).

    The caller is responsible for setting the correct type_version on the
    returned skeleton (RAG-sourced or 1.0 fallback) before appending to
    built_nodes, via ``model_copy(update={"type_version": ...})``.
    This function sets type_version=1.0 as a safe default placeholder.
    """
    if candidate is None:
        return None

    step_id = step.step_id
    chosen_type = candidate.chosen_type

    # C1-1:B-COMP-03 — field rules
    return BuiltNode(
        step_id=step_id,
        name=f"Missing step {step_id}",
        type=chosen_type,
        typeVersion=1.0,  # default; overridden by caller after RAG lookup
        position=[0.0, 0.0],
        parameters={"_completeness_injected": "TODO: fill required parameters for this node"},
    )


# ---------------------------------------------------------------------------
# Core step function
# ---------------------------------------------------------------------------


# C1-1:B-COMP-01
def completeness_check_step(
    state: AgentState, retriever: RetrieverProtocol
) -> dict[str, Any]:
    """Inject skeleton BuiltNode for plan steps missing from built_nodes.

    Returns a delta dict with (at most) updated built_nodes and messages.
    If no steps are missing, returns {} (no-op; fast path).
    """
    plan = state.plan
    built_nodes = state.built_nodes
    candidates = state.candidates or []

    # C1-1:B-COMP-01 — fast path: empty/None plan → no-op
    if not plan:
        return {}

    # C1-1:B-COMP-02 — build covered set; BuiltNode.step_id is None → covers nothing
    covered: set[str] = {
        n.step_id for n in built_nodes if n.step_id is not None
    }

    # Find missing steps
    missing_steps: list[StepPlan] = [
        step for step in plan if step.step_id not in covered
    ]

    # C1-1:B-COMP-01 — all steps covered → fast path no-op
    if not missing_steps:
        return {}

    # C1-1:B-COMP-03 — build lookup dict from candidates
    candidate_by_step: dict[str, NodeCandidate] = {
        c.step_id: c for c in candidates
    }

    # C1-1:B-COMP-04 — collect chosen_types for missing steps that have a candidate
    missing_with_candidate: list[tuple[StepPlan, NodeCandidate]] = []
    for step in missing_steps:
        cand = candidate_by_step.get(step.step_id)
        if cand is not None:
            missing_with_candidate.append((step, cand))

    # C1-1:B-COMP-04 — single batch RAG lookup for all missing-step types
    unique_types: list[str] = list(
        dict.fromkeys(cand.chosen_type for _, cand in missing_with_candidate)
    )

    rag_results: dict[str, Any] = {}
    if unique_types:
        try:
            rag_results = retriever.get_definitions_by_types(unique_types)  # C1-1:B-COMP-04
        except Exception as exc:  # noqa: BLE001  # C1-1:B-COMP-04 / Errors §C
            diag_msg = (
                f"completeness: RAG batch lookup failed: {exc}; "
                "all types treated as RAG miss, injecting typeVersion=1.0"
            )
            logger.warning(diag_msg)
            # treat all types as None — rag_results stays {}

    # C1-1:B-COMP-03 / B-COMP-04 — inject skeletons
    new_nodes: list[BuiltNode] = []
    new_messages: list[dict[str, str]] = []

    for step in missing_steps:
        step_id = step.step_id
        cand = candidate_by_step.get(step_id)

        # C1-1:B-COMP-03 — no matching candidate → skip
        if cand is None:
            logger.warning(
                "completeness_check: no candidate for missing step %s; skipping", step_id
            )
            new_messages.append(
                {
                    "role": "completeness",
                    "content": f"skip missing step {step_id}: no matching candidate",
                }
            )
            continue

        chosen_type = cand.chosen_type

        # C1-1:B-COMP-04 — strict spec reading: RAG is sole source for type_version;
        # candidate.definition is NOT consulted; if RAG absent → always 1.0
        rag_def = rag_results.get(chosen_type)  # None if key absent or batch failed

        if rag_def is None:
            # C1-1:B-COMP-04 — RAG miss: use type_version=1.0, emit diagnostic
            type_version: float = 1.0
            new_messages.append(
                {
                    "role": "completeness",
                    "content": (
                        f"no RAG detail for type {chosen_type} (step {step_id}); "
                        "injecting with typeVersion=1.0"
                    ),
                }
            )
        else:
            type_version = float(rag_def.type_version)

        # Build the skeleton node
        skeleton = _build_skeleton(step, cand)
        if skeleton is None:
            # Should not happen (cand is not None here), but defensive
            continue

        # Override the default type_version set in _build_skeleton
        skeleton = skeleton.model_copy(update={"type_version": type_version})

        new_nodes.append(skeleton)
        new_messages.append(
            {
                "role": "completeness",
                "content": f"injected skeleton for missing step {step_id} (type={chosen_type})",
            }
        )

    if not new_nodes and not new_messages:
        return {}

    return {
        "built_nodes": built_nodes + new_nodes,
        "messages": state.messages + new_messages,
    }


# ---------------------------------------------------------------------------
# Node factory (mirrors _make_build_node pattern in graph.py)
# ---------------------------------------------------------------------------


# C1-1:B-COMP-01
def _make_completeness_check_node(retriever: RetrieverProtocol):
    """Factory returning a LangGraph node closure bound to `retriever`.

    Wraps completeness_check_step; on unexpected exception writes
    state.error="completeness_failed: {detail}" (Errors §C).
    """

    def _completeness(state: AgentState) -> dict[str, Any]:
        try:
            return completeness_check_step(state, retriever)
        except Exception as exc:  # noqa: BLE001  # C1-1:Errors §C
            detail = str(exc)
            err_msg = f"completeness_failed: {detail}"
            logger.error("completeness_check node failed: %s", detail, exc_info=True)
            return {
                "error": err_msg,
                "messages": state.messages
                + [{"role": "completeness", "content": err_msg}],
            }

    return _completeness
