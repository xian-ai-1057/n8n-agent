"""HTTP API request/response models (Implements D0-2 §8)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .validation import ValidationIssue


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)  # C1-5:A-MSG-01


class ChatResponse(BaseModel):
    ok: bool
    workflow_url: str | None = None
    workflow_id: str | None = None
    workflow_json: dict[str, Any] | None = None
    retry_count: int = 0
    errors: list[ValidationIssue] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)  # C1-5:A-RESP-01
    error_message: str | None = None
