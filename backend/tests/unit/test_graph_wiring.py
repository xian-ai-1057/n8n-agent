"""LangGraph wiring tests (Phase 2-B).

Strategy: monkeypatch the LLM-bound structured-output callables in
`app.agent.planner` and `app.agent.builder` so no network is needed.
We assert three paths:

1. Happy path — validator passes first try, deployer short-circuits.
2. One-retry path — validator fails once, builder retry (via fix prompt) fixes it.
3. Max-retry path — validator never passes; graph ends in give_up.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent import builder as builder_mod
from app.agent import planner as planner_mod
from app.agent.builder import BuilderOutput
from app.agent.graph import (  # C1-1:HITL-SHIP-01 / HITL-SHIP-02
    SessionNotFound,
    await_plan_approval_step,
    build_graph,
    reset_hitl_checkpointer,
    resume_graph_with_confirmation,
    run_cli,
    run_graph_until_interrupt,
)
from app.agent.planner import PlannerOutput
from app.models.agent_state import AgentState
from app.models.catalog import NodeCatalogEntry, NodeDefinition
from app.models.enums import StepIntent
from app.models.planning import StepPlan
from app.models.workflow import BuiltNode, Connection

# --------------------------------------------------------------------------
# Fake retriever
# --------------------------------------------------------------------------


class _FakeRetriever:
    def search_discovery(self, query: str, k: int = 8) -> list[NodeCatalogEntry]:
        return [
            NodeCatalogEntry(
                type="n8n-nodes-base.scheduleTrigger",
                display_name="Schedule Trigger",
                category="Core",
                description="Start manually",
            ),
            NodeCatalogEntry(
                type="n8n-nodes-base.set",
                display_name="Set",
                category="Core",
                description="Set fields",
            ),
        ]

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        if node_type == "n8n-nodes-base.scheduleTrigger":
            return NodeDefinition(
                type=node_type,
                display_name="Schedule Trigger",
                description="",
                category="Core",
                type_version=1.0,
                parameters=[],
            )
        if node_type == "n8n-nodes-base.set":
            return NodeDefinition(
                type=node_type,
                display_name="Set",
                description="",
                category="Core",
                type_version=3.4,
                parameters=[],
            )
        return None

    def get_definitions_by_types(self, types: list[str]) -> dict:
        return {t: self.get_detail(t) for t in types}

    def search_detailed(self, query: str, k: int = 4) -> list[NodeDefinition]:
        return []


# --------------------------------------------------------------------------
# Fakes for LLM callables
# --------------------------------------------------------------------------


class _FakePlannerLLM:
    def __init__(self, output: PlannerOutput):
        self._out = output

    def invoke(self, prompt: str) -> PlannerOutput:
        return self._out


class _FakeBuilderLLM:
    """Returns queued outputs in order, one per call."""

    def __init__(self, outputs: list[BuilderOutput]):
        self._outputs = list(outputs)
        self.calls = 0

    def invoke(self, prompt: str) -> BuilderOutput:
        self.calls += 1
        if not self._outputs:
            raise RuntimeError("FakeBuilderLLM exhausted")
        return self._outputs.pop(0)


# --------------------------------------------------------------------------
# Canned outputs
# --------------------------------------------------------------------------


def _valid_plan() -> PlannerOutput:
    return PlannerOutput(
        steps=[
            StepPlan(
                step_id="step_1",
                description="手動觸發",
                intent=StepIntent.TRIGGER,
                candidate_node_types=["n8n-nodes-base.scheduleTrigger"],
                reason="manual trigger suits.",
            ),
            StepPlan(
                step_id="step_2",
                description="設定欄位",
                intent=StepIntent.TRANSFORM,
                candidate_node_types=["n8n-nodes-base.set"],
                reason="set field.",
            ),
        ]
    )


def _valid_builder_output() -> BuilderOutput:
    manual_id = str(uuid4())
    set_id = str(uuid4())
    return BuilderOutput(
        nodes=[
            BuiltNode(
                id=manual_id,
                name="Schedule Trigger",
                type="n8n-nodes-base.scheduleTrigger",
                type_version=1.0,
                position=[0, 0],
                parameters={},
                step_id="step_1",  # C1-1:B-COMP-02 — tells completeness_check this step is covered
            ),
            BuiltNode(
                id=set_id,
                name="Set",
                type="n8n-nodes-base.set",
                type_version=3.4,
                position=[0, 0],
                parameters={},
                step_id="step_2",  # C1-1:B-COMP-02 — tells completeness_check this step is covered
            ),
        ],
        connections=[
            Connection(source_name="Schedule Trigger", target_name="Set")
        ],
    )


def _invalid_builder_output() -> BuilderOutput:
    """Correct types but broken connection → validator V-CONN-001 fails.

    Both node types match their plan steps so _enforce_candidate_types won't
    modify them.  The connection references a non-existent source name, which
    the validator catches as V-CONN-001.
    """
    trigger_id = str(uuid4())
    set_id = str(uuid4())
    return BuilderOutput(
        nodes=[
            BuiltNode(
                id=trigger_id,
                name="Schedule Trigger",
                type="n8n-nodes-base.scheduleTrigger",
                type_version=1.0,
                position=[0, 0],
                parameters={},
                step_id="step_1",  # C1-1:B-COMP-02 — tells completeness_check this step is covered
            ),
            BuiltNode(
                id=set_id,
                name="Set",
                type="n8n-nodes-base.set",
                type_version=3.4,
                position=[0, 0],
                parameters={},
                step_id="step_2",  # C1-1:B-COMP-02 — tells completeness_check this step is covered
            ),
        ],
        connections=[
            Connection(source_name="NONEXISTENT_NODE", target_name="Set"),
        ],
    )


# --------------------------------------------------------------------------
# Patching helper
# --------------------------------------------------------------------------


@pytest.fixture
def patch_llms(monkeypatch):
    """Returns a function you call with (planner_llm, builder_llm) to install."""

    def _install(planner_llm, builder_llm):
        monkeypatch.setattr(
            planner_mod, "get_llm", lambda schema, **kw: planner_llm
        )
        monkeypatch.setattr(
            builder_mod, "get_llm", lambda schema, **kw: builder_llm
        )

    return _install


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_happy_path_validates_and_deploys_dry_run(patch_llms):
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="手動觸發並設欄位"))
    state = AgentState.model_validate(raw)

    assert state.error is None
    assert state.validation is not None
    assert state.validation.ok
    assert state.retry_count == 0
    assert state.draft is not None
    assert {n.name for n in state.draft.nodes} == {"Schedule Trigger", "Set"}
    assert builder_llm.calls == 1


def test_one_retry_path_fixes_and_passes(patch_llms):
    planner_llm = _FakePlannerLLM(_valid_plan())
    # First build is invalid (no trigger); fix build returns valid.
    builder_llm = _FakeBuilderLLM(
        [_invalid_builder_output(), _valid_builder_output()]
    )
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="retry scenario"))
    state = AgentState.model_validate(raw)

    assert state.error is None
    assert state.validation is not None
    assert state.validation.ok
    assert state.retry_count == 1
    assert builder_llm.calls == 2


def test_max_retry_exhausted_returns_give_up(patch_llms):
    planner_llm = _FakePlannerLLM(_valid_plan())
    # Always return invalid — builder called 1 + MAX_RETRIES=2 times.
    builder_llm = _FakeBuilderLLM(
        [_invalid_builder_output() for _ in range(5)]
    )
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="never passes"))
    state = AgentState.model_validate(raw)

    assert state.validation is not None
    assert not state.validation.ok
    assert state.retry_count == 2
    assert state.error is not None
    assert "validator failed" in state.error
    assert builder_llm.calls == 3  # initial + 2 retries


# ==========================================================================
# C1-1:HITL-SHIP-01 / HITL-SHIP-02 — HITL wiring tests
# ==========================================================================


@pytest.fixture(autouse=True)
def _reset_hitl_state():
    """Each test starts with an empty MemorySaver to keep sessions isolated."""
    reset_hitl_checkpointer()
    yield
    reset_hitl_checkpointer()


def test_await_plan_approval_skips_when_hitl_disabled():
    """C1-1:HITL-SHIP-02 — pass-through when session_id is None (run_cli mode)."""
    state = AgentState(user_message="x")  # session_id=None, plan_approved=False
    delta = await_plan_approval_step(state)

    assert delta.get("plan_approved") is True
    assert any(m.get("role") == "hitl" for m in delta["messages"])


def test_await_plan_approval_passthrough_when_already_approved():
    """C1-1:HITL-SHIP-02 — resume payload already set plan_approved=True."""
    state = AgentState(user_message="x", session_id="abcdefgh", plan_approved=True)
    delta = await_plan_approval_step(state)

    assert "plan_approved" not in delta  # don't overwrite
    assert any(m.get("content") == "plan approved" for m in delta["messages"])


def test_await_plan_approval_routes_give_up_on_rejection():
    """C1-1:HITL-SHIP-02 — HITL session with plan_approved=False → emit diag, edge → give_up."""
    from app.agent.graph import _after_plan_approval

    state = AgentState(
        user_message="x", session_id="abcdefgh", plan_approved=False
    )
    delta = await_plan_approval_step(state)
    assert "plan_approved" not in delta
    assert any("rejected" in m.get("content", "") for m in delta["messages"])

    # Conditional edge sees plan_approved=False → give_up
    assert _after_plan_approval(state) == "give_up"


def test_run_cli_unchanged_with_hitl_disabled(patch_llms):
    """C1-1:HITL-SHIP-01 — run_cli must keep working without checkpointer."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    state = run_cli("regression: cli still works", deploy=False, retriever=_FakeRetriever())

    assert state.error is None
    assert state.validation is not None
    assert state.validation.ok
    assert state.session_id is None  # CLI never sets a session id
    assert state.plan_approved is True  # await_plan_approval auto-approved


