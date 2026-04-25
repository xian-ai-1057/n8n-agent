"""LangGraph wiring (Implements C1-1)."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver  # C1-1:HITL-SHIP-01
from langgraph.graph import END, START, StateGraph

from ..config import get_settings
from ..models.agent_state import AgentState
from ..models.planning import StepPlan  # C1-1:HITL-SHIP-01
from .assembler import assemble_step
from .builder import BuilderTimeoutError, build_nodes
from .completeness import _make_completeness_check_node  # C1-1:B-COMP-01
from .deployer import deploy_step
from .planner import plan_step
from .retriever_protocol import RetrieverProtocol, get_retriever
from .validator_node import validate_step

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# C1-1:HITL-SHIP-01 — public exception surfaced for chat / API layer.
# ----------------------------------------------------------------------


class SessionNotFound(LookupError):
    """Raised by ``resume_graph_with_confirmation`` when ``session_id`` is unknown.

    The HITL checkpointer is in-process ``MemorySaver`` for MVP (TTL=30 min by
    GC of the session store, see C1-9). When the chat layer hands us a stale
    or never-seen ``session_id`` we surface it explicitly so the API layer
    (C1-5) can map it to HTTP 404.
    """


# C1-1:HITL-SHIP-01 — process-wide singleton checkpointer. The MemorySaver
# stores graph state across separate ``invoke()`` calls keyed by ``thread_id``;
# every HITL ``build_graph`` call must reuse the SAME instance so the second
# call (resume) can find the session created by the first call (initial run).
# Tests can reset it via ``reset_hitl_checkpointer()``.
_HITL_CHECKPOINTER: MemorySaver | None = None


def _get_hitl_checkpointer() -> MemorySaver:
    global _HITL_CHECKPOINTER
    if _HITL_CHECKPOINTER is None:
        _HITL_CHECKPOINTER = MemorySaver()
    return _HITL_CHECKPOINTER


def reset_hitl_checkpointer() -> None:
    """Drop the singleton MemorySaver — primarily for unit-test isolation."""
    global _HITL_CHECKPOINTER
    _HITL_CHECKPOINTER = None


# ----------------------------------------------------------------------
# Node factories — retriever is injected via closure.
# ----------------------------------------------------------------------


def _make_plan_node(retriever: RetrieverProtocol):
    def _plan(state: AgentState) -> dict[str, Any]:
        return plan_step(state, retriever)

    return _plan


def _make_build_node(retriever: RetrieverProtocol):
    def _build(state: AgentState) -> dict[str, Any]:
        try:
            return build_nodes(state, retriever)
        except BuilderTimeoutError as exc:  # C1-1:B-TIMEOUT-01
            msg = f"building_timeout: {exc}"
            return {
                "error": msg,
                "messages": state.messages + [{"role": "builder", "content": msg}],
            }

    return _build


def _make_fix_build_node(retriever: RetrieverProtocol):
    def _fix_build(state: AgentState) -> dict[str, Any]:
        # Increment retry_count first; builder.build_nodes reads state.retry_count
        # to pick the fix prompt (retry_count >= 1 + validation errors present).
        bumped = AgentState(
            **{**state.model_dump(), "retry_count": state.retry_count + 1}
        )
        try:
            delta = build_nodes(bumped, retriever)
        except BuilderTimeoutError as exc:  # C1-1:B-TIMEOUT-01
            msg = f"building_timeout: {exc}"
            return {
                "error": msg,
                "retry_count": bumped.retry_count,
                "messages": bumped.messages + [{"role": "builder", "content": msg}],
            }
        delta["retry_count"] = bumped.retry_count
        return delta

    return _fix_build


# ----------------------------------------------------------------------
# C1-1:HITL-SHIP-02 — await_plan_approval node + conditional edge.
# ----------------------------------------------------------------------


def await_plan_approval_step(state: AgentState) -> dict[str, Any]:
    """HITL gate: pass-through under hitl=False, sink for resume under hitl=True.

    Behaviour:
      - When ``state.session_id is None`` (run_cli / non-HITL invocation), the
        graph was compiled without ``interrupt_before``; this function runs
        inline and writes ``plan_approved=True`` so the conditional edge falls
        through to ``build``.
      - When HITL is enabled the graph is compiled with
        ``interrupt_before=["await_plan_approval"]``; the *first* invoke pauses
        BEFORE this function executes. The chat / API layer then calls
        ``resume_graph_with_confirmation`` which uses ``update_state`` to inject
        ``plan_approved`` (and optionally ``plan``, ``error``) into the
        checkpointed state, then ``invoke(None, config)`` resumes. On resume,
        this node simply reads what was injected and emits a ``hitl`` message
        for observability — the conditional edge does the real routing.

    See C1-1 §2.2 (await_plan_approval contract) and C1-1:HITL-SHIP-02.
    """
    # If approval was already granted (run_cli pre-set, or HITL resume payload
    # injected plan_approved=True), pass through silently with a hitl note.
    if state.plan_approved:
        return {
            "messages": state.messages
            + [{"role": "hitl", "content": "plan approved"}],
        }

    # Non-HITL fast path: session_id is None — auto-approve so run_cli is
    # unchanged. This branch is only hit when the graph was compiled WITHOUT
    # interrupt_before (hitl_enabled=False); in HITL mode we never reach here
    # with plan_approved=False because the interrupt_before fires first.
    if state.session_id is None:
        return {
            "plan_approved": True,
            "messages": state.messages
            + [{"role": "hitl", "content": "auto-approve (hitl disabled)"}],
        }

    # HITL mode reached this branch with plan_approved=False — that means the
    # user rejected. The resume helper sets state.error="plan_rejected: ...";
    # here we just emit a diagnostic and let the conditional edge route to
    # give_up.
    return {
        "messages": state.messages
        + [{"role": "hitl", "content": "plan rejected by user"}],
    }


def _after_plan_approval(state: AgentState) -> str:  # C1-1:HITL-SHIP-02
    """Conditional edge from await_plan_approval.

    plan_approved=True → build; otherwise → give_up (carrying plan_rejected
    error if the resume helper populated it).
    """
    return "build" if state.plan_approved else "give_up"


def _after_build(state: AgentState) -> str:  # C1-1:B-TIMEOUT-01
    """Conditional edge from build node.

    Route to give_up immediately on timeout or unrecoverable build failure,
    skipping assemble/validate so they never process empty node lists.
    ok-branch routes to completeness_check (C1-1:B-COMP-01).
    """
    if state.error and (
        state.error.startswith("building_timeout:")
        or state.error.startswith("building_failed:")
    ):
        return "give_up"
    return "completeness_check"  # C1-1:B-COMP-01


def _after_validate(state: AgentState) -> str:
    """Conditional edge from validator.

    Route:
      - validation.ok → "deploy"
      - retry_count < agent_max_retries → "fix_build"
      - else → END with error populated.
    """
    if state.validation is not None and state.validation.ok:
        return "deploy"
    if state.retry_count < get_settings().agent_max_retries:
        return "fix_build"
    return "give_up"


def _give_up_step(state: AgentState) -> dict[str, Any]:
    # Preserve build-time errors (building_timeout:, building_failed:, plan_rejected:)
    if state.error:
        return {"messages": state.messages + [{"role": "system", "content": state.error}]}
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
    hitl_enabled: bool = False,  # C1-1:HITL-SHIP-01
):
    """Compile and return the LangGraph.

    When ``retriever`` is None, uses ``get_retriever()`` (Phase 2-A if present,
    filesystem stub otherwise).

    When ``deploy_enabled=False``, the ``deploy`` node is replaced with a no-op
    that records a dry-run message, so the CLI can request the full
    plan/build/validate pipeline without attempting a network POST.

    When ``hitl_enabled=True`` (C1-1:HITL-SHIP-01):
      - a ``MemorySaver`` checkpointer is attached so per-session graph state
        can be paused / resumed across HTTP calls;
      - the graph is compiled with ``interrupt_before=["await_plan_approval"]``
        which pauses *before* the gate node runs. The chat / API layer then
        injects user decision via ``update_state`` and resumes. The
        ``await_plan_approval`` node itself becomes a thin sink that emits a
        ``hitl`` diagnostic message and lets the conditional edge route on
        ``plan_approved`` (see ``_after_plan_approval``).

    Note re: spec wording. C1-1 §8 example writes
    ``interrupt_before=["build_step_loop"]``. We anchor the interrupt on the
    gate node instead because:
      1. ``await_plan_approval`` is the explicit gate in the §1 graph diagram;
      2. the user decision must be injected BEFORE the gate runs so the
         conditional edge after the gate can read ``plan_approved`` — this is
         the LangGraph-idiomatic pattern;
      3. v1 code uses bulk ``build`` (not v2 ``build_step_loop``) and the spec
         expects per-step migration to inherit HITL wiring transparently.
    The behaviour is semantically equivalent: the user confirms before any
    builder work runs.
    """
    r = retriever or get_retriever()

    g = StateGraph(AgentState)
    g.add_node("plan", _make_plan_node(r))
    g.add_node("await_plan_approval", await_plan_approval_step)  # C1-1:HITL-SHIP-02
    g.add_node("build", _make_build_node(r))
    g.add_node("completeness_check", _make_completeness_check_node(r))  # C1-1:B-COMP-01
    g.add_node("assemble", assemble_step)
    g.add_node("validate", validate_step)
    g.add_node("fix_build", _make_fix_build_node(r))
    g.add_node("deploy", deploy_step if deploy_enabled else _dry_run_deploy)
    g.add_node("give_up", _give_up_step)

    g.add_edge(START, "plan")
    g.add_edge("plan", "await_plan_approval")  # C1-1:HITL-SHIP-02
    g.add_conditional_edges(  # C1-1:HITL-SHIP-02
        "await_plan_approval",
        _after_plan_approval,
        {"build": "build", "give_up": "give_up"},
    )
    g.add_conditional_edges(
        "build",
        _after_build,
        {"completeness_check": "completeness_check", "give_up": "give_up"},  # C1-1:B-COMP-01
    )
    g.add_edge("completeness_check", "assemble")  # C1-1:B-COMP-01
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

    # C1-1:HITL-SHIP-01 — checkpointer + interrupt only when HITL is on.
    # NB: must be a process-wide singleton so resume() finds the session
    # written by the initial invoke().
    checkpointer = _get_hitl_checkpointer() if hitl_enabled else None
    compiled = g.compile(
        checkpointer=checkpointer,
        interrupt_before=["await_plan_approval"] if hitl_enabled else [],
    )
    # C1-1:B-COMP-01 — expose .graph for test introspection (langchain_core Graph
    # includes both hard and conditional edges as indexable Edge tuples).
    compiled.graph = compiled.get_graph()  # type: ignore[attr-defined]
    return compiled


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
    """Invoke the graph and return the final AgentState.

    Always non-HITL (``hitl_enabled=False``) — preserves v1 behaviour for
    eval harness, smoke tests, and any direct CLI usage. C1-1:HITL-SHIP-01
    explicitly mandates this path stays untouched.
    """
    compiled = build_graph(retriever, deploy_enabled=deploy, hitl_enabled=False)
    initial = AgentState(user_message=user_input)
    raw = compiled.invoke(initial)
    if isinstance(raw, AgentState):
        return raw
    return AgentState.model_validate(raw)


# ----------------------------------------------------------------------
# C1-1:HITL-SHIP-01 — HITL helpers consumed by chat layer (C1-9) / API (C1-5).
# ----------------------------------------------------------------------


def _hitl_config(session_id: str) -> dict[str, Any]:
    """Build the LangGraph ``configurable`` dict whose thread_id == session_id."""
    return {"configurable": {"thread_id": session_id}}


def _coerce_state(raw: Any) -> AgentState:
    """LangGraph may return either an AgentState instance or a dict."""
    if isinstance(raw, AgentState):
        return raw
    return AgentState.model_validate(raw)


def run_graph_until_interrupt(
    user_message: str,
    session_id: str,
    *,
    retriever: RetrieverProtocol | None = None,
    deploy_enabled: bool = True,
) -> dict[str, Any]:
    """Run the graph until the HITL interrupt fires (or the graph ends).

    C1-1:HITL-SHIP-01.

    Parameters
    ----------
    user_message:
        The user's natural-language workflow request (becomes
        ``AgentState.user_message``).
    session_id:
        Stable id used as the LangGraph ``thread_id`` so resume can find the
        checkpoint. Must be supplied by the caller (chat layer / API);
        validation of the id format is the caller's responsibility
        (C1-9:CHAT-SEC-01).
    retriever / deploy_enabled:
        Same semantics as ``build_graph``.

    Returns
    -------
    dict
        ``{"status": "awaiting_plan_approval" | "completed", "state": AgentState,
           "plan": list[StepPlan], "session_id": str}``.

        - ``"awaiting_plan_approval"``: the graph paused before the HITL gate.
          ``plan`` echoes ``state.plan`` for the chat layer's confirm UI.
        - ``"completed"``: the graph reached END without needing HITL — happens
          if planner produced 0 steps and the empty-plan path short-circuits,
          or if the gate auto-approves (should not occur with hitl_enabled=True
          because plan_approved is False on entry, but we handle it for safety).
    """
    compiled = build_graph(
        retriever, deploy_enabled=deploy_enabled, hitl_enabled=True
    )
    cfg = _hitl_config(session_id)
    initial = AgentState(user_message=user_message, session_id=session_id)
    raw = compiled.invoke(initial, config=cfg)
    state = _coerce_state(raw)

    snapshot = compiled.get_state(cfg)
    # ``snapshot.next`` is non-empty when an interrupt is active; if it
    # contains "await_plan_approval" we are paused at the HITL gate.
    if snapshot.next and "await_plan_approval" in snapshot.next:
        return {
            "status": "awaiting_plan_approval",
            "state": state,
            "plan": list(state.plan),
            "session_id": session_id,
        }
    return {
        "status": "completed",
        "state": state,
        "plan": list(state.plan),
        "session_id": session_id,
    }


def resume_graph_with_confirmation(
    session_id: str,
    approved: bool,
    *,
    edited_plan: list[StepPlan] | None = None,
    feedback: str | None = None,
    retriever: RetrieverProtocol | None = None,
    deploy_enabled: bool = True,
) -> dict[str, Any]:
    """Resume the paused graph after the user accepts / rejects / edits the plan.

    C1-1:HITL-SHIP-01.

    Parameters
    ----------
    session_id:
        Same id used during ``run_graph_until_interrupt``. Raises
        ``SessionNotFound`` if the checkpointer has no record (TTL expired or
        never created).
    approved:
        ``True`` → graph routes through ``build`` to deploy / give_up as usual.
        ``False`` → ``state.error = "plan_rejected: ..."`` is injected and the
        gate routes to ``give_up``.
    edited_plan:
        If supplied (only meaningful when ``approved=True``), overrides
        ``state.plan`` and adds a ``user`` message recording the edit.
    feedback:
        Optional free-form rejection reason; appended to the ``plan_rejected``
        error string for traceability.
    retriever / deploy_enabled:
        Must match the values used when the graph was first started so the
        recompiled graph wires identical nodes. The MemorySaver state is keyed
        on thread_id alone, so as long as the topology matches, resume works.

    Returns
    -------
    dict
        ``{"status": "completed" | "rejected" | "error", "state": AgentState,
           "session_id": str}``.
    """
    compiled = build_graph(
        retriever, deploy_enabled=deploy_enabled, hitl_enabled=True
    )
    cfg = _hitl_config(session_id)

    snapshot = compiled.get_state(cfg)
    # ``values`` is empty (and ``next`` is empty) when the thread_id has never
    # been initialised on this checkpointer — surface as 404-able error.
    if not snapshot.values:
        raise SessionNotFound(
            f"no HITL session found for session_id={session_id!r}"
        )

    # Build the patch we want to merge into the checkpointed state.
    patch: dict[str, Any] = {"plan_approved": bool(approved)}
    existing_messages = list(snapshot.values.get("messages", []))

    if approved:
        if edited_plan is not None:
            patch["plan"] = list(edited_plan)
            existing_messages = existing_messages + [
                {"role": "user", "content": f"plan edited: {len(edited_plan)} steps"}
            ]
            patch["messages"] = existing_messages
    else:
        # Rejection path: write a categorised error so give_up preserves it.
        reason = feedback.strip() if feedback else "user rejected plan"
        patch["error"] = f"plan_rejected: {reason}"
        existing_messages = existing_messages + [
            {"role": "user", "content": f"plan rejected: {reason}"}
        ]
        patch["messages"] = existing_messages

    # Inject the patch then resume. ``update_state`` writes a new checkpoint;
    # ``invoke(None, config)`` continues from the next pending node (the
    # await_plan_approval gate, which now sees plan_approved=...).
    compiled.update_state(cfg, patch)
    raw = compiled.invoke(None, config=cfg)
    state = _coerce_state(raw)

    if not approved:
        return {
            "status": "rejected",
            "state": state,
            "session_id": session_id,
        }
    if state.error:
        return {
            "status": "error",
            "state": state,
            "session_id": session_id,
        }
    return {
        "status": "completed",
        "state": state,
        "session_id": session_id,
    }
