"""Agent subpackage (Implements C1-1 scaffold).

Phase 1-C ships the deterministic `WorkflowValidator` and re-exports it here.
Phase 2 will add planner / builder / assembler / graph modules.
"""

from .validator import WorkflowValidator, validate_workflow

__all__ = ["WorkflowValidator", "validate_workflow"]
