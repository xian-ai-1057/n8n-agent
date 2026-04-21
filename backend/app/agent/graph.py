"""LangGraph wiring (Implements C1-1)."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..models.agent_state import AgentState
from .assembler import assemble_step
from .builder import build_nodes
from .deployer import deploy_step
from .planner import plan_step
from .retriever_protocol import RetrieverProtocol, get_retriever
from .validator_node import validate_step

logger = logging.getLogger(__name__)

MAX_RETRIES: int = 2


# ----------------------------------------------------------------------
# Node factories — retriever is injected via closure.
# ----------------------------------------------------------------------


def _make_plan_node(retriever: RetrieverProtocol):
    def _plan(state: AgentState) -> dict[str, Any]:
        return plan_step(state, retriever)

    return _plan


def _make_build_node(retriever: RetrieverProtocol):
    def _build(state: AgentState) -> dict[str, Any]:
        return build_nodes(state, retriever)

    return _build


def _make_fix_build_node(retriever: RetrieverProtocol):
    def _fix_build(state: AgentState) -> dict[str, Any]:
        # Increment retry_count first; builder.build_nodes reads state.retry_count
        # to pick the fix prompt (retry_count >= 1 + validation errors present).
        bumped = AgentState(
            **{**state.model_dump(), "retry_count": state.retry_count + 1}
        )
        delta = build_nodes(bumped, retriever)
        delta["retry_count"] = bumped.retry_count
        return delta

    return _fix_build


def _after_validate(state: AgentState) -> str:
    """Conditional edge from validator.

    Route:
      - validation.ok → "deploy"
      - retry_count < MAX_RETRIES → "fix_build"
      - else → END with error populated.
    """
    if state.validation is not None and state.validation.ok:
        return "deploy"
    if state.retry_count < MAX_RETRIES:
        return "fix_build"
    return "give_up"


def _give_up_step(state: AgentState) -> dict[str, Any]:
    errs = state.validation.errors if state.validation else []
    msg = f"validator failed after {state.retry_count} retries; {len(errs)} errors"
    return {
        "error": msg,
        "messages": state.messages
        + [{"role": "system", "content": msg}],
    }


# ----------------------------------------------------------------------
# Graph builder
# ----------------------------------------------------------------------


def build_graph(
    retriever: RetrieverProtocol | None = None,
    *,
    deploy_enabled: bool = True,
):
    """Compile and return the LangGraph.

    When `retriever` is None, uses `get_retriever()` (Phase 2-A if present,
    filesystem stub otherwise).

    When `deploy_enabled=False`, the `deploy` node is replaced with a no-op
    that records a dry-run message, so the CLI can request the full
    plan/build/validate pipeline without attempting a network POST.
    """
    r = retriever or get_retriever()

    g = StateGraph(AgentState)
    g.add_node("plan", _make_plan_node(r))
    g.add_node("build", _make_build_node(r))
    g.add_node("assemble", assemble_step)
    g.add_node("validate", validate_step)
    g.add_node("fix_build", _make_fix_build_node(r))
    g.add_node("deploy", deploy_step if deploy_enabled else _dry_run_deploy)
    g.add_node("give_up", _give_up_step)

    g.add_edge(START, "plan")
    g.add_edge("plan", "build")
    g.add_edge("build", "assemble")
    g.add_edge("assemble", "validate")

    g.add_conditional_edges(
        "validate",
        _after_validate,
        {
            "deploy": "deploy",
            "fix_build": "fix_build",
            "give_up": "give_up",
        },
    )

    g.add_edge("fix_build", "assemble")
    g.add_edge("deploy", END)
    g.add_edge("give_up", END)

    return g.compile()


def _dry_run_deploy(state: AgentState) -> dict[str, Any]:
    return {
        "messages": state.messages
        + [{"role": "deployer", "content": "dry_run: deploy disabled via CLI flag"}],
    }


# ----------------------------------------------------------------------
# CLI helpers
# ----------------------------------------------------------------------


def run_cli(
    user_input: str,
    *,
    deploy: bool = False,
    retriever: RetrieverProtocol | None = None,
) -> AgentState:
    """Invoke the graph and return the final AgentState."""
    compiled = build_graph(retriever, deploy_enabled=deploy)
    initial = AgentState(user_message=user_input)
    raw = compiled.invoke(initial)
    if isinstance(raw, AgentState):
        return raw
    return AgentState.model_validate(raw)
