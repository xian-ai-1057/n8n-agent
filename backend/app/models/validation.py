"""Validation result models (Implements D0-2 §6)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import ValidationSeverity


class ValidationIssue(BaseModel):
    rule_id: str = Field(..., description="See C1-4 rule table, e.g. 'V-NODE-001'.")
    severity: ValidationSeverity
    message: str
    node_name: str | None = None
    path: str | None = Field(
        default=None,
        description="Dotted path into the draft, e.g. 'nodes[3].parameters.url'.",
    )
    rule_class: str = "structural"  # C1-4:V-PARAM-009
    suggested_fix: str | None = None  # C1-4:V-PARAM-009


class ValidationReport(BaseModel):
    ok: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> "ValidationReport":
        errs = [i for i in issues if i.severity == ValidationSeverity.ERROR]
        warns = [i for i in issues if i.severity == ValidationSeverity.WARNING]
        return cls(ok=len(errs) == 0, errors=errs, warnings=warns)
