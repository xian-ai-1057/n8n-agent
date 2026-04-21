"""Planner node (Implements C1-1 §2.1 + R2-3 §1)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel

from ..models.agent_state import AgentState
from ..models.planning import StepPlan
from .llm import LLMTimeoutError, get_llm, invoke_with_timeout
from .prompts.loader import render_prompt
from .retriever_protocol import (
    RetrieverProtocol,
    format_discovery_hits,
)

logger = logging.getLogger(__name__)

DISCOVERY_K = 8

# Core control-flow / utility node types the planner should *always* see as
# candidates, even when discovery embedding fails to surface them. Measured
# on 2026-04-20: embeddinggemma did not rank `if`/`switch` for the Chinese
# conditional prompt "body.type=='urgent' 則發 X 否則 Y" at any k up to 20,
# causing the planner to pick `respondToWebhook` for the condition step.
_CORE_CONTROL_TYPES = (
    "n8n-nodes-base.if",
    "n8n-nodes-base.switch",
    "n8n-nodes-base.filter",
    "n8n-nodes-base.merge",
    "n8n-nodes-base.set",
    "n8n-nodes-base.code",
)


class PlannerOutput(BaseModel):
    """LLM structured-output wrapper (C1-1 §3)."""

    steps: list[StepPlan]


_CORE_CATALOG_ENTRIES: dict[str, Any] | None = None


def _load_core_catalog() -> dict[str, Any]:
    """Lazy-load the catalog_discovery.json once so we can cheaply pull core
    control-flow entries by type without re-embedding queries.
    """
    global _CORE_CATALOG_ENTRIES
    if _CORE_CATALOG_ENTRIES is not None:
        return _CORE_CATALOG_ENTRIES

    from pathlib import Path

    from ..models.catalog import NodeCatalogEntry

    path = (
        Path(__file__).resolve().parents[3] / "data" / "nodes" / "catalog_discovery.json"
    )
    entries: dict[str, Any] = {}
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for raw in json.load(f):
                if raw.get("type") in _CORE_CONTROL_TYPES:
                    try:
                        entries[raw["type"]] = NodeCatalogEntry.model_validate(raw)
                    except Exception:  # noqa: BLE001
                        pass
    _CORE_CATALOG_ENTRIES = entries
    return entries


def _augment_with_core_controls(hits: list, retriever: RetrieverProtocol) -> list:
    """Append core control-flow nodes that discovery missed.

    Measured on 2026-04-20: embeddinggemma fails to rank `if`/`switch` for
    Chinese conditional user messages, causing the planner to pick semantically
    wrong alternatives (e.g. `respondToWebhook`). Seeding these core types
    unconditionally sidesteps the embedding gap; ranking of retrieved hits is
    preserved — core entries are appended to the end.
    """
    existing = {h.type for h in hits}
    core = _load_core_catalog()
    appended = list(hits)
    for t in _CORE_CONTROL_TYPES:
        if t in existing or t not in core:
            continue
        appended.append(core[t])
    return appended


def plan_step(state: AgentState, retriever: RetrieverProtocol) -> dict[str, Any]:
    """Run the planner node.

    Returns a state delta dict LangGraph will merge.
    """
    t0 = time.monotonic()
    logger.info("planner start msg=%r", state.user_message[:80])
    hits = retriever.search_discovery(state.user_message, DISCOVERY_K)
    hits = _augment_with_core_controls(hits, retriever)
    logger.info(
        "retriever top_k=%d types=%s",
        len(hits),
        [h.type for h in hits],
    )
    hit_text = format_discovery_hits(hits)
    prompt = render_prompt(
        "planner",
        {
            "user_message": state.user_message,
            "discovery_hits": hit_text or "(no hits)",
        },
    )

    llm = get_llm(PlannerOutput)
    try:
        result: PlannerOutput = invoke_with_timeout(llm, prompt)  # type: ignore[assignment]
    except LLMTimeoutError as exc:
        logger.warning("planner LLM timeout: %s", exc)
        return {
            "error": f"planning_timeout: {exc}",
            "messages": state.messages
            + [{"role": "planner", "content": f"timeout: {exc}"}],
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner LLM call failed")
        return {
            "error": f"planning_failed: {exc}",
            "messages": state.messages
            + [{"role": "planner", "content": f"error: {exc}"}],
        }

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "planner ok steps=%d latency_ms=%d", len(result.steps), elapsed_ms
    )

    discovery_hits = [h.model_dump() for h in hits]
    return {
        "plan": result.steps,
        "discovery_hits": discovery_hits,
        "messages": state.messages
        + [
            {
                "role": "planner",
                "content": json.dumps(
                    [s.model_dump(mode="json") for s in result.steps],
                    ensure_ascii=False,
                ),
            }
        ],
    }
