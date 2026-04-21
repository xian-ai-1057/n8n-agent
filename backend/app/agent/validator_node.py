"""Validator node wrapper (Implements C1-1 §2.4).

Delegates to `WorkflowValidator` and appends any errors to `state.messages`
so the builder retry path can read them.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..models.agent_state import AgentState
from .validator import WorkflowValidator

logger = logging.getLogger(__name__)


def validate_step(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    if state.draft is None:
        return {"error": "validate_called_without_draft"}
    validator = WorkflowValidator()
    report = validator.validate(state.draft)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "validator ok=%s errors=%d warnings=%d latency_ms=%d",
        report.ok,
        len(report.errors),
        len(report.warnings),
        elapsed_ms,
    )
    updates: dict[str, Any] = {"validation": report}
    if not report.ok:
        for e in report.errors:
            logger.warning(
                "validator fail rule=%s node=%s path=%s msg=%s",
                e.rule_id,
                e.node_name,
                e.path,
                e.message,
            )
        updates["messages"] = state.messages + [
            {
                "role": "validator",
                "content": json.dumps(
                    [e.model_dump(mode="json") for e in report.errors],
                    ensure_ascii=False,
                ),
            }
        ]
    return updates
