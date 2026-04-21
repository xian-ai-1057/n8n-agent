"""Node catalog models (Implements D0-2 §4, R2-2).

`NodeCatalogEntry` is the discovery-level record sourced from the xlsx.
`NodeDefinition` is the detailed record with parameter schema used by the Builder.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class NodeCatalogEntry(BaseModel):
    """Discovery-level entry — one row from xlsx, no parameter schema."""

    type: str = Field(..., description="Canonical n8n node type, e.g. 'n8n-nodes-base.slack'.")
    display_name: str
    category: str = Field(..., description="e.g. 'Core Nodes', 'Communication'.")
    description: str
    default_type_version: float | None = Field(
        default=None,
        description="Latest known typeVersion; may be filled during ingest.",
    )
    has_detail: bool = Field(
        default=False,
        description=(
            "True when a definitions/{slug}.json file exists for this type. "
            "Synthesized by ingest (R2-2 §6); not present in xlsx source."
        ),
    )


class NodeParameter(BaseModel):
    """One parameter of a detailed NodeDefinition."""

    name: str
    display_name: str | None = None
    type: Literal[
        "string",
        "number",
        "boolean",
        "options",
        "multiOptions",
        "collection",
        "fixedCollection",
        "json",
        "color",
        "dateTime",
    ]
    required: bool = False
    default: Any = None
    description: str | None = None
    options: list[dict[str, Any]] | None = None
    schema_hint: Literal[
        "url",
        "cron",
        "node_id",
        "expression",
        "credential_ref",
        "email",
        "datetime",
        "secret",
        "resource_locator",
    ] | None = Field(
        default=None,
        description=(
            "Controlled vocabulary semantic hint beyond structural type. "
            "Values outside the allowlist raise ValidationError. See R2-2 §3."
        ),
    )


class NodeDefinition(BaseModel):
    """Detailed node schema — source for Builder parameter filling."""

    type: str
    display_name: str
    description: str
    category: str
    type_version: float
    parameters: list[NodeParameter] = Field(default_factory=list)
    credentials: list[str] = Field(
        default_factory=list,
        description="Credential type names, empty in MVP.",
    )
    inputs: list[str] = Field(default_factory=lambda: ["main"])
    outputs: list[str] = Field(default_factory=lambda: ["main"])
