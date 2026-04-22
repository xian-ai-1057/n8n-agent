"""Unit tests for WorkflowValidator (C1-4).

Each C1-4 rule has at least one positive and one negative fixture.
Fixtures use the wire-format dict shape (R2-1) so we exercise both the
dict path and the pydantic-draft path.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent.validator import WorkflowValidator, validate_workflow
from app.models.enums import ConnectionType
from app.models.workflow import BuiltNode, Connection, WorkflowDraft

# ----------------------------------------------------------------------
# Helpers / fixtures
# ----------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid4())


KNOWN_TYPES: set[str] = {
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.set",
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.httpRequest",
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.if",
    "n8n-nodes-base.slack",
    "n8n-nodes-base.gmail",
}


def _v() -> WorkflowValidator:
    return WorkflowValidator(known_types=set(KNOWN_TYPES))


def _minimal_workflow() -> dict:
    """Manual Trigger -> Set; valid per C1-4 acceptance criteria."""
    mid = _uuid()
    sid = _uuid()
    return {
        "name": "Hello n8n",
        "nodes": [
            {
                "id": mid,
                "name": "Manual",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [240, 300],
                "parameters": {},
            },
            {
                "id": sid,
                "name": "Set",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [460, 300],
                "parameters": {"assignments": {"assignments": []}, "options": {}},
            },
        ],
        "connections": {
            "Manual": {
                "main": [[{"node": "Set", "type": "main", "index": 0}]]
            }
        },
        "settings": {"executionOrder": "v1"},
    }


def _rule_ids(issues) -> set[str]:
    return {i.rule_id for i in issues}


# ----------------------------------------------------------------------
# Baseline: the minimal workflow is valid
# ----------------------------------------------------------------------


def test_minimal_workflow_passes():
    rpt = _v().validate(_minimal_workflow())
    assert rpt.ok is True
    assert rpt.errors == []


# ----------------------------------------------------------------------
# V-TOP rules
# ----------------------------------------------------------------------


def test_vtop001_name_missing():
    wf = _minimal_workflow()
    wf["name"] = ""
    rpt = _v().validate(wf)
    assert rpt.ok is False
    assert "V-TOP-001" in _rule_ids(rpt.errors)


def test_vtop002_nodes_empty():
    wf = _minimal_workflow()
    wf["nodes"] = []
    rpt = _v().validate(wf)
    assert "V-TOP-002" in _rule_ids(rpt.errors)


def test_vtop003_settings_missing_execution_order():
    wf = _minimal_workflow()
    wf["settings"] = {}
    rpt = _v().validate(wf)
    assert "V-TOP-003" in _rule_ids(rpt.errors)


def test_vtop004_execution_order_warn_unknown():
    wf = _minimal_workflow()
    wf["settings"] = {"executionOrder": "weird"}
    rpt = _v().validate(wf)
    assert "V-TOP-004" in _rule_ids(rpt.warnings)
    # still no blocking error from V-TOP
    assert "V-TOP-003" not in _rule_ids(rpt.errors)


def test_vtop005_readonly_fields_warn():
    wf = _minimal_workflow()
    wf["id"] = "srv-generated"
    wf["active"] = True
    rpt = _v().validate(wf)
    assert "V-TOP-005" in _rule_ids(rpt.warnings)


# ----------------------------------------------------------------------
# V-NODE rules
# ----------------------------------------------------------------------


def test_vnode001_missing_required_field():
    wf = _minimal_workflow()
    del wf["nodes"][1]["parameters"]
    rpt = _v().validate(wf)
    assert "V-NODE-001" in _rule_ids(rpt.errors)


def test_vnode002_id_empty_string():
    wf = _minimal_workflow()
    wf["nodes"][0]["id"] = ""
    rpt = _v().validate(wf)
    assert "V-NODE-002" in _rule_ids(rpt.errors)


def test_vnode003_duplicate_name():
    wf = _minimal_workflow()
    wf["nodes"][1]["name"] = "Manual"  # clash with trigger
    # fix connection key so V-CONN-001 doesn't also scream
    wf["connections"] = {"Manual": {"main": [[]]}}
    rpt = _v().validate(wf)
    assert "V-NODE-003" in _rule_ids(rpt.errors)


def test_vnode004_unknown_type():
    wf = _minimal_workflow()
    wf["nodes"][1]["type"] = "n8n-nodes-base.doesNotExist"
    rpt = _v().validate(wf)
    assert "V-NODE-004" in _rule_ids(rpt.errors)


def test_vnode005_type_version_not_number():
    wf = _minimal_workflow()
    wf["nodes"][1]["typeVersion"] = "3.4"  # string, not number
    rpt = _v().validate(wf)
    assert "V-NODE-005" in _rule_ids(rpt.errors)


def test_vnode006_position_malformed():
    wf = _minimal_workflow()
    wf["nodes"][1]["position"] = [1, 2, 3]  # three elements
    rpt = _v().validate(wf)
    assert "V-NODE-006" in _rule_ids(rpt.errors)


def test_vnode007_parameters_not_dict():
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"] = None  # must not be silently coerced
    rpt = _v().validate(wf)
    assert "V-NODE-007" in _rule_ids(rpt.errors)


def test_vnode008_continue_on_fail_warn():
    wf = _minimal_workflow()
    wf["nodes"][1]["continueOnFail"] = True
    rpt = _v().validate(wf)
    assert "V-NODE-008" in _rule_ids(rpt.warnings)
    # still allowed to deploy — not in errors
    assert "V-NODE-008" not in _rule_ids(rpt.errors)


def test_vnode009_duplicate_id():
    wf = _minimal_workflow()
    wf["nodes"][1]["id"] = wf["nodes"][0]["id"]
    rpt = _v().validate(wf)
    assert "V-NODE-009" in _rule_ids(rpt.errors)


# ----------------------------------------------------------------------
# V-CONN rules
# ----------------------------------------------------------------------


def test_vconn001_key_is_id_not_name():
    wf = _minimal_workflow()
    manual_id = wf["nodes"][0]["id"]
    wf["connections"] = {
        manual_id: {"main": [[{"node": "Set", "type": "main", "index": 0}]]}
    }
    rpt = _v().validate(wf)
    ids = _rule_ids(rpt.errors)
    assert "V-CONN-001" in ids


def test_vconn002_target_unknown():
    wf = _minimal_workflow()
    wf["connections"] = {
        "Manual": {"main": [[{"node": "Ghost", "type": "main", "index": 0}]]}
    }
    rpt = _v().validate(wf)
    assert "V-CONN-002" in _rule_ids(rpt.errors)


def test_vconn003_invalid_type():
    wf = _minimal_workflow()
    wf["connections"] = {
        "Manual": {"garbage": [[{"node": "Set", "type": "garbage", "index": 0}]]}
    }
    rpt = _v().validate(wf)
    assert "V-CONN-003" in _rule_ids(rpt.errors)


# ----------------------------------------------------------------------
# V-TRIG rules
# ----------------------------------------------------------------------


def test_vtrig001_no_trigger():
    wf = _minimal_workflow()
    # replace manualTrigger with a non-trigger node
    wf["nodes"][0]["type"] = "n8n-nodes-base.set"
    wf["nodes"][0]["typeVersion"] = 3.4
    rpt = _v().validate(wf)
    assert "V-TRIG-001" in _rule_ids(rpt.errors)


def test_vtrig002_multiple_triggers_warn():
    wf = _minimal_workflow()
    wf["nodes"][1]["type"] = "n8n-nodes-base.scheduleTrigger"
    wf["nodes"][1]["typeVersion"] = 1.2
    rpt = _v().validate(wf)
    assert "V-TRIG-002" in _rule_ids(rpt.warnings)


# ----------------------------------------------------------------------
# Pydantic-draft entry point
# ----------------------------------------------------------------------


def test_pydantic_draft_input_passes():
    draft = WorkflowDraft(
        name="From Pydantic",
        nodes=[
            BuiltNode(
                id=_uuid(),
                name="Manual",
                type="n8n-nodes-base.manualTrigger",
                typeVersion=1,
                position=[240, 300],
                parameters={},
            ),
            BuiltNode(
                id=_uuid(),
                name="Set",
                type="n8n-nodes-base.set",
                typeVersion=3.4,
                position=[460, 300],
                parameters={},
            ),
        ],
        connections=[
            Connection(
                source_name="Manual",
                target_name="Set",
                type=ConnectionType.MAIN,
            )
        ],
    )
    rpt = validate_workflow(draft, known_types=set(KNOWN_TYPES))
    assert rpt.ok is True


def test_validate_none_raises():
    with pytest.raises(TypeError):
        _v().validate(None)


# ----------------------------------------------------------------------
# V-PARAM-009: placeholder detection
# ----------------------------------------------------------------------


def test_v_param_009_detects_todo_in_param():
    """V-PARAM-009: a TODO value in a parameter triggers an error."""
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"]["url"] = "TODO: fill in the URL"
    rpt = _v().validate(wf)
    assert rpt.ok is False
    assert "V-PARAM-009" in _rule_ids(rpt.errors)


def test_v_param_009_detects_nested_placeholder():
    """V-PARAM-009: placeholder in deeply nested parameter is found."""
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"] = {
        "authentication": {"apiKey": "your-api-key"},
    }
    rpt = _v().validate(wf)
    assert rpt.ok is False
    ids = _rule_ids(rpt.errors)
    assert "V-PARAM-009" in ids


def test_v_param_009_skips_n8n_expressions():
    """V-PARAM-009: values that are n8n expressions (={{ }}) are not flagged."""
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"]["url"] = "={{ $json.url }}"
    rpt = _v().validate(wf)
    # V-PARAM-009 must NOT fire for expression values
    assert "V-PARAM-009" not in _rule_ids(rpt.errors)


def test_v_param_009_issue_has_rule_class_and_suggested_fix():
    """V-PARAM-009: ValidationIssue carries rule_class and suggested_fix fields."""
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"]["webhook_url"] = "https://example.com/hook"
    rpt = _v().validate(wf)
    param_009_issues = [i for i in rpt.errors if i.rule_id == "V-PARAM-009"]
    assert param_009_issues, "expected at least one V-PARAM-009 error"
    issue = param_009_issues[0]
    assert issue.rule_class == "parameter_quality"
    assert issue.suggested_fix is not None
    assert len(issue.suggested_fix) > 0


def test_v_param_009_clean_workflow_no_false_positive():
    """V-PARAM-009: minimal workflow with real-looking values passes clean."""
    wf = _minimal_workflow()
    # The minimal workflow uses empty parameters {}, so no placeholders
    rpt = _v().validate(wf)
    assert "V-PARAM-009" not in _rule_ids(rpt.errors)


def test_v_param_009_detects_fill_in_placeholder():
    """V-PARAM-009: <fill_in> angle-bracket placeholder triggers an error."""
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"]["token"] = "<fill_in>"
    rpt = _v().validate(wf)
    assert rpt.ok is False
    assert "V-PARAM-009" in _rule_ids(rpt.errors)


def test_v_param_009_word_boundary_no_false_positive():
    """V-PARAM-009: substrings that contain TODO/XXX as part of a larger word do not trigger."""
    wf = _minimal_workflow()
    wf["nodes"][1]["parameters"]["description"] = "methodology and stodgy"
    rpt = _v().validate(wf)
    assert "V-PARAM-009" not in _rule_ids(rpt.errors)