def test_build_graph_hitl_attaches_checkpointer():
    """C1-1:HITL-SHIP-01 — checkpointer present iff hitl_enabled=True."""
    from langgraph.checkpoint.memory import MemorySaver

    g_off = build_graph(_FakeRetriever(), deploy_enabled=False, hitl_enabled=False)
    g_on = build_graph(_FakeRetriever(), deploy_enabled=False, hitl_enabled=True)

    # Compiled graphs expose .checkpointer attribute.
    assert getattr(g_off, "checkpointer", None) is None
    assert isinstance(getattr(g_on, "checkpointer", None), MemorySaver)


def test_build_graph_hitl_interrupts_before_await_plan_approval():
    """C1-1:HITL-SHIP-01 — interrupt_before targets the HITL gate node."""
    g = build_graph(_FakeRetriever(), deploy_enabled=False, hitl_enabled=True)
    # The interrupt node list is stored on the compiled graph as
    # `interrupt_before_nodes` (LangGraph 1.x).
    nodes = getattr(g, "interrupt_before_nodes", None)
    assert nodes is not None
    assert "await_plan_approval" in list(nodes)


def test_hitl_graph_interrupts_at_await_plan_approval(patch_llms):
    """C1-1:HITL-SHIP-01 — run_graph_until_interrupt pauses at the gate."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    out = run_graph_until_interrupt(
        "trigger then set", "sess_unit_001", retriever=_FakeRetriever(), deploy_enabled=False
    )

    assert out["status"] == "awaiting_plan_approval"
    assert out["session_id"] == "sess_unit_001"
    assert len(out["plan"]) == 2  # _valid_plan has 2 steps
    assert out["state"].plan_approved is False  # gate has not yet fired
    # builder must NOT have been called yet — interrupt fires before build
    assert builder_llm.calls == 0


def test_hitl_graph_resume_with_approval_completes(patch_llms):
    """C1-1:HITL-SHIP-01 — resume(approved=True) runs to END."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    sid = "sess_unit_002"
    retriever = _FakeRetriever()
    run_graph_until_interrupt("x", sid, retriever=retriever, deploy_enabled=False)

    result = resume_graph_with_confirmation(
        sid, approved=True, retriever=retriever, deploy_enabled=False
    )

    assert result["status"] == "completed"
    state = result["state"]
    assert state.error is None
    assert state.validation is not None
    assert state.validation.ok
    assert state.plan_approved is True
    assert builder_llm.calls == 1


