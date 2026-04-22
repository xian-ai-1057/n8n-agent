"""Builder node (Implements C1-1 §2.2 + R2-3 §2 / §3)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel

from ..config import get_settings
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


# C1-1:B-TIMEOUT-01
class BuilderTimeoutError(RuntimeError):
    """Raised when the builder LLM exceeds the configured timeout.
    Caught by graph node wrapper; graph writes state.error and routes to give_up.
    """


class BuilderOutput(BaseModel):
    """LLM structured-output wrapper (C1-1 §3)."""

    nodes: list[BuiltNode]
    connections: list[Connection]


# C1-1:B-CAND-01, C1-1:B-CAND-02
def _collect_candidates(
    plan: list, retriever: RetrieverProtocol
) -> tuple[list[NodeCandidate], list[dict]]:
    """Batch-fetch definitions for all candidate types, then pick best per step.

    Uses a single batch query (O(1) Chroma round-trip) instead of one query per
    step (O(N)). For each step, falls back to candidate[1], [2], ... until one
    with a detail is found. If all candidates lack detail, uses an empty shell.

    Returns (candidates, diagnostic_messages).
    """  # C1-1:B-CAND-01, C1-1:B-CAND-02
    # 1. Collect all candidate types across all steps (deduplicated)
    all_types = list(dict.fromkeys(
        t for step in plan for t in step.candidate_node_types
    ))

    # 2. Single batch query — O(1) Chroma round-trip
    details = retriever.get_definitions_by_types(all_types)

    candidates: list[NodeCandidate] = []
    messages: list[dict] = []

    # 3. Per step: find first candidate that has a detail, with fallback
    for step in plan:
        if not step.candidate_node_types:
            continue

        chosen = None
        definition = None
        fallback_index = -1

        for idx, t in enumerate(step.candidate_node_types):
            if details.get(t) is not None:
                chosen = t
                definition = details[t]
                fallback_index = idx
                break

        if chosen is None:
            # All candidates lack detail — use first, proceed with empty shell
            chosen = step.candidate_node_types[0]
            messages.append({
                "role": "builder",
                "content": (
                    f"fallback_exhausted: step={step.step_id} all "
                    f"{len(step.candidate_node_types)} candidates lack detail, "
                    f"proceeding with empty shell for '{chosen}'"
                ),
            })
        elif fallback_index > 0:
            messages.append({
                "role": "builder",
                "content": (
                    f"fallback: step={step.step_id} picked "
                    f"candidate[{fallback_index}]='{chosen}', "
                    f"skipped {fallback_index} no-detail candidate(s)"
                ),
            })

        candidates.append(NodeCandidate(
            step_id=step.step_id,
            chosen_type=chosen,
            definition=definition,
        ))

    return candidates, messages


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
    settings = get_settings()
    prompt_budget = settings.builder_prompt_char_budget
    t0 = time.monotonic()

    candidates, candidate_messages = _collect_candidates(state.plan, retriever)  # C1-1:B-CAND-02

    # Build the deduplicated definitions list from candidate results for prompt
    seen_types: set[str] = set()
    definitions = []
    for c in candidates:
        if c.definition is not None and c.chosen_type not in seen_types:
            definitions.append(c.definition)
            seen_types.add(c.chosen_type)

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

    if len(prompt) > prompt_budget and defs_payload:
        original_len = len(prompt)
        keep = max(1, len(defs_payload) // 2)
        while len(prompt) > prompt_budget and keep >= 1:
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
            prompt_budget,
        )
    else:
        logger.info("builder prompt len=%d defs=%d", len(prompt), len(defs_payload))

    stage = "fix" if is_retry else "builder"
    llm = get_llm(BuilderOutput, stage=stage)
    try:
        result: BuilderOutput = invoke_with_timeout(llm, prompt)  # type: ignore[assignment]
    except LLMTimeoutError as exc:  # C1-1:B-TIMEOUT-01
        logger.warning("builder LLM timeout (%s): %s", prompt_name, exc)
        raise BuilderTimeoutError(f"stage={stage} cause={exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("builder LLM call failed (%s)", prompt_name)
        return {
            "error": f"building_failed: {exc}",
            "messages": state.messages
            + candidate_messages
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
        + candidate_messages
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
