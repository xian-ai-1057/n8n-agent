"""LangGraph shared agent state (Implements D0-2 §7).

D0-2 specifies a Pydantic `AgentState`. LangGraph accepts either a `TypedDict` or a
Pydantic model as the state type — we follow the SSOT and use Pydantic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .catalog import NodeDefinition  # re-export convenience
from .planning import NodeCandidate, StepPlan
from .validation import ValidationReport
from .workflow import BuiltNode, Connection, WorkflowDraft

__all__ = ["AgentState", "NodeDefinition"]


class AgentState(BaseModel):
    """LangGraph shared state. Each node reads/writes a subset — see C1-1."""

    # input
    user_message: str

    # planner
    plan: list[StepPlan] = Field(default_factory=list)
    discovery_hits: list[dict[str, Any]] = Field(default_factory=list)

    # builder
    candidates: list[NodeCandidate] = Field(default_factory=list)
    built_nodes: list[BuiltNode] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)

    # assembler
    draft: WorkflowDraft | None = None

    # validator
    validation: ValidationReport | None = None
    retry_count: int = 0

    # deployer
    workflow_id: str | None = None
    workflow_url: str | None = None

    # diagnostics
    messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Role/content tuples. Validator errors are appended here before retry.",
    )
    error: str | None = None