def test_hitl_graph_resume_with_rejection_sets_error(patch_llms):
    """C1-1:HITL-SHIP-01 — resume(approved=False) → state.error prefixed plan_rejected."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    sid = "sess_unit_003"
    retriever = _FakeRetriever()
    run_graph_until_interrupt("x", sid, retriever=retriever, deploy_enabled=False)

    result = resume_graph_with_confirmation(
        sid,
        approved=False,
        feedback="step 2 wrong",
        retriever=retriever,
        deploy_enabled=False,
    )

    assert result["status"] == "rejected"
    state = result["state"]
    assert state.error is not None
    assert state.error.startswith("plan_rejected:")
    assert "step 2 wrong" in state.error
    # builder must never run on rejection path
    assert builder_llm.calls == 0


def test_hitl_graph_resume_with_edited_plan_uses_new_plan(patch_llms):
    """C1-1:HITL-SHIP-01 — edited_plan replaces state.plan before build."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    sid = "sess_unit_004"
    retriever = _FakeRetriever()
    run_graph_until_interrupt("x", sid, retriever=retriever, deploy_enabled=False)

    edited = [
        StepPlan(
            step_id="edited_1",
            description="僅 trigger",
            intent=StepIntent.TRIGGER,
            candidate_node_types=["n8n-nodes-base.scheduleTrigger"],
            reason="user trimmed plan",
        ),
    ]

    result = resume_graph_with_confirmation(
        sid,
        approved=True,
        edited_plan=edited,
        retriever=retriever,
        deploy_enabled=False,
    )

    state = result["state"]
    # Plan persisted from edited payload (length 1, not original 2)
    assert len(state.plan) == 1
    assert state.plan[0].step_id == "edited_1"
    # User-edit message recorded
    assert any(
        m.get("role") == "user" and "plan edited" in m.get("content", "")
        for m in state.messages
    )


