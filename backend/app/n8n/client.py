"""n8n Public API client (Implements C1-3).

Wraps n8n `POST /api/v1/workflows` and related endpoints. Key responsibilities
beyond plain HTTP:

1. Strip read-only top-level fields (R2-1 §Read-only) before POST.
2. Default `settings={"executionOrder":"v1"}` when empty.
3. Migrate deprecated `continueOnFail: true` on nodes to
   `onError: "continueRegularOutput"` (archive mapping; also logs a WARN).
4. Map HTTP status to typed exceptions from `errors.py`.

Accepts either a `WorkflowDraft` pydantic model or a plain dict payload so it
is usable from both the agent graph and bare scripts (`scripts/deploy_smoke.py`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from ..config import get_settings
from .errors import (
    N8nApiError,
    N8nAuthError,
    N8nBadRequestError,
    N8nNotFoundError,
    N8nServerError,
    N8nUnavailable,
)
from .types import WorkflowDeployResult

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from ..models.workflow import Connection, WorkflowDraft

logger = logging.getLogger(__name__)

# R2-1 §Read-only — n8n rejects (or silently ignores) these on create.
READ_ONLY_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "active",
        "createdAt",
        "updatedAt",
        "isArchived",
        "versionId",
        "triggerCount",
        "shared",
        "activeVersion",
        "tags",  # n8n Public API 1.1.0 rejects tags on create (use dedicated endpoint)
        "pinData",
    }
)

# Node-level keys that are safe to forward. Anything else is dropped from node
# dicts (including the deprecated `continueOnFail`).
_ALLOWED_NODE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "type",
        "typeVersion",
        "position",
        "parameters",
        "credentials",
        "disabled",
        "onError",
        "executeOnce",
        "retryOnFail",
        "notes",
        "notesInFlow",
    }
)

_DEFAULT_SETTINGS: dict[str, Any] = {"executionOrder": "v1"}


class N8nClient:
    """Synchronous wrapper around `httpx.Client` for the n8n Public API.

    Typical construction uses `get_settings()` so callers only need `N8nClient()`.
    Tests inject a custom `transport=httpx.MockTransport(...)` to assert request
    shape without hitting the network.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url: str = (base_url or settings.n8n_url).rstrip("/")
        self._api_key: str = api_key if api_key is not None else settings.n8n_api_key

        headers = {
            "X-N8N-API-KEY": self._api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }
        self._client = httpx.Client(
            base_url=f"{self._base_url}/api/v1",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # context manager sugar
    # ------------------------------------------------------------------

    def __enter__(self) -> N8nClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_workflow(
        self, draft: "WorkflowDraft | dict[str, Any]"
    ) -> WorkflowDeployResult:
        """POST a workflow draft; return id + editor URL."""
        payload = self._draft_to_payload(draft)
        data = self._request("POST", "/workflows", json=payload)
        wf_id = str(data["id"])
        return WorkflowDeployResult(
            id=wf_id,
            url=f"{self._base_url}/workflow/{wf_id}",
            name=data.get("name", payload["name"]),
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}")

    def list_workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self._request("GET", "/workflows", params={"limit": limit})
        # n8n wraps in {"data": [...], "nextCursor": ...}
        if isinstance(data, dict) and "data" in data:
            return list(data["data"])
        if isinstance(data, list):
            return data
        return []

    def activate_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{workflow_id}/activate")

    def delete_workflow(self, workflow_id: str) -> None:
        self._request("DELETE", f"/workflows/{workflow_id}")

    def health(self) -> bool:
        """True if auth + network path to n8n are good."""
        try:
            self._request("GET", "/workflows", params={"limit": 1})
            return True
        except N8nApiError:
            return False

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _draft_to_payload(
        self, draft: "WorkflowDraft | dict[str, Any]"
    ) -> dict[str, Any]:
        """Convert a `WorkflowDraft` or dict into n8n POST body.

        Accepts dicts for test / script convenience. For dicts we still:
          - strip read-only top-level fields;
          - default `settings`;
          - sanitise each node (allowed keys only + `continueOnFail` migration).
        """
        if hasattr(draft, "model_dump"):
            # Pydantic WorkflowDraft — use alias so typeVersion/onError are correct.
            raw = draft.model_dump(by_alias=True, exclude_none=True)
            # `connections` on the draft is a list[Connection]; convert to n8n map.
            conns = getattr(draft, "connections", [])
            raw["connections"] = _connections_list_to_map(conns)
        else:
            raw = dict(draft)  # shallow copy

        # 1. Strip read-only top-level fields.
        for key in list(raw.keys()):
            if key in READ_ONLY_TOP_LEVEL_FIELDS:
                logger.debug("stripping read-only top-level field: %s", key)
                raw.pop(key, None)

        # 2. Default settings.
        settings = raw.get("settings")
        if not settings or not isinstance(settings, dict):
            settings = dict(_DEFAULT_SETTINGS)
        else:
            settings = dict(settings)
            settings.setdefault("executionOrder", "v1")
        raw["settings"] = settings

        # 3. Sanitise nodes.
        nodes = raw.get("nodes") or []
        raw["nodes"] = [self._sanitise_node(n) for n in nodes]

        # 4. Ensure connections map shape (the WorkflowDraft path already did).
        if "connections" not in raw or raw["connections"] is None:
            raw["connections"] = {}

        # 5. Ensure name present; n8n requires it.
        if "name" not in raw or not raw["name"]:
            raise ValueError("workflow draft is missing 'name'")

        # n8n POST body has strict additionalProperties=false. Keep only the
        # four top-level fields documented in R2-1.
        allowed_top = {"name", "nodes", "connections", "settings"}
        for key in list(raw.keys()):
            if key not in allowed_top:
                logger.debug("dropping unexpected top-level field: %s", key)
                raw.pop(key, None)

        return raw

    def _sanitise_node(self, node: dict[str, Any]) -> dict[str, Any]:
        """Drop disallowed keys; migrate `continueOnFail` → `onError`."""
        out: dict[str, Any] = {}
        # Migrate deprecated continueOnFail first (so it still applies if caller
        # supplied both — the explicit onError wins if already set).
        if node.get("continueOnFail"):
            mapped = "continueRegularOutput"
            if not node.get("onError"):
                logger.warning(
                    "node %r uses deprecated continueOnFail; mapping to onError=%s",
                    node.get("name"),
                    mapped,
                )
                out["onError"] = mapped
            else:
                logger.warning(
                    "node %r has both continueOnFail and onError; dropping continueOnFail",
                    node.get("name"),
                )

        for key, value in node.items():
            if key == "continueOnFail":
                continue  # already handled
            if key not in _ALLOWED_NODE_KEYS:
                logger.debug("dropping node field %s on node %r", key, node.get("name"))
                continue
            if value is None:
                continue
            out[key] = value

        # n8n requires an `id` on each node; preserve whatever caller gave us
        # (empty strings included — the caller likely has a reason; but we
        # warn since n8n will reject an empty id).
        if "id" not in out:
            raise ValueError(
                f"node {node.get('name')!r} missing required 'id' (expect uuid v4)"
            )
        return out

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise N8nUnavailable(f"n8n timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise N8nUnavailable(f"n8n unreachable: {exc}") from exc

        status = response.status_code
        if 200 <= status < 300:
            if status == 204 or not response.content:
                return {}
            return response.json()

        # Error branches.
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        detail = _extract_message(payload) or response.text[:500]

        if status == 400:
            raise N8nBadRequestError(
                f"n8n 400: {detail}", detail=detail, payload=payload
            )
        if status in (401, 403):
            raise N8nAuthError(
                f"n8n {status}: {detail}", status_code=status, payload=payload
            )
        if status == 404:
            raise N8nNotFoundError(
                f"n8n 404: {detail}", status_code=404, payload=payload
            )
        if 500 <= status < 600:
            raise N8nServerError(
                f"n8n {status}: {detail}", status_code=status, payload=payload
            )
        raise N8nApiError(
            f"n8n {status}: {detail}", status_code=status, payload=payload
        )


# ======================================================================
# Helpers
# ======================================================================


def _connections_list_to_map(
    conns: "list[Connection] | list[dict[str, Any]]",
) -> dict[str, Any]:
    """Convert a flat list of `Connection` records into n8n's nested map.

    Matches R2-1 §3: `{source_name: {type: [[{node,type,index}, ...], ...]}}`.
    Outer list is source output index, inner list is fan-out.
    """
    out: dict[str, dict[str, list[list[dict[str, Any]]]]] = {}
    for c in conns:
        if hasattr(c, "model_dump"):
            d = c.model_dump()
        else:
            d = dict(c)
        src = d["source_name"]
        ct = d.get("type", "main")
        if hasattr(ct, "value"):  # enum
            ct = ct.value
        out_idx = int(d.get("source_output_index", 0))
        slots = out.setdefault(src, {}).setdefault(ct, [])
        while len(slots) <= out_idx:
            slots.append([])
        slots[out_idx].append(
            {
                "node": d["target_name"],
                "type": ct,
                "index": int(d.get("target_input_index", 0)),
            }
        )
    return out


def _extract_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
    if isinstance(payload, str):
        return payload[:500]
    return ""
