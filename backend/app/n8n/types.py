"""n8n client return types.

`WorkflowDeployResult` lives here (not in `app.models.workflow`) per C1-3 — it
is a client-side result object, not part of the draft model SSOT. Phase 1-B's
`workflow.py` does not define it, so this module is the canonical home.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkflowDeployResult(BaseModel):
    """Return value of `N8nClient.create_workflow` (Implements C1-3 §4)."""

    id: str = Field(..., description="Server-assigned workflow id.")
    url: str = Field(..., description="Deep link to the editor: '{N8N_URL}/workflow/{id}'.")
    name: str = Field(..., description="Echo of the workflow name accepted by the server.")