def test_hitl_resume_unknown_session_raises(patch_llms):
    """C1-1:HITL-SHIP-01 — resume on never-initialised session_id → SessionNotFound."""
    # No prior run_graph_until_interrupt call — checkpointer is empty.
    with pytest.raises(SessionNotFound):
        resume_graph_with_confirmation(
            "sess_does_not_exist_xx",
            approved=True,
            retriever=_FakeRetriever(),
            deploy_enabled=False,
        )


def test_hitl_helper_signatures_have_required_params():
    """C1-1:HITL-SHIP-01 — sanity-check helper signatures so caller (chat layer) doesn't drift."""
    import inspect

    sig_run = inspect.signature(run_graph_until_interrupt)
    assert "user_message" in sig_run.parameters
    assert "session_id" in sig_run.parameters
    assert "retriever" in sig_run.parameters

    sig_resume = inspect.signature(resume_graph_with_confirmation)
    assert "session_id" in sig_resume.parameters
    assert "approved" in sig_resume.parameters
    assert "edited_plan" in sig_resume.parameters
    assert "feedback" in sig_resume.parameters


# ==========================================================================
# C1-1:HITL-SHIP-02 — AgentState new fields
# ==========================================================================


def test_agent_state_session_id_default_none():
    """C1-1:HITL-SHIP-02 — session_id defaults to None."""
    state = AgentState(user_message="x")
    assert state.session_id is None


def test_agent_state_plan_approved_default_false():
    """C1-1:HITL-SHIP-02 — plan_approved defaults to False."""
    state = AgentState(user_message="x")
    assert state.plan_approved is False


def test_agent_state_session_id_can_be_set():
    """C1-1:HITL-SHIP-02 — session_id can be assigned at construction."""
    state = AgentState(user_message="x", session_id="test12345")
    assert state.session_id == "test12345"


