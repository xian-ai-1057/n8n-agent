"""Planning-phase models (Implements D0-2 §3 & §4)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .catalog import NodeDefinition
from .enums import StepIntent


class StepPlan(BaseModel):
    """A single step produced by the Planner from user intent + discovery RAG hits."""

    step_id: str = Field(..., description="Stable id within the plan, e.g. 'step_1'.")
    description: str = Field(
        ...,
        max_length=200,
        description="Natural-language step description.",
    )
    intent: StepIntent = Field(..., description="Coarse intent classification.")
    candidate_node_types: list[str] = Field(
        ...,
        min_length=1,
        max_length=5,
        description=(
            "Ranked n8n node types the Builder may choose from "
            "(e.g. 'n8n-nodes-base.httpRequest')."
        ),
    )
    reason: str = Field(..., max_length=300, description="Why these candidates match.")

    @field_validator("candidate_node_types")
    @classmethod
    def _types_nonempty(cls, v: list[str]) -> list[str]:
        if any(not t.strip() for t in v):
            raise ValueError("candidate_node_types must not contain empty strings")
        return v


class NodeCandidate(BaseModel):
    """Builder-time bundle: a StepPlan combined with the chosen NodeDefinition."""

    step_id: str
    chosen_type: str
    definition: NodeDefinition | None = Field(
        default=None,
        description=(
            "None means type was found in discovery only; "
            "Builder may emit an empty shell."
        ),
    )
