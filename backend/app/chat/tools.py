"""Chat-layer tools exposed to the chat LLM.

Implements C1-9:
- CHAT-TOOL-01 — ``build_workflow`` tool (kicks off the LangGraph and pauses
  at the HITL gate).
- CHAT-TOOL-02 — ``confirm_plan`` tool (resumes the paused graph with the
  user's accept / reject / edit decision).
- CHAT-API-02   — ``confirm_plan`` invokes an injected in-process callable
  (``do_confirm_plan`` from ``app.api``) instead of round-tripping through
  the public HTTP endpoint.

Design notes
------------
The two tools are produced by ``make_chat_tools(session_id, ...)``. The
factory binds the current session id and a set of injectable callables
into the tool closures. This keeps the tools fully **stateless w.r.t. the
chat dispatcher** — the dispatcher (A-4, not yet shipped) is responsible
for honoring "first-tool-call-wins" semantics (CHAT-DISP-01) and for
managing per-turn session state.

The factory also lets unit tests pass mocked callables without booting
FastAPI. In particular ``confirm_plan_callable`` defaults to
``app.api.do_confirm_plan`` (the in-process callable re-exported from the
routes module), so prod stays on a single code path.

Errors & return shape
---------------------
Tools NEVER raise. Any exception is caught and returned as a structured
error dict so the chat LLM cannot accidentally see a Python traceback (which
would tend to hallucinate). The shapes follow the spec examples in
``docs/L1-components/C1-9_Chat_Layer.md``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from ..agent.builder import BuilderTimeoutError
from ..agent.graph import (
    SessionNotFound,
    run_graph_until_interrupt,
)
from ..agent.retriever_protocol import RetrieverProtocol
from ..models.agent_state import AgentState
from ..models.api import ConfirmPlanRequest
from ..models.enums import StepIntent
from ..models.planning import StepPlan

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic argument schemas — these double as the OpenAI tool JSON schema.
# ---------------------------------------------------------------------------


# C1-9:CHAT-TOOL-01
class BuildWorkflowArgs(BaseModel):
    """Args schema for the ``build_workflow`` tool."""

    user_request: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="The user's full automation request, restated clearly and self-contained.",
    )
    clarifications: dict[str, str] | None = Field(
        default=None,
        description=(
            "Key-value clarifications gathered during chat "
            "(e.g. {'frequency': 'hourly', 'destination': 'sheet'})."
        ),
    )


# C1-9:CHAT-TOOL-02
class StepEdit(BaseModel):
    """Partial-update patch for a single ``StepPlan``.

    The chat LLM emits these when the user asks to refine a plan ("change
    step 2 to use Slack"). When ``pending_plan`` is available the patches
    are merged on top of the existing plan; otherwise every patch must
    carry the fields needed to materialise a full ``StepPlan``.
    """

    step_id: str = Field(..., description="ID of the step to edit, e.g. 'step_2'.")
    description: str | None = Field(default=None, max_length=200)
    intent: StepIntent | None = Field(default=None)
    candidate_node_types: list[str] | None = Field(
        default=None,
        max_length=5,
        description="Override the node-type candidate list for this step.",
    )
    reason: str | None = Field(default=None, max_length=300)


# C1-9:CHAT-TOOL-02
class ConfirmPlanArgs(BaseModel):
    """Args schema for the ``confirm_plan`` tool."""

    approved: bool = Field(
        ...,
        description="True to accept the plan (with optional edits); False to reject.",
    )
    edits: list[StepEdit] | None = Field(
        default=None,
        description="Optional per-step patches; only honored when approved=True.",
    )
    feedback: str | None = Field(
        default=None,
        max_length=500,
        description="Optional free-form rejection reason or commentary.",
    )


# ---------------------------------------------------------------------------
# Docstrings exposed to the LLM (also embedded as the tool description).
# ---------------------------------------------------------------------------


# C1-9:CHAT-TOOL-01
BUILD_WORKFLOW_DOCSTRING = """\
Build an n8n workflow from a user request.

Use this ONLY when:
1. The user clearly wants to automate something (build / schedule / sync /
   fetch / ...).
2. You have enough information: trigger, source, destination, frequency
   (if any).

Do NOT call this tool if:
- The user is just chatting (greetings, weather, off-topic).
- The request is ambiguous (you still have unresolved questions — ask them
  first).
- The user has rejected a previous plan and is exploring alternatives
  without committing.

After this tool returns 'awaiting_plan_approval', present the plan_summary
to the user and wait for their confirmation. Then call confirm_plan with
their decision.
"""


# C1-9:CHAT-TOOL-02
CONFIRM_PLAN_DOCSTRING = """\
Confirm, edit, or reject a plan that build_workflow returned.

Call this only when:
- The previous tool call was build_workflow returning awaiting_plan_approval.
- The user has explicitly responded ("yes / 確認 / 改 step 2 / 取消 / ...").

Do NOT call this tool:
- Before any plan exists.
- If the user has not given a clear yes/no/edit decision.

Set approved=true for confirmation (with optional edits) or approved=false
to reject.
"""


# ---------------------------------------------------------------------------
# Constants & types
# ---------------------------------------------------------------------------

# Pre-flight minimum length for ``user_request`` (CHAT-TOOL-01 §error handling).
_MIN_REQUEST_LEN = 10

# In-process callable signatures — kept loose so tests can pass mocks.
ConfirmPlanCallable = Callable[..., dict[str, Any]]
RunGraphCallable = Callable[..., dict[str, Any]]


def _default_confirm_plan_callable() -> ConfirmPlanCallable:
    """Lazy import to avoid circular imports at module load time."""
    from ..api import do_confirm_plan  # noqa: WPS433 (intentional local import)

    return do_confirm_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_user_message(
    user_request: str, clarifications: dict[str, str] | None
) -> str:
    """Append clarifications block to user_request if any are supplied."""
    if not clarifications:
        return user_request
    lines = ["Collected context:"]
    for k, v in clarifications.items():
        lines.append(f"- {k}: {v}")
    return f"{user_request}\n\n" + "\n".join(lines)


def _summarise_plan(plan: list[StepPlan]) -> str:
    """Return a numbered, human-readable plan summary for the LLM."""
    if not plan:
        return "(empty plan)"
    return "\n".join(
        f"{i + 1}. [{p.step_id}] {p.description}" for i, p in enumerate(plan)
    )


def _categorise_state_error(state: AgentState) -> tuple[str, str]:
    """Map ``state.error`` prefix → (error_category, error_message)."""
    err = state.error or "unknown error"
    if err.startswith("building_timeout:"):
        return "building_timeout", err
    if err.startswith("building_failed:"):
        return "building_failed", err
    if err.startswith("give_up:"):
        return "give_up", err
    if err.startswith("plan_rejected:"):
        return "plan_rejected", err
    return "internal_error", err


def _log_tool_event(
    *,
    tool_name: str,
    session_id: str,
    started_at: float,
    status: str,
    exc_type: str | None = None,
) -> None:
    """Structured log per tool call (CHAT-OBS-01 partial — full obs in dispatcher)."""
    latency_ms = int((time.monotonic() - started_at) * 1000)
    payload = {
        "event": "chat_tool_call",
        "tool_name": tool_name,
        "session_id": session_id,
        "status": status,
        "latency_ms": latency_ms,
    }
    if exc_type is not None:
        payload["exc_type"] = exc_type
        logger.warning("chat_tool_call_failed", extra=payload)
    else:
        logger.info("chat_tool_call_ok", extra=payload)


def _merge_edits_into_plan(
    edits: list[StepEdit],
    pending_plan: list[StepPlan] | None,
) -> list[StepPlan]:
    """Apply ``edits`` patches to ``pending_plan`` and return the new plan.

    Two modes:

    1. **Merge mode** (``pending_plan`` provided): for each edit, find the
       matching ``step_id`` in pending_plan and override only the fields
       present in the patch. Unknown ``step_id`` raises ``ValueError``.

    2. **Standalone mode** (``pending_plan is None``): every edit must
       supply enough fields to materialise a full ``StepPlan``. Used as a
       fallback so the tool still works even when the dispatcher hasn't
       wired in the live plan snapshot.
    """
    if pending_plan is not None:
        # Index by step_id for O(1) lookup.
        by_id: dict[str, StepPlan] = {p.step_id: p for p in pending_plan}
        new_plan: list[StepPlan] = []
        for edit in edits:
            base = by_id.get(edit.step_id)
            if base is None:
                raise ValueError(
                    f"unknown step_id in edit: {edit.step_id!r}"
                )
            patch: dict[str, Any] = base.model_dump()
            if edit.description is not None:
                patch["description"] = edit.description
            if edit.intent is not None:
                patch["intent"] = edit.intent
            if edit.candidate_node_types is not None:
                patch["candidate_node_types"] = edit.candidate_node_types
            if edit.reason is not None:
                patch["reason"] = edit.reason
            new_plan.append(StepPlan.model_validate(patch))

        # Carry over any unedited steps in original order.
        edited_ids = {e.step_id for e in edits}
        merged: list[StepPlan] = []
        for p in pending_plan:
            if p.step_id in edited_ids:
                # Find the edit's resolved StepPlan in new_plan
                matching = next(np for np in new_plan if np.step_id == p.step_id)
                merged.append(matching)
            else:
                merged.append(p)
        return merged

    # Standalone mode: edit must have all required StepPlan fields.
    out: list[StepPlan] = []
    for edit in edits:
        if (
            edit.description is None
            or edit.intent is None
            or edit.candidate_node_types is None
            or edit.reason is None
        ):
            raise ValueError(
                "edits without pending_plan must include description, intent, "
                "candidate_node_types and reason for each step"
            )
        out.append(
            StepPlan(
                step_id=edit.step_id,
                description=edit.description,
                intent=edit.intent,
                candidate_node_types=edit.candidate_node_types,
                reason=edit.reason,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Tool implementations (private; wrapped via factory)
# ---------------------------------------------------------------------------


# C1-9:CHAT-TOOL-01
def _build_workflow_impl(
    user_request: str,
    clarifications: dict[str, str] | None,
    *,
    session_id: str,
    retriever: RetrieverProtocol | None,
    deploy_enabled: bool,
    run_graph_callable: RunGraphCallable,
) -> dict[str, Any]:
    """Execute the build_workflow tool; never raises."""
    started_at = time.monotonic()

    # Pre-flight validation: too-short request → reject without invoking graph.
    stripped = (user_request or "").strip()
    if len(stripped) < _MIN_REQUEST_LEN:
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="invalid_argument",
        )
        return {
            "ok": False,
            "status": "invalid_argument",
            "error": "user_request_too_short",
            "error_message": (
                f"user_request must be at least {_MIN_REQUEST_LEN} characters"
            ),
            "session_id": session_id,
        }

    user_message = _format_user_message(stripped, clarifications)

    try:
        result = run_graph_callable(
            user_message,
            session_id,
            retriever=retriever,
            deploy_enabled=deploy_enabled,
        )
    except BuilderTimeoutError as exc:
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="error",
            exc_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "error",
            "error_category": "building_timeout",
            "error_message": f"building_timeout: {exc}",
            "session_id": session_id,
        }
    except Exception as exc:  # noqa: BLE001 — must not leak to LLM
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="error",
            exc_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "error",
            "error": f"tool_internal: {type(exc).__name__}",
            "error_message": str(exc),
            "session_id": session_id,
        }

    status = result.get("status")
    state: AgentState | None = result.get("state")
    plan: list[StepPlan] = list(result.get("plan") or [])

    if status == "awaiting_plan_approval":
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="awaiting_plan_approval",
        )
        return {
            "ok": True,
            "status": "awaiting_plan_approval",
            "plan_summary": _summarise_plan(plan),
            "plan": [p.model_dump() for p in plan],
            "session_id": session_id,
        }

    # Either "completed" (HITL_ENABLED=0 fast path) or unexpected status.
    if state is None:
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="error",
        )
        return {
            "ok": False,
            "status": "error",
            "error": "tool_internal: missing_state",
            "session_id": session_id,
        }

    if state.error:
        category, message = _categorise_state_error(state)
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="error",
        )
        return {
            "ok": False,
            "status": "error",
            "error_category": category,
            "error_message": message,
            "session_id": session_id,
        }

    if state.workflow_url is not None:
        _log_tool_event(
            tool_name="build_workflow",
            session_id=session_id,
            started_at=started_at,
            status="deployed",
        )
        return {
            "ok": True,
            "status": "deployed",
            "workflow_url": state.workflow_url,
            "workflow_id": state.workflow_id,
            "session_id": session_id,
        }

    # Completed with no workflow_url and no error → dry run / deploy disabled.
    _log_tool_event(
        tool_name="build_workflow",
        session_id=session_id,
        started_at=started_at,
        status="completed",
    )
    return {
        "ok": True,
        "status": "completed",
        "session_id": session_id,
    }


# C1-9:CHAT-TOOL-02
# C1-9:CHAT-API-02
def _confirm_plan_impl(
    approved: bool,
    edits: list[StepEdit] | None,
    feedback: str | None,
    *,
    session_id: str,
    deploy_enabled: bool,
    confirm_plan_callable: ConfirmPlanCallable,
    pending_plan: list[StepPlan] | None,
) -> dict[str, Any]:
    """Execute the confirm_plan tool; never raises.

    Note re CHAT-API-02: this calls ``confirm_plan_callable`` (default
    ``app.api.do_confirm_plan``) directly in-process — it does NOT round-trip
    through HTTP.
    """
    started_at = time.monotonic()

    # Build edited_plan if edits were supplied (and approved).
    edited_plan: list[StepPlan] | None = None
    if approved and edits:
        try:
            edited_plan = _merge_edits_into_plan(edits, pending_plan)
        except (ValueError, ValidationError) as exc:
            _log_tool_event(
                tool_name="confirm_plan",
                session_id=session_id,
                started_at=started_at,
                status="invalid_argument",
                exc_type=type(exc).__name__,
            )
            return {
                "ok": False,
                "status": "invalid_argument",
                "error_message": str(exc),
                "session_id": session_id,
            }

    # Build the request body for the in-process confirm endpoint.
    # C1-9:CHAT-TOOL-02 — include feedback so the graph writes
    # "plan_rejected: <reason>" into state.error on rejection.
    try:
        req = ConfirmPlanRequest(
            approved=approved,
            edited_plan=edited_plan,
            feedback=feedback,
        )
    except ValidationError as exc:
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="invalid_argument",
            exc_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "invalid_argument",
            "error_message": str(exc),
            "session_id": session_id,
        }

    try:
        # CHAT-API-02 — injected callable, NOT a self-HTTP call.
        result = confirm_plan_callable(
            session_id, req, deploy_enabled=deploy_enabled
        )
    except SessionNotFound as exc:
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="session_expired",
            exc_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "session_expired",
            "error": "session_expired",
            "error_message": (
                f"session {session_id!r} expired or not found; please start over."
            ),
            "session_id": session_id,
        }
    except ValueError as exc:
        # _do_confirm_plan raises ValueError for invalid edited_plan.
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="invalid_argument",
            exc_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "invalid_argument",
            "error_message": str(exc),
            "session_id": session_id,
        }
    except Exception as exc:  # noqa: BLE001
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="error",
            exc_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "status": "error",
            "error": f"tool_internal: {type(exc).__name__}",
            "error_message": str(exc),
            "session_id": session_id,
        }

    # Map graph result → tool dict. ``result`` follows the
    # ``resume_graph_with_confirmation`` shape: status ∈
    # {"completed", "rejected", "error"}.
    inner_status = result.get("status")
    state: AgentState | None = result.get("state")

    if inner_status == "rejected":
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="rejected",
        )
        return {
            "ok": True,
            "status": "rejected",
            "message": "plan rejected; you can refine the request and try again.",
            "session_id": session_id,
        }

    if inner_status == "error" or (state is not None and state.error):
        category, message = (
            _categorise_state_error(state) if state else ("internal_error", "unknown")
        )
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="error",
        )
        return {
            "ok": False,
            "status": "deploy_failed" if category == "give_up" else "error",
            "error_category": category,
            "error_message": message,
            "session_id": session_id,
        }

    # Successful resume → graph completed.
    if state is None:
        _log_tool_event(
            tool_name="confirm_plan",
            session_id=session_id,
            started_at=started_at,
            status="error",
        )
        return {
            "ok": False,
            "status": "error",
            "error": "tool_internal: missing_state",
            "session_id": session_id,
        }

    _log_tool_event(
        tool_name="confirm_plan",
        session_id=session_id,
        started_at=started_at,
        status="deployed",
    )
    return {
        "ok": True,
        "status": "deployed",
        "workflow_url": state.workflow_url,
        "workflow_id": state.workflow_id,
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Factory — the public entry point used by the dispatcher and tests.
# ---------------------------------------------------------------------------


# C1-9:CHAT-TOOL-01
# C1-9:CHAT-TOOL-02
# C1-9:CHAT-API-02
def make_chat_tools(
    session_id: str,
    *,
    retriever: RetrieverProtocol | None = None,
    deploy_enabled: bool = True,
    confirm_plan_callable: ConfirmPlanCallable | None = None,
    run_graph_callable: RunGraphCallable | None = None,
    pending_plan: list[StepPlan] | None = None,
) -> list[StructuredTool]:
    """Build the two ``StructuredTool`` instances bound to this chat turn.

    Parameters
    ----------
    session_id:
        The chat / LangGraph thread id. Must already be validated by the
        caller (the dispatcher does ``_validate_session_id`` on entry).
    retriever:
        Optional retriever override; ``None`` means "use the default in
        ``run_graph_until_interrupt``".
    deploy_enabled:
        Forwarded to graph helpers; usually derived from
        ``settings.n8n_api_key``.
    confirm_plan_callable:
        Injection seam for CHAT-API-02. Defaults to
        ``app.api.do_confirm_plan`` (lazy-imported to avoid a circular
        import at module load).
    run_graph_callable:
        Injection seam for build_workflow. Defaults to
        ``app.agent.graph.run_graph_until_interrupt``.
    pending_plan:
        The current ``state.plan`` (if any) used by ``confirm_plan`` to
        merge partial ``edits`` patches. The dispatcher (A-4) is expected
        to fish this out of the LangGraph checkpointer and inject it here
        when the session is in ``awaiting_plan_approval`` state.

    Returns
    -------
    list[StructuredTool]
        ``[build_workflow_tool, confirm_plan_tool]`` — order is stable so
        the dispatcher can wire them into the LLM call.
    """
    confirm_callable: ConfirmPlanCallable = (
        confirm_plan_callable
        if confirm_plan_callable is not None
        else _default_confirm_plan_callable()
    )
    run_callable: RunGraphCallable = (
        run_graph_callable
        if run_graph_callable is not None
        else run_graph_until_interrupt
    )

    # ------------------------------------------------------------------
    # build_workflow tool
    # ------------------------------------------------------------------
    def _build_workflow(
        user_request: str,
        clarifications: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return _build_workflow_impl(
            user_request,
            clarifications,
            session_id=session_id,
            retriever=retriever,
            deploy_enabled=deploy_enabled,
            run_graph_callable=run_callable,
        )

    build_tool = StructuredTool.from_function(
        func=_build_workflow,
        name="build_workflow",
        description=BUILD_WORKFLOW_DOCSTRING,
        args_schema=BuildWorkflowArgs,
    )

    # ------------------------------------------------------------------
    # confirm_plan tool
    # ------------------------------------------------------------------
    def _confirm_plan(
        approved: bool,
        edits: list[dict[str, Any]] | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        # When invoked through StructuredTool with ConfirmPlanArgs, edits is
        # already list[StepEdit]. But langchain may pass dicts in some flows
        # so coerce defensively.
        coerced_edits: list[StepEdit] | None = None
        if edits:
            try:
                coerced_edits = [
                    e if isinstance(e, StepEdit) else StepEdit.model_validate(e)
                    for e in edits
                ]
            except ValidationError as exc:
                return {
                    "ok": False,
                    "status": "invalid_argument",
                    "error_message": str(exc),
                    "session_id": session_id,
                }
        return _confirm_plan_impl(
            approved,
            coerced_edits,
            feedback,
            session_id=session_id,
            deploy_enabled=deploy_enabled,
            confirm_plan_callable=confirm_callable,
            pending_plan=pending_plan,
        )

    confirm_tool = StructuredTool.from_function(
        func=_confirm_plan,
        name="confirm_plan",
        description=CONFIRM_PLAN_DOCSTRING,
        args_schema=ConfirmPlanArgs,
    )

    return [build_tool, confirm_tool]


__all__ = [
    "BUILD_WORKFLOW_DOCSTRING",
    "BuildWorkflowArgs",
    "CONFIRM_PLAN_DOCSTRING",
    "ConfirmPlanArgs",
    "StepEdit",
    "make_chat_tools",
]
