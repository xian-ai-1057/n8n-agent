# C1-5:A-RESP-01 - _state_to_response populates plan from AgentState
# C1-5:A-WEB-01  - create_app() mounts frontend/web/ at /app

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.agent_state import AgentState
from app.models.enums import StepIntent
from app.models.planning import StepPlan
from app.api.routes import _state_to_response
from app.config import get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEB_DIR = Path(__file__).resolve().parents[3] / "frontend" / "web"

_STEP_1 = StepPlan(
    step_id="s1",
    description="trigger",
    intent=StepIntent.TRIGGER,
    candidate_node_types=["n8n-nodes-base.manualTrigger"],
    reason="test step 1",
)

_STEP_2 = StepPlan(
    step_id="s2",
    description="action",
    intent=StepIntent.ACTION,
    candidate_node_types=["n8n-nodes-base.httpRequest"],
    reason="test step 2",
)


# ---------------------------------------------------------------------------
# A-RESP-01: _state_to_response plan population
# ---------------------------------------------------------------------------


def test_a_resp_01_state_to_response_populates_plan() -> None:
    """State with 2 StepPlan items must produce response.plan as a list of 2 dicts."""
    state = AgentState(
        user_message="test",
        plan=[_STEP_1, _STEP_2],
    )
    settings = get_settings()
    response = _state_to_response(state, settings)

    assert response.plan is not None
    assert len(response.plan) == 2
    # Each element is a plain dict (model_dump result)
    for item in response.plan:
        assert isinstance(item, dict)
        assert "step_id" in item

    step_ids = {item["step_id"] for item in response.plan}
    assert step_ids == {"s1", "s2"}


def test_a_resp_01_state_to_response_empty_plan_gives_empty_list() -> None:
    """State with plan=[] must produce response.plan == [] (never null — spec A-RESP-01)."""
    state = AgentState(
        user_message="test",
        plan=[],
    )
    settings = get_settings()
    response = _state_to_response(state, settings)

    assert response.plan == []


def test_a_resp_01_plan_dicts_have_required_keys() -> None:
    """Serialized StepPlan dicts must include all expected keys."""
    state = AgentState(
        user_message="test",
        plan=[_STEP_1],
    )
    settings = get_settings()
    response = _state_to_response(state, settings)

    assert response.plan is not None
    item = response.plan[0]
    for key in ("step_id", "description", "intent", "candidate_node_types", "reason"):
        assert key in item, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# A-WEB-01: static files mounted at /app
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not WEB_DIR.is_dir(), reason="frontend/web not present")
def test_a_web_01_app_mounts_static_at_app() -> None:
    """GET /app/ must return 200 or a redirect when frontend/web/ exists."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/app/")
    assert r.status_code in (200, 301, 307), (
        f"Expected 200/301/307 from /app/, got {r.status_code}"
    )
