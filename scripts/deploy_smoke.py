"""Smoke-test the live n8n deploy path (Phase 1-C acceptance).

Builds the minimal Manual Trigger -> Set workflow from R2-1 §7 and POSTs it
to the running n8n instance via `N8nClient`. Prints the editor URL so the
user can open it to confirm.

Preconditions:
    1. `docker compose up -d` from project root.
    2. n8n UI at http://localhost:5678 — finish first-run account creation.
    3. `Settings -> n8n API -> Create API key`; paste into `.env` as N8N_API_KEY.

Usage:
    python scripts/deploy_smoke.py

Exits non-zero if the API key is missing or the POST fails.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.models.enums import ConnectionType  # noqa: E402
from app.models.workflow import BuiltNode, Connection, WorkflowDraft  # noqa: E402
from app.n8n.client import N8nClient  # noqa: E402


def build_minimal_draft() -> WorkflowDraft:
    return WorkflowDraft(
        name=f"smoke-{uuid4().hex[:8]}",
        nodes=[
            BuiltNode(
                id=str(uuid4()),
                name="When clicking 'Execute Workflow'",
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
                parameters={
                    "assignments": {
                        "assignments": [
                            {
                                "id": "a1",
                                "name": "greeting",
                                "value": "hello",
                                "type": "string",
                            }
                        ]
                    },
                    "options": {},
                },
            ),
        ],
        connections=[
            Connection(
                source_name="When clicking 'Execute Workflow'",
                target_name="Set",
                type=ConnectionType.MAIN,
            )
        ],
    )


def main() -> int:
    settings = get_settings()
    if not settings.n8n_api_key:
        print(
            "ERROR: N8N_API_KEY is not set. Create it in the n8n UI "
            "(Settings -> n8n API) and add it to .env.",
            file=sys.stderr,
        )
        return 2

    client = N8nClient()
    try:
        draft = build_minimal_draft()
        print(f"POST {settings.n8n_url}/api/v1/workflows  name={draft.name}")
        result = client.create_workflow(draft)
        print(f"OK  id={result.id}")
        print(f"URL {result.url}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- CLI surface
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
