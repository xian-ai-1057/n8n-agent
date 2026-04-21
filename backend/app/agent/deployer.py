"""Deployer node (Implements C1-1 §2.5).

Short-circuits to a dry-run result when no API key is configured — this keeps
the CLI usable without a live n8n.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import get_settings
from ..models.agent_state import AgentState
from ..n8n.client import N8nClient
from ..n8n.errors import N8nApiError

logger = logging.getLogger(__name__)


def deploy_step(state: AgentState) -> dict[str, Any]:
    t0 = time.monotonic()
    if state.draft is None:
        return {"error": "deploy_called_without_draft"}

    settings = get_settings()
    if not settings.n8n_api_key:
        logger.info("deployer dry-run (N8N_API_KEY not set)")
        return {
            "workflow_id": None,
            "workflow_url": None,
            "messages": state.messages
            + [
                {
                    "role": "deployer",
                    "content": "dry_run: N8N_API_KEY not set",
                }
            ],
        }

    try:
        with N8nClient() as client:
            result = client.create_workflow(state.draft)
    except N8nApiError as exc:
        logger.exception("deployer failed")
        return {
            "error": f"deploy_failed: {exc}",
            "messages": state.messages
            + [{"role": "deployer", "content": f"error: {exc}"}],
        }

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("deployer ok id=%s latency_ms=%d", result.id, elapsed_ms)
    return {
        "workflow_id": result.id,
        "workflow_url": result.url,
        "messages": state.messages
        + [
            {
                "role": "deployer",
                "content": f"deployed id={result.id} url={result.url}",
            }
        ],
    }