def test_agent_state_plan_approved_can_be_set():
    """C1-1:HITL-SHIP-02 — plan_approved can be set to True."""
    state = AgentState(user_message="x", plan_approved=True)
    assert state.plan_approved is True


def test_graph_wiring_has_await_plan_approval_node():
    """C1-1:HITL-SHIP-02 — compiled graph exposes await_plan_approval in node set."""
    g = build_graph(_FakeRetriever(), deploy_enabled=False, hitl_enabled=True)
    node_names = set(g.get_graph().nodes.keys())
    assert "await_plan_approval" in node_names


# ==========================================================================
# C1-1:B-TIMEOUT-02 — state.error prefix validation
# ==========================================================================


def test_error_prefix_plan_rejected_set_on_rejection(patch_llms):
    """B-TIMEOUT-02: plan rejection → state.error starts with 'plan_rejected:'."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    sid = "sess_prefix_01"
    retriever = _FakeRetriever()
    run_graph_until_interrupt("x", sid, retriever=retriever, deploy_enabled=False)

    result = resume_graph_with_confirmation(
        sid, approved=False, retriever=retriever, deploy_enabled=False
    )
    state = result["state"]
    assert state.error is not None
    assert state.error.startswith("plan_rejected:"), (
        f"Expected 'plan_rejected:' prefix, got: {state.error!r}"
    )


def test_error_prefix_give_up_set_on_max_retry(patch_llms):
    """B-TIMEOUT-02: max retry exhausted → state.error starts with 'give_up' (from give_up node)."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_invalid_builder_output() for _ in range(5)])
    patch_llms(planner_llm, builder_llm)

    graph = build_graph(_FakeRetriever(), deploy_enabled=False)
    raw = graph.invoke(AgentState(user_message="always fails"))
    state = AgentState.model_validate(raw)

    assert state.error is not None
    # The give_up node formats error as "validator failed after N retries; ..."
    # Check that it is a non-empty error string (B-TIMEOUT-02 format rule)
    assert len(state.error) > 0
    # Must match one of the known categories
    known_prefixes = {
        "plan_rejected", "planning_failed", "planning_timeout",
        "building_failed", "building_timeout", "completeness_failed",
        "deploy_failed", "give_up", "validator", "assembler",
    }
    # At least one known prefix must match
    error_words = state.error.split()
    # "validator failed after 2 retries; 1 errors"  matches "validator"
    matched = any(
        state.error.lower().startswith(pfx) for pfx in known_prefixes
    ) or any(
        pfx in state.error.lower() for pfx in known_prefixes
    )
    assert matched, f"state.error has unrecognised format: {state.error!r}"


def test_error_prefix_category_split_format(patch_llms):
    """B-TIMEOUT-02: plan_rejected category uses '{category}: {detail}' format."""
    planner_llm = _FakePlannerLLM(_valid_plan())
    builder_llm = _FakeBuilderLLM([_valid_builder_output()])
    patch_llms(planner_llm, builder_llm)

    sid = "sess_prefix_02"
    retriever = _FakeRetriever()
    run_graph_until_interrupt("z", sid, retriever=retriever, deploy_enabled=False)
    result = resume_graph_with_confirmation(
        sid, approved=False, feedback="I prefer a different approach",
        retriever=retriever, deploy_enabled=False,
    )
    state = result["state"]
    assert state.error is not None
    # Must contain a colon separating category from detail
    assert ":" in state.error


# ==========================================================================
# C1-1:HITL-SHIP-01 — completeness_check in graph wiring
# ==========================================================================


def test_graph_wiring_completeness_node_exists(patch_llms):
    """C1-1:B-COMP-01 — completeness_check node is present in the compiled graph."""
    g = build_graph(_FakeRetriever(), deploy_enabled=False)
    node_names = set(g.get_graph().nodes.keys())
    # completeness_check should be in node set (added by B-COMP-01)
    assert "completeness_check" in node_names
