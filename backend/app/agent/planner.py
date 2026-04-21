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


class PlannerOutput(BaseModel):
    """LLM structured-output wrapper (C1-1 §3)."""

    steps: list[StepPlan]


def plan_step(state: AgentState, retriever: RetrieverProtocol) -> dict[str, Any]:
    """Run the planner node.

    Returns a state delta dict LangGraph will merge.
    """
    t0 = time.monotonic()
    logger.info("planner start msg=%r", state.user_message[:80])
    hits = retriever.search_discovery(state.user_message, DISCOVERY_K)
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
