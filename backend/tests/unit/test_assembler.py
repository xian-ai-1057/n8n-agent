"""Unit tests for the pure-code Assembler (Phase 2-B)."""

from __future__ import annotations

import re
from uuid import UUID

import pytest

from app.agent.assembler import (
    X_START,
    X_STEP,
    Y_BRANCH_OFFSET,
    Y_MAIN,
    assemble_workflow,
)
from app.models.enums import ConnectionType
from app.models.workflow import BuiltNode, Connection


def _node(
    name: str,
    type_: str = "n8n-nodes-base.set",
    *,
    node_id: str = "",
    type_version: float = 1.0,
    position: tuple[float, float] = (0.0, 0.0),
) -> BuiltNode:
    # Bypass uuid default by supplying explicit (possibly empty) id — the
    # assembler should regenerate when it doesn't look like a uuid.
    kwargs = {
        "name": name,
        "type": type_,
        "type_version": type_version,
        "position": list(position),
    }
    if node_id:
        kwargs["id"] = node_id
    return BuiltNode(**kwargs)


# --------------------------------------------------------------------------
# 1. Linear layout: x starts at -100, steps by 220; y stays on 300.
# --------------------------------------------------------------------------
def test_linear_layout_positions():
    nodes = [
        _node("Manual", "n8n-nodes-base.manualTrigger"),
        _node("Set", "n8n-nodes-base.set"),
        _node("HTTP", "n8n-nodes-base.httpRequest"),
    ]
    conns = [
        Connection(source_name="Manual", target_name="Set"),
        Connection(source_name="Set", target_name="HTTP"),
    ]
    draft = assemble_workflow(
        built_nodes=nodes, connections=conns, user_message="linear"
    )
    assert [n.position for n in draft.nodes] == [
        [X_START + 0 * X_STEP, Y_MAIN],
        [X_START + 1 * X_STEP, Y_MAIN],
        [X_START + 2 * X_STEP, Y_MAIN],
    ]


# --------------------------------------------------------------------------
# 2. Branch layout: slot 0 → y-offset above, slot 1 → below.
# --------------------------------------------------------------------------
def test_branch_layout_offsets_y():
    nodes = [
        _node("Webhook", "n8n-nodes-base.webhook"),
        _node("If", "n8n-nodes-base.if"),
        _node("Slack", "n8n-nodes-base.slack"),
        _node("Gmail", "n8n-nodes-base.gmail"),
    ]
    conns = [
        Connection(source_name="Webhook", target_name="If"),
        Connection(source_name="If", source_output_index=0, target_name="Slack"),
        Connection(source_name="If", source_output_index=1, target_name="Gmail"),
    ]
    draft = assemble_workflow(
        built_nodes=nodes, connections=conns, user_message="branch demo"
    )
    by_name = {n.name: n for n in draft.nodes}
    assert by_name["Webhook"].position[1] == Y_MAIN
    assert by_name["If"].position[1] == Y_MAIN
    assert by_name["Slack"].position[1] == Y_MAIN - Y_BRANCH_OFFSET
    assert by_name["Gmail"].position[1] == Y_MAIN + Y_BRANCH_OFFSET


# --------------------------------------------------------------------------
# 3. UUID generation when node lacks one.
# --------------------------------------------------------------------------
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def test_missing_uuid_is_regenerated():
    # Supply a clearly-not-uuid id and check the assembler rewrote it.
    n = _node("N", "n8n-nodes-base.set", node_id="abc")
    draft = assemble_workflow(
        built_nodes=[n], connections=[], user_message="uuid"
    )
    new_id = draft.nodes[0].id
    assert _UUID_RE.match(new_id), f"not a uuid: {new_id}"
    # parseable as UUID too
    UUID(new_id)


def test_existing_uuid_is_preserved():
    good = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    n = _node("N", "n8n-nodes-base.set", node_id=good)
    draft = assemble_workflow(
        built_nodes=[n], connections=[], user_message="keep"
    )
    assert draft.nodes[0].id == good


# --------------------------------------------------------------------------
# 4. settings default = executionOrder v1
# --------------------------------------------------------------------------
def test_settings_default():
    n = _node("Manual", "n8n-nodes-base.manualTrigger")
    draft = assemble_workflow(
        built_nodes=[n], connections=[], user_message="hi"
    )
    assert draft.settings == {"executionOrder": "v1"}


# --------------------------------------------------------------------------
# 5. Workflow name derived from first 30 chars of user_message.
# --------------------------------------------------------------------------
def test_workflow_name_derived_from_user_message():
    long = "每小時抓 https://api.github.com/zen 存到 Google Sheet 之類的描述超長"
    n = _node("Manual", "n8n-nodes-base.manualTrigger")
    draft = assemble_workflow(
        built_nodes=[n], connections=[], user_message=long
    )
    assert 0 < len(draft.name) <= 30
    assert draft.name == long[:30]


def test_workflow_name_override_wins():
    n = _node("Manual", "n8n-nodes-base.manualTrigger")
    draft = assemble_workflow(
        built_nodes=[n],
        connections=[],
        user_message="ignored",
        workflow_name="Custom Name",
    )
    assert draft.name == "Custom Name"


# --------------------------------------------------------------------------
# 6. Connection shape: draft keeps list form; client conversion maps by NAME.
# --------------------------------------------------------------------------
def test_connections_list_to_map_keys_on_name():
    from app.n8n.client import _connections_list_to_map

    conns = [
        Connection(
            source_name="Manual",
            source_output_index=0,
            target_name="Set",
            target_input_index=0,
            type=ConnectionType.MAIN,
        )
    ]
    m = _connections_list_to_map(conns)
    assert "Manual" in m
    assert m["Manual"]["main"] == [[{"node": "Set", "type": "main", "index": 0}]]


def test_empty_input_produces_draft_with_default_name():
    draft = assemble_workflow(
        built_nodes=[],
        connections=[],
        user_message="",
    )
    assert draft.name == "n8n workflow"
    assert draft.nodes == []
    assert draft.connections == []
    assert draft.settings["executionOrder"] == "v1"


def test_does_not_mutate_input_nodes():
    # Assembler should copy nodes, leaving caller's list untouched.
    nodes = [_node("Manual", "n8n-nodes-base.manualTrigger")]
    original_position = list(nodes[0].position)
    assemble_workflow(built_nodes=nodes, connections=[], user_message="nomut")
    assert nodes[0].position == original_position


@pytest.mark.parametrize(
    "i,expected_x",
    [(0, X_START), (1, X_START + X_STEP), (4, X_START + 4 * X_STEP)],
)
def test_x_stride(i: int, expected_x: float):
    nodes = [
        _node(f"N{j}", "n8n-nodes-base.set") for j in range(i + 1)
    ]
    draft = assemble_workflow(
        built_nodes=nodes, connections=[], user_message="stride"
    )
    assert draft.nodes[i].position[0] == expected_x
