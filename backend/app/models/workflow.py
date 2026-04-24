"""Workflow-draft models (Implements D0-2 §5, aligned with R2-1)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .enums import ConnectionType


class BuiltNode(BaseModel):
    """A node ready to be assembled into n8n workflow JSON. Aligns with R2-1."""

    id: str = Field(default_factory=lambda: str(uuid4()), description="UUID v4.")
    name: str = Field(..., description="Unique within the workflow; used as connections key.")
    type: str = Field(..., description="e.g. 'n8n-nodes-base.httpRequest'.")
    type_version: float = Field(..., alias="typeVersion")
    position: list[float] = Field(..., description="[x, y]; see C1-1 for layout rule.")
    parameters: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] | None = None
    disabled: bool | None = None
    on_error: str | None = Field(
        default=None,
        alias="onError",
        description="Replaces deprecated continueOnFail.",
    )
    execute_once: bool | None = Field(default=None, alias="executeOnce")
    retry_on_fail: bool | None = Field(default=None, alias="retryOnFail")
    notes: str | None = None
    notes_in_flow: bool | None = Field(default=None, alias="notesInFlow")
    # C1-1:B-COMP-02
    step_id: str | None = Field(
        default=None,
        exclude=True,  # C1-1:B-COMP-02 — internal only, never serialised to n8n
        description="Internal: maps this node back to its StepPlan.step_id.",
    )

    model_config = {"populate_by_name": True}

    @field_validator("position")
    @classmethod
    def _position_is_xy(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("position must be [x, y]")
        return v


class Connection(BaseModel):
    """One directed edge in n8n connections map. Source is by node NAME (not id)."""

    source_name: str
    source_output_index: int = 0
    target_name: str
    target_input_index: int = 0
    type: ConnectionType = ConnectionType.MAIN


class WorkflowDraft(BaseModel):
    """Assembler output — pre-deploy representation. Serialised to n8n JSON by the client."""

    name: str = Field(..., max_length=128)
    nodes: list[BuiltNode]
    connections: list[Connection] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=lambda: {"executionOrder": "v1"})
