"""Builder node (Implements C1-1 §2.2 + R2-3 §2 / §3)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel

from ..models.agent_state import AgentState
from ..models.planning import NodeCandidate
from ..models.workflow import BuiltNode, Connection
from .llm import LLMTimeoutError, get_llm, invoke_with_timeout
from .prompts.loader import render_prompt
from .retriever_protocol import (
    RetrieverProtocol,
    definitions_as_trimmed_json,
)

logger = logging.getLogger(__name__)


class BuilderOutput(BaseModel):
    """LLM structured-output wrapper (C1-1 §3)."""

    nodes: list[BuiltNode]
    connections: list[Connection]


# Rough char budget for the builder prompt. Above this we drop trailing
# definitions (least-ranked by plan order) to avoid LLM context overrun.
_PROMPT_CHAR_BUDGET: int = 12000


def _collect_candidates(
    plan: list, retriever: RetrieverProtocol
) -> tuple[list[NodeCandidate], list]:
    """For each StepPlan, pick the first candidate type and fetch its detailed
    definition. Return (candidates, definitions) where candidates align with
    plan order and definitions is a deduped list for the prompt.
    """
    candidates: list[NodeCandidate] = []
    seen_types: set[str] = set()
    defs_ordered: list = []
    for step in plan:
        if not step.candidate_node_types:
            continue
        chosen = step.candidate_node_types[0]
        defn = retriever.get_detail(chosen)
        candidates.append(
            NodeCandidate(step_id=step.step_id, chosen_type=chosen, definition=defn)
        )
        if defn is not None and chosen not in seen_types:
            defs_ordered.append(defn)
            seen_types.add(chosen)
    return candidates, defs_ordered


def _render_builder_prompt(
    state: AgentState,
    plan_payload: list[dict[str, Any]],
    defs_payload: list[dict[str, Any]],
) -> str:
    return render_prompt(
        "builder",
        {
            "user_message": state.user_message,
            "plan_json": json.dumps(plan_payload, ensure_ascii=False),
            "definitions_json": json.dumps(defs_payload, ensure_ascii=False),
        },
    )


def _render_fix_prompt(
    state: AgentState,
    defs_payload: list[dict[str, Any]],
) -> str:
    return render_prompt(
        "fix",
        {
            "user_message": state.user_message,
            "previous_nodes_json": json.dumps(
                [n.model_dump(by_alias=True, exclude_none=True) for n in state.built_nodes],
                ensure_ascii=False,
            ),
            "previous_connections_json": json.dumps(
                [c.model_dump(mode="json") for c in state.connections],
                ensure_ascii=False,
            ),
            "errors_json": json.dumps(
                [e.model_dump(mode="json") for e in state.validation.errors],
                ensure_ascii=False,
            ),
            "definitions_json": json.dumps(defs_payload, ensure_ascii=False),
        },
    )


def build_nodes(
    state: AgentState,
    retriever: RetrieverProtocol,
) -> dict[str, Any]:
    """Run the builder node (fresh or retry depending on state)."""
    t0 = time.monotonic()

    candidates, definitions = _collect_candidates(state.plan, retriever)

    plan_payload = [s.model_dump(mode="json") for s in state.plan]
    defs_payload = definitions_as_trimmed_json(definitions)

    is_retry = bool(
        state.validation
        and not state.validation.ok
        and state.retry_count >= 1
        and state.built_nodes
    )

    if is_retry:
        logger.info(
            "builder retry=%d fixing=%s",
            state.retry_count,
            [e.rule_id for e in state.validation.errors],
        )
        prompt_name = "fix"
        prompt = _render_fix_prompt(state, defs_payload)
    else:
        prompt_name = "builder"
        prompt = _render_builder_prompt(state, plan_payload, defs_payload)

    if len(prompt) > _PROMPT_CHAR_BUDGET and defs_payload:
        original_len = len(prompt)
        keep = max(1, len(defs_payload) // 2)
        while len(prompt) > _PROMPT_CHAR_BUDGET and keep >= 1:
            trimmed = defs_payload[:keep]
            if is_retry:
                prompt = _render_fix_prompt(state, trimmed)
            else:
                prompt = _render_builder_prompt(state, plan_payload, trimmed)
            if keep == 1:
                break
            keep = max(1, keep // 2)
        logger.warning(
            "builder prompt trimmed defs %d->%d len %d->%d (budget=%d)",
            len(defs_payload),
            keep,
            original_len,
            len(prompt),
            _PROMPT_CHAR_BUDGET,
        )
    else:
        logger.info("builder prompt len=%d defs=%d", len(prompt), len(defs_payload))

    llm = get_llm(BuilderOutput)
    try:
        result: BuilderOutput = invoke_with_timeout(llm, prompt)  # type: ignore[assignment]
    except LLMTimeoutError as exc:
        logger.warning("builder LLM timeout (%s): %s", prompt_name, exc)
        return {
            "built_nodes": [],
            "connections": [],
            "candidates": candidates,
            "messages": state.messages
            + [{"role": "builder", "content": f"timeout: {exc}"}],
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("builder LLM call failed (%s)", prompt_name)
        return {
            "error": f"building_failed: {exc}",
            "messages": state.messages
            + [{"role": "builder", "content": f"error: {exc}"}],
        }

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "builder ok prompt=%s nodes=%d conns=%d retry=%d latency_ms=%d",
        prompt_name,
        len(result.nodes),
        len(result.connections),
        state.retry_count,
        elapsed_ms,
    )

    return {
        "built_nodes": result.nodes,
        "connections": result.connections,
        "candidates": candidates,
        "messages": state.messages
        + [
            {
                "role": "builder",
                "content": json.dumps(
                    {
                        "mode": prompt_name,
                        "nodes": [
                            n.model_dump(by_alias=True, exclude_none=True)
                            for n in result.nodes
                        ],
                        "connections": [
                            c.model_dump(mode="json") for c in result.connections
                        ],
                    },
                    ensure_ascii=False,
                ),
            }
        ],
    }
