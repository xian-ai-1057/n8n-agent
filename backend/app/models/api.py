"""HTTP API request/response models (Implements D0-2 §8)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..chat.session_store import SESSION_ID_PATTERN  # P1-6: canonical pattern
from .planning import StepPlan
from .validation import ValidationIssue


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)  # C1-5:A-MSG-01
    # C1-9:CHAT-API-01 — optional client-supplied session id (chat session +
    # LangGraph thread id). Server generates a uuid4-derived id when omitted.
    # Pattern matches C1-5 §3 + CHAT-SEC-01.
    session_id: str | None = Field(
        default=None,
        pattern=SESSION_ID_PATTERN,
        description=(
            "Client-supplied chat session id. Omit to let the server allocate "
            "one; reuse the value returned in ChatResponse.session_id to "
            "continue an existing conversation."
        ),
    )


class ChatResponse(BaseModel):
    ok: bool
    workflow_url: str | None = None
    workflow_id: str | None = None
    workflow_json: dict[str, Any] | None = None
    retry_count: int = 0
    errors: list[ValidationIssue] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)  # C1-5:A-RESP-01
    error_message: str | None = None

    # ------------------------------------------------------------------
    # C1-9:CHAT-API-01 — chat-layer envelope extensions
    # ------------------------------------------------------------------
    session_id: str | None = Field(
        default=None,
        description=(
            "Echo of the chat session id (always populated for chat-layer "
            "responses; may be null on legacy run_cli paths)."
        ),
    )
    assistant_text: str = Field(
        default="",
        description="Natural-language reply from the chat LLM.",
    )
    status: Literal[
        "chat", "awaiting_plan_approval", "deployed", "rejected", "completed", "error"
    ] = Field(
        default="chat",
        description="Coarse-grained turn outcome for the frontend to switch on.",
    )
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-turn tool-call trace for observability "
            "(name + args_summary + status + latency_ms). Cleared when "
            "REDACT_TRACE=1 (CHAT-SEC-01)."
        ),
    )


# C1-5:HITL-SHIP-01
# C1-9:CHAT-TOOL-02
class ConfirmPlanRequest(BaseModel):
    """Request body for POST /chat/{session_id}/confirm-plan (C1-5 §4)."""

    approved: bool
    edited_plan: list[StepPlan] | None = None
    feedback: str | None = None
