"""Workflow Validator (Implements C1-4).

Pure-python, no LLM, no I/O (except optionally reading the node catalog on
construction). Adapted from the archive 569-line validator, trimmed to the
19 rules specified in C1-4 §1. Each rule has a stable `rule_id` so retry
prompt templates can reference them.

Accepts either a `WorkflowDraft` pydantic model or a dict (shape per R2-1).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models.enums import ConnectionType, ValidationSeverity
from ..models.validation import ValidationIssue, ValidationReport

if TYPE_CHECKING:  # pragma: no cover
    from ..models.workflow import WorkflowDraft

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Trigger allowlist per C1-4 §1 (V-TRIG-001).
_TRIGGER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "n8n-nodes-base.webhook",
        "n8n-nodes-base.formTrigger",
        "n8n-nodes-base.emailReadImap",
        "n8n-nodes-base.manualTrigger",
    }
)

_VALID_CONNECTION_TYPES: frozenset[str] = frozenset(e.value for e in ConnectionType)

# Phase 1-B catalog file — best-effort. If missing, V-NODE-004 degrades to warning.
_DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "nodes" / "catalog_discovery.json"
)


class WorkflowValidator:
    """Stateless validator — construct once, call `validate()` many times.

    `known_types` is the registry used by `V-NODE-004`. If not provided the
    validator tries to load `data/nodes/catalog_discovery.json` (Phase 1-B
    artefact). If that file is missing it continues but emits a single
    `V-NODE-004-warn` warning telling the caller the registry is empty.
    """

    def __init__(
        self,
        *,
        known_types: set[str] | None = None,
        catalog_path: Path | None = None,
    ) -> None:
        self._catalog_missing: bool = False
        if known_types is not None:
            self.known_types = set(known_types)
            return

        path = catalog_path or _DEFAULT_CATALOG_PATH
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
            self.known_types = {
                e["type"] for e in entries if isinstance(e, dict) and "type" in e
            }
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("catalog_discovery.json unavailable (%s); type checks will warn", exc)
            self.known_types = set()
            self._catalog_missing = True

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def validate(
        self, draft: "WorkflowDraft | dict[str, Any]"
    ) -> ValidationReport:
        if draft is None:
            raise TypeError("draft must not be None")

        data = _coerce_to_dict(draft)
        issues: list[ValidationIssue] = []
        issues.extend(self._check_top_level(data))
        issues.extend(self._check_nodes(data))
        issues.extend(self._check_connections(data))
        issues.extend(self._check_triggers(data))

        return ValidationReport.from_issues(issues)

    # ------------------------------------------------------------------
    # Top-level rules (V-TOP-xxx)
    # ------------------------------------------------------------------

    def _check_top_level(self, data: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            out.append(
                ValidationIssue(
                    rule_id="V-TOP-001",
                    severity=ValidationSeverity.ERROR,
                    message="workflow name is required",
                    path="name",
                )
            )

        nodes = data.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            out.append(
                ValidationIssue(
                    rule_id="V-TOP-002",
                    severity=ValidationSeverity.ERROR,
                    message="workflow must contain at least one node",
                    path="nodes",
                )
            )

        settings = data.get("settings")
        if not isinstance(settings, dict) or "executionOrder" not in settings:
            out.append(
                ValidationIssue(
                    rule_id="V-TOP-003",
                    severity=ValidationSeverity.ERROR,
                    message="settings.executionOrder is required (use 'v1')",
                    path="settings.executionOrder",
                )
            )
        else:
            order = settings.get("executionOrder")
            if order not in ("v0", "v1"):
                out.append(
                    ValidationIssue(
                        rule_id="V-TOP-004",
                        severity=ValidationSeverity.WARNING,
                        message=f"unknown executionOrder: {order}",
                        path="settings.executionOrder",
                    )
                )

        # Bonus per brief §B(9): no read-only top-level fields should appear.
        from ..n8n.client import READ_ONLY_TOP_LEVEL_FIELDS  # local import to avoid cycle

        present_readonly = [k for k in data.keys() if k in READ_ONLY_TOP_LEVEL_FIELDS]
        if present_readonly:
            for k in present_readonly:
                out.append(
                    ValidationIssue(
                        rule_id="V-TOP-005",
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"read-only field '{k}' present at top level; "
                            "client will strip it before POST"
                        ),
                        path=k,
                    )
                )

        return out

    # ------------------------------------------------------------------
    # Node rules (V-NODE-xxx)
    # ------------------------------------------------------------------

    def _check_nodes(self, data: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        nodes = data.get("nodes")
        if not isinstance(nodes, list):
            return out

        seen_names: dict[str, int] = {}
        seen_ids: dict[str, int] = {}
        catalog_warned = False

        for idx, node in enumerate(nodes):
            if not isinstance(node, dict):
                out.append(
                    ValidationIssue(
                        rule_id="V-NODE-001",
                        severity=ValidationSeverity.ERROR,
                        message=f"node[{idx}] is not an object",
                        path=f"nodes[{idx}]",
                    )
                )
                continue

            required = ("id", "name", "type", "typeVersion", "position", "parameters")
            for field in required:
                if field not in node:
                    out.append(
                        ValidationIssue(
                            rule_id="V-NODE-001",
                            severity=ValidationSeverity.ERROR,
                            message=f"node[{idx}] missing required field: {field}",
                            node_name=node.get("name"),
                            path=f"nodes[{idx}].{field}",
                        )
                    )

            nid = node.get("id")
            name = node.get("name")
            ntype = node.get("type")
            type_version = node.get("typeVersion")
            position = node.get("position")
            params = node.get("parameters")

            # V-NODE-002: id non-empty string (uuid pattern recommended)
            if not isinstance(nid, str) or not nid.strip():
                out.append(
                    ValidationIssue(
                        rule_id="V-NODE-002",
                        severity=ValidationSeverity.ERROR,
                        message=f"node[{idx}].id must be a non-empty string",
                        node_name=name if isinstance(name, str) else None,
                        path=f"nodes[{idx}].id",
                    )
                )
            else:
                if not _UUID_RE.match(nid):
                    out.append(
                        ValidationIssue(
                            rule_id="V-NODE-002W",
                            severity=ValidationSeverity.WARNING,
                            message=(
                                f"node '{name}' id is not a uuid v4 "
                                "(n8n accepts it but uuid is recommended)"
                            ),
                            node_name=name if isinstance(name, str) else None,
                            path=f"nodes[{idx}].id",
                        )
                    )

                # V-NODE-009: id uniqueness
                if nid in seen_ids:
                    out.append(
                        ValidationIssue(
                            rule_id="V-NODE-009",
                            severity=ValidationSeverity.ERROR,
                            message=f"duplicate node id: {nid}",
                            node_name=name if isinstance(name, str) else None,
                            path=f"nodes[{idx}].id",
                        )
                    )
                else:
                    seen_ids[nid] = idx

            # V-NODE-003: name uniqueness
            if isinstance(name, str) and name:
                if name in seen_names:
                    out.append(
                        ValidationIssue(
                            rule_id="V-NODE-003",
                            severity=ValidationSeverity.ERROR,
                            message=f"duplicate node name: {name}",
                            node_name=name,
                            path=f"nodes[{idx}].name",
                        )
                    )
                else:
                    seen_names[name] = idx

            # V-NODE-004: type exists in registry
            if isinstance(ntype, str) and ntype:
                if self._catalog_missing:
                    if not catalog_warned:
                        out.append(
                            ValidationIssue(
                                rule_id="V-NODE-004-warn",
                                severity=ValidationSeverity.WARNING,
                                message=(
                                    "node type registry unavailable "
                                    "(data/nodes/catalog_discovery.json missing); "
                                    "type existence not verified"
                                ),
                                path="nodes",
                            )
                        )
                        catalog_warned = True
                elif self.known_types and ntype not in self.known_types:
                    out.append(
                        ValidationIssue(
                            rule_id="V-NODE-004",
                            severity=ValidationSeverity.ERROR,
                            message=f"unknown node type: {ntype}",
                            node_name=name if isinstance(name, str) else None,
                            path=f"nodes[{idx}].type",
                        )
                    )

            # V-NODE-005: typeVersion is a number
            if "typeVersion" in node and not isinstance(type_version, (int, float)):
                out.append(
                    ValidationIssue(
                        rule_id="V-NODE-005",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"node '{name}' typeVersion must be a number, got "
                            f"{type(type_version).__name__}"
                        ),
                        node_name=name if isinstance(name, str) else None,
                        path=f"nodes[{idx}].typeVersion",
                    )
                )

            # V-NODE-006: position is [x, y] numbers
            if "position" in node:
                ok = (
                    isinstance(position, list)
                    and len(position) == 2
                    and all(isinstance(p, (int, float)) for p in position)
                )
                if not ok:
                    out.append(
                        ValidationIssue(
                            rule_id="V-NODE-006",
                            severity=ValidationSeverity.ERROR,
                            message=f"node '{name}' position must be [x, y] numbers",
                            node_name=name if isinstance(name, str) else None,
                            path=f"nodes[{idx}].position",
                        )
                    )

            # V-NODE-007: parameters is a dict (including None check — do NOT
            # silently coerce; missing is distinct from empty-but-present).
            if "parameters" in node and not isinstance(params, dict):
                out.append(
                    ValidationIssue(
                        rule_id="V-NODE-007",
                        severity=ValidationSeverity.ERROR,
                        message=f"node '{name}' parameters must be an object",
                        node_name=name if isinstance(name, str) else None,
                        path=f"nodes[{idx}].parameters",
                    )
                )

            # V-NODE-008: deprecated continueOnFail
            if node.get("continueOnFail") is True:
                out.append(
                    ValidationIssue(
                        rule_id="V-NODE-008",
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"node '{name}' uses deprecated 'continueOnFail'; "
                            "use 'onError' instead"
                        ),
                        node_name=name if isinstance(name, str) else None,
                        path=f"nodes[{idx}].continueOnFail",
                    )
                )

        return out

    # ------------------------------------------------------------------
    # Connection rules (V-CONN-xxx)
    # ------------------------------------------------------------------

    def _check_connections(self, data: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        nodes = data.get("nodes") or []
        connections = data.get("connections") or {}
        if not isinstance(connections, dict):
            return out

        node_names = {n.get("name") for n in nodes if isinstance(n, dict)}
        node_ids = {n.get("id") for n in nodes if isinstance(n, dict)}

        inbound: set[str] = set()
        outbound_sources: set[str] = set()

        for source_name, conn_types in connections.items():
            # V-CONN-001: key is a known node NAME (not id).
            if source_name not in node_names:
                if source_name in node_ids:
                    out.append(
                        ValidationIssue(
                            rule_id="V-CONN-001",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"connection key '{source_name}' looks like a node id; "
                                "connections must be keyed by node NAME"
                            ),
                            path=f"connections['{source_name}']",
                        )
                    )
                else:
                    out.append(
                        ValidationIssue(
                            rule_id="V-CONN-001",
                            severity=ValidationSeverity.ERROR,
                            message=f"connection key '{source_name}' is not a known node name",
                            path=f"connections['{source_name}']",
                        )
                    )
                continue

            outbound_sources.add(source_name)
            if not isinstance(conn_types, dict):
                continue
            for ct, slots in conn_types.items():
                # V-CONN-003: type belongs to known set
                if ct not in _VALID_CONNECTION_TYPES:
                    out.append(
                        ValidationIssue(
                            rule_id="V-CONN-003",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"connection {source_name}->? has invalid type '{ct}'"
                            ),
                            path=f"connections['{source_name}'].{ct}",
                        )
                    )
                    continue
                if not isinstance(slots, list):
                    continue
                for slot in slots:
                    if not isinstance(slot, list):
                        continue
                    for target in slot:
                        if not isinstance(target, dict):
                            continue
                        tname = target.get("node")
                        # V-CONN-002: target is an existing node name
                        if tname not in node_names:
                            out.append(
                                ValidationIssue(
                                    rule_id="V-CONN-002",
                                    severity=ValidationSeverity.ERROR,
                                    message=(
                                        f"connection {source_name}->{tname} "
                                        "targets unknown node"
                                    ),
                                    path=f"connections['{source_name}']",
                                )
                            )
                        else:
                            inbound.add(tname)

        # V-CONN-004 / V-CONN-005: isolated nodes (warnings only).
        for n in nodes:
            if not isinstance(n, dict):
                continue
            name = n.get("name")
            if not isinstance(name, str):
                continue
            is_trig = _is_trigger(n)
            if not is_trig and name not in inbound:
                out.append(
                    ValidationIssue(
                        rule_id="V-CONN-004",
                        severity=ValidationSeverity.WARNING,
                        message=f"node '{name}' has no incoming connection",
                        node_name=name,
                    )
                )
            if name not in outbound_sources and len(nodes) > 1:
                # Terminal sinks are legal; warn only, do not block.
                out.append(
                    ValidationIssue(
                        rule_id="V-CONN-005",
                        severity=ValidationSeverity.WARNING,
                        message=f"node '{name}' has no outgoing connection",
                        node_name=name,
                    )
                )

        return out

    # ------------------------------------------------------------------
    # Trigger rules (V-TRIG-xxx)
    # ------------------------------------------------------------------

    def _check_triggers(self, data: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        nodes = data.get("nodes") or []
        triggers = [n for n in nodes if isinstance(n, dict) and _is_trigger(n)]

        if not triggers:
            out.append(
                ValidationIssue(
                    rule_id="V-TRIG-001",
                    severity=ValidationSeverity.ERROR,
                    message="workflow must contain at least one trigger node",
                    path="nodes",
                )
            )
        elif len(triggers) > 1:
            out.append(
                ValidationIssue(
                    rule_id="V-TRIG-002",
                    severity=ValidationSeverity.WARNING,
                    message=f"workflow has {len(triggers)} trigger nodes",
                )
            )

        return out


# ======================================================================
# Module-level helpers
# ======================================================================


def _coerce_to_dict(draft: "WorkflowDraft | dict[str, Any]") -> dict[str, Any]:
    """Convert a pydantic WorkflowDraft (if given) to the raw n8n dict shape.

    Uses aliases so `typeVersion`, `onError`, etc. round-trip correctly.
    The validator then operates on keys in the R2-1 wire format.
    """
    if hasattr(draft, "model_dump"):
        data = draft.model_dump(by_alias=True, exclude_none=True)
        # Convert the draft's list-of-Connection into the n8n map form so
        # V-CONN rules can operate on the wire shape.
        conns = data.get("connections")
        if isinstance(conns, list):
            from ..n8n.client import _connections_list_to_map  # local import

            data["connections"] = _connections_list_to_map(conns)
        return data
    if isinstance(draft, dict):
        return draft
    raise TypeError(f"validate() requires WorkflowDraft or dict, got {type(draft)}")


def _is_trigger(node: dict[str, Any]) -> bool:
    ntype = node.get("type")
    if not isinstance(ntype, str):
        return False
    if ntype in _TRIGGER_ALLOWLIST:
        return True
    # suffix-match: anything ending in 'Trigger' (e.g. scheduleTrigger)
    # case-insensitively; n8n official node ids are camelCase after the dot.
    tail = ntype.rsplit(".", 1)[-1]
    return tail.endswith("Trigger") or tail.lower().endswith("trigger")


def validate_workflow(
    draft: "WorkflowDraft | dict[str, Any]",
    *,
    known_types: set[str] | None = None,
) -> ValidationReport:
    """Functional alias per C1-4 §2 signature."""
    return WorkflowValidator(known_types=known_types).validate(draft)
