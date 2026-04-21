"""Assembler node (Implements C1-1 §2.3).

Pure code: no LLM. Takes BuiltNode[] + Connection[] from Builder and produces a
deployable WorkflowDraft. Responsibilities:

1. Ensure each node has a uuid4 `id`.
2. Lay out positions left-to-right (x = -100 + 220*i, y = 300) and offset
   branches (+/- 200 on y) for fan-out after an `if`/`switch`.
3. Derive workflow name from user_message (first 30 chars, trimmed) unless an
   explicit override is supplied.
4. settings = {"executionOrder": "v1"}.

Connection list → n8n nested map conversion stays in `n8n.client` (there is
already `_connections_list_to_map`); Assembler keeps the list form in the
draft and the client converts on POST.
"""

from __future__ import annotations

import logging
from typing import Iterable
from uuid import uuid4

from ..models.agent_state import AgentState
from ..models.workflow import BuiltNode, Connection, WorkflowDraft

logger = logging.getLogger(__name__)

X_START: float = -100.0
X_STEP: float = 220.0
Y_MAIN: float = 300.0
Y_BRANCH_OFFSET: float = 200.0

_BRANCH_TYPE_SUFFIXES = ("if", "switch")


def _is_branch(node: BuiltNode) -> bool:
    tail = node.type.rsplit(".", 1)[-1].lower()
    return tail in _BRANCH_TYPE_SUFFIXES


def _uuid4_str() -> str:
    return str(uuid4())


def _assign_positions(nodes: list[BuiltNode], connections: list[Connection]) -> None:
    """Mutate nodes in-place to lay them out left-to-right.

    - Base line: x = X_START + i*X_STEP, y = Y_MAIN.
    - After a branch node (`if`/`switch`), the first two fan-out children get
      y = Y_MAIN - Y_BRANCH_OFFSET (true branch) and y = Y_MAIN + Y_BRANCH_OFFSET
      (false branch) respectively; deeper descendants inherit their parent's y.
    """
    if not nodes:
        return

    name_to_node = {n.name: n for n in nodes}
    branch_parents = {n.name for n in nodes if _is_branch(n)}

    # For each branch parent, map its targets (by slot index) to y-offsets.
    y_override: dict[str, float] = {}
    for c in connections:
        if c.source_name not in branch_parents:
            continue
        offset = -Y_BRANCH_OFFSET if c.source_output_index == 0 else Y_BRANCH_OFFSET
        # Later overrides wins only if not already set (first assignment wins).
        y_override.setdefault(c.target_name, Y_MAIN + offset)

    # Propagate y from branch-target descendants: any node that only has one
    # incoming edge from a node we've placed inherits its parent's y.
    # (Simple best-effort; deep branch trees aren't MVP scope.)
    incoming: dict[str, list[str]] = {n.name: [] for n in nodes}
    for c in connections:
        if c.target_name in incoming:
            incoming[c.target_name].append(c.source_name)

    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n.name in y_override:
                continue
            sources = incoming.get(n.name, [])
            if len(sources) == 1 and sources[0] in y_override:
                y_override[n.name] = y_override[sources[0]]
                changed = True

    for i, node in enumerate(nodes):
        x = X_START + X_STEP * i
        y = y_override.get(node.name, Y_MAIN)
        node.position = [x, y]


def _ensure_uuid_ids(nodes: Iterable[BuiltNode]) -> None:
    for node in nodes:
        if not node.id or not _looks_like_uuid(node.id):
            node.id = _uuid4_str()


def _looks_like_uuid(value: str) -> bool:
    import re as _re

    return bool(
        _re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            value,
        )
    )


def _derive_workflow_name(user_message: str) -> str:
    cleaned = (user_message or "").strip().replace("\n", " ").replace("\r", " ")
    if not cleaned:
        return "n8n workflow"
    return cleaned[:30]


def assemble_workflow(
    *,
    built_nodes: list[BuiltNode],
    connections: list[Connection],
    user_message: str,
    workflow_name: str | None = None,
) -> WorkflowDraft:
    """Pure functional assembler — used by tests and the graph node alike."""
    # Make copies to avoid mutating the caller's list.
    nodes = [n.model_copy(deep=True) for n in built_nodes]
    conns = [c.model_copy(deep=True) for c in connections]

    _ensure_uuid_ids(nodes)
    _assign_positions(nodes, conns)

    name = workflow_name or _derive_workflow_name(user_message)

    draft = WorkflowDraft(
        name=name,
        nodes=nodes,
        connections=conns,
        settings={"executionOrder": "v1"},
    )
    return draft


def assemble_step(state: AgentState) -> dict:
    """LangGraph node wrapper."""
    draft = assemble_workflow(
        built_nodes=state.built_nodes,
        connections=state.connections,
        user_message=state.user_message,
    )
    return {"draft": draft}
