"""Live integration test against a running n8n instance (C1-3 acceptance).

Skips at module-import time if `N8N_API_KEY` is not set.
Run explicitly:

    N8N_API_KEY=... N8N_URL=http://localhost:5678 \\
      pytest backend/tests/integration/test_n8n_live.py -m integration
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

if not os.environ.get("N8N_API_KEY"):
    pytest.skip(
        "N8N_API_KEY not set; skipping live n8n tests",
        allow_module_level=True,
    )

from app.models.enums import ConnectionType
from app.models.workflow import BuiltNode, Connection, WorkflowDraft
from app.n8n.client import N8nClient

pytestmark = pytest.mark.integration


def test_create_and_get_minimal_workflow():
    draft = WorkflowDraft(
        name=f"smoke-{uuid4().hex[:8]}",
        nodes=[
            BuiltNode(
                id=str(uuid4()),
                name="Manual",
                type="n8n-nodes-base.manualTrigger",
                typeVersion=1,
                position=[240, 300],
                parameters={},
            ),
            BuiltNode(
                id=str(uuid4()),
                name="Set",
                type="n8n-nodes-base.set",
                typeVersion=3.4,
                position=[460, 300],
                parameters={"assignments": {"assignments": []}, "options": {}},
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

    client = N8nClient()
    try:
        result = client.create_workflow(draft)
        assert result.id
        assert result.url.endswith(f"/workflow/{result.id}")

        fetched = client.get_workflow(result.id)
        assert fetched["name"] == draft.name
        assert any(n.get("name") == "Manual" for n in fetched["nodes"])
    finally:
        client.close()
