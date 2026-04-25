"""Unit tests for chat dispatcher (C1-9:CHAT-DISP-01/02/03 + CHAT-OBS-01).

Baseline coverage for the backend-engineer-opus delivery:
- session_id resolution (None / explicit valid / explicit invalid)
- keyword match propagation into log + system prompt
- history truncation (under-limit / over-limit / tool-pair preserved)
- pure-text turn (LLM returns no tool_calls → status="chat")
- single tool call turn (LLM emits build_workflow → tool runs → second LLM
  produces user-facing text → state="awaiting_plan_approval")
- multiple tool_calls in one response (only first honoured)
- tool returns ok=False → second LLM still produces friendly text
- second LLM tool_call attempt is ignored (no recursion)

LLM and ``make_chat_tools`` are mocked so these tests don't require langgraph
or a live LLM endpoint.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.chat import session_store as ss_module
from app.chat.dispatcher import (
    ChatTurnResult,
    _truncate_history,
    build_system_prompt,
    dispatch_chat_turn,
)
from app.chat.keywords import KeywordHits
from app.chat.session_store import SessionState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_session_store():
    """Each test gets a fresh process-local SessionStore singleton."""
    ss_module._store_instance = None
    yield
    ss_module._store_instance = None


def _ai_text(text: str = "hello") -> AIMessage:
    return AIMessage(content=text)


def _ai_tool(
    *,
    tool_name: str,
    args: dict[str, Any],
    text: str = "",
    tool_id: str = "call_1",
) -> AIMessage:
    """An AIMessage carrying a single tool_call (LangChain dict format)."""
    return AIMessage(
        content=text,
        tool_calls=[{"id": tool_id, "name": tool_name, "args": args}],
    )


def _ai_multi_tool(*, calls: list[dict[str, Any]], text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=calls)


# ---------------------------------------------------------------------------
# CHAT-DISP-03 — _truncate_history
# ---------------------------------------------------------------------------


def test_truncate_under_limit_noop() -> None:
    h = [{"role": "user", "content": str(i)} for i in range(5)]
    out = _truncate_history(h, max_len=10)
    assert out == h


def test_truncate_drops_oldest() -> None:
    h = [{"role": "user", "content": str(i)} for i in range(15)]
    out = _truncate_history(h, max_len=10)
    assert len(out) == 10
    # 5 oldest dropped → starts at "5"
    assert out[0]["content"] == "5"
    assert out[-1]["content"] == "14"


def test_truncate_preserves_tool_pair_when_dropping() -> None:
    """A drop point in the middle of an assistant(tool_calls)→tool pair
    must not split them."""
    # 5 chitchat user msgs, then assistant(tool_call), tool result, then 5 more user msgs.
    h: list[dict[str, Any]] = [
        {"role": "user", "content": f"u{i}"} for i in range(5)
    ]
    h.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "build_workflow", "args": {}}],
        }
    )
    h.append({"role": "tool", "content": "{}", "tool_call_id": "c1"})
    h.extend({"role": "user", "content": f"v{i}"} for i in range(5))
    # Total 12. Set max_len=8 so naive cut would drop into the pair.
    out = _truncate_history(h, max_len=8)
    # Either the pair is wholly retained or wholly dropped — never split.
    has_assistant_tc = any(
        m.get("role") == "assistant" and m.get("tool_calls") for m in out
    )
    has_tool = any(m.get("role") == "tool" for m in out)
    assert has_assistant_tc == has_tool
    # We never produce orphan tool messages
    if has_tool:
        # tool must not be the first message.
        assert out[0].get("role") != "tool"


def test_truncate_orphan_tool_at_front_dropped() -> None:
    """Orphan tool messages at the front (no preceding tool_call assistant)
    are dropped as a single-element group, preventing 400s from providers."""
    h: list[dict[str, Any]] = [
        {"role": "tool", "content": "orphan", "tool_call_id": "x"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
    ]
    out = _truncate_history(h, max_len=2)
    # The orphan must be the first thing dropped.
    assert all(m.get("role") != "tool" for m in out) or out[0].get("role") != "tool"


# ---------------------------------------------------------------------------
# CHAT-DISP-02 — build_system_prompt
# ---------------------------------------------------------------------------


def test_prompt_base_only() -> None:
    sess = SessionState(session_id="abc12345")
    p = build_system_prompt(sess, KeywordHits(build=[], confirm=[], reject=[]))
    # No injected blocks (the base prompt may mention "<plan_pending>" in
    # describing it, but the live `<plan_pending>` opening tag at line start
    # is the dispatcher's injection marker).
    assert "<plan_pending>\n" not in p
    assert "<keyword_hint>\n" not in p


def test_prompt_build_hint_when_not_awaiting() -> None:
    sess = SessionState(session_id="abc12345")
    p = build_system_prompt(sess, KeywordHits(build=["建立 workflow"], confirm=[], reject=[]))
    assert "<keyword_hint>" in p
    assert "建立 workflow" in p


def test_prompt_plan_pending_block() -> None:
    sess = SessionState(
        session_id="abc12345",
        awaiting_plan_approval=True,
        pending_plan_summary="1. step one\n2. step two",
    )
    p = build_system_prompt(sess, KeywordHits(build=[], confirm=[], reject=[]))
    assert "<plan_pending>" in p
    assert "step one" in p


def test_prompt_confirm_hint_only_when_awaiting() -> None:
    """Confirm keywords must NOT inject a hint when no plan is pending."""
    sess = SessionState(session_id="abc12345", awaiting_plan_approval=False)
    p = build_system_prompt(sess, KeywordHits(build=[], confirm=["好"], reject=[]))
    # confirm hint is suppressed
    assert "confirm_plan(approved=true)" not in p


def test_prompt_reject_with_awaiting_suggests_confirm_false() -> None:
    sess = SessionState(
        session_id="abc12345",
        awaiting_plan_approval=True,
        pending_plan_summary="(plan)",
    )
    p = build_system_prompt(sess, KeywordHits(build=[], confirm=[], reject=["不要"]))
    assert "confirm_plan(approved=false)" in p


# ---------------------------------------------------------------------------
# CHAT-DISP-01 — session_id resolution
# ---------------------------------------------------------------------------


def _patched_dispatcher(
    *,
    first_response: AIMessage,
    second_response: AIMessage | None = None,
    tool_result: dict[str, Any] | None = None,
):
    """Build (patches, fake_llm, fake_make_tools_calls)."""
    fake_llm = MagicMock()
    fake_llm_with_tools = MagicMock()
    fake_llm_no_tools = MagicMock()
    fake_llm.bind_tools.side_effect = (
        lambda tools, **kw: fake_llm_no_tools if "tool_choice" in kw else fake_llm_with_tools
    )
    fake_llm.invoke.return_value = second_response or _ai_text("fallback")
    fake_llm_with_tools.invoke.return_value = first_response
    fake_llm_no_tools.invoke.return_value = second_response or _ai_text("done")

    fake_tool = MagicMock()
    fake_tool.name = "build_workflow"
    fake_tool.invoke.return_value = tool_result or {
        "ok": True,
        "status": "awaiting_plan_approval",
        "plan_summary": "1. step",
    }
    fake_confirm_tool = MagicMock()
    fake_confirm_tool.name = "confirm_plan"
    fake_confirm_tool.invoke.return_value = tool_result or {
        "ok": True,
        "status": "deployed",
        "workflow_url": "https://x",
    }

    def _mt(session_id, **kwargs):  # noqa: ARG001
        return [fake_tool, fake_confirm_tool]

    p1 = patch("app.chat.dispatcher._make_chat_llm", return_value=fake_llm)
    p2 = patch("app.chat.dispatcher.make_chat_tools", side_effect=_mt)
    return p1, p2, fake_tool, fake_confirm_tool, fake_llm_with_tools, fake_llm_no_tools


def test_dispatch_session_id_none_generates_new() -> None:
    p1, p2, *_ = _patched_dispatcher(first_response=_ai_text("Hi!"))
    with p1, p2:
        result = dispatch_chat_turn(None, "hello")
    assert result.session_id
    assert len(result.session_id) >= 8
    assert result.status == "chat"
    assert result.assistant_text == "Hi!"


def test_dispatch_session_id_invalid_raises_value_error() -> None:
    with pytest.raises(ValueError):
        dispatch_chat_turn("ab", "hi")  # too short


def test_dispatch_session_id_explicit_valid_creates_or_fetches() -> None:
    p1, p2, *_ = _patched_dispatcher(first_response=_ai_text("Hi"))
    with p1, p2:
        r1 = dispatch_chat_turn("user_test_001", "msg one")
        r2 = dispatch_chat_turn("user_test_001", "msg two")
    assert r1.session_id == "user_test_001"
    assert r2.session_id == "user_test_001"


# ---------------------------------------------------------------------------
# CHAT-DISP-01 — pure chat path
# ---------------------------------------------------------------------------


def test_dispatch_pure_chat_status() -> None:
    p1, p2, *_ = _patched_dispatcher(first_response=_ai_text("天氣不錯啊"))
    with p1, p2:
        r = dispatch_chat_turn(None, "今天天氣怎樣")
    assert r.status == "chat"
    assert "天氣" in r.assistant_text
    assert r.tool_calls == []
    assert r.workflow_url is None


# ---------------------------------------------------------------------------
# CHAT-DISP-01 — tool call path (single tool, second LLM humanises)
# ---------------------------------------------------------------------------


def test_dispatch_tool_call_runs_and_humanises() -> None:
    first = _ai_tool(tool_name="build_workflow", args={"user_request": "build a wf"})
    second = _ai_text("好的,計畫如下:1. 步驟一")
    tool_result = {
        "ok": True,
        "status": "awaiting_plan_approval",
        "plan_summary": "1. step one",
        "plan": [{"step_id": "step_1"}],
    }
    p1, p2, fake_tool, _, _, _ = _patched_dispatcher(
        first_response=first,
        second_response=second,
        tool_result=tool_result,
    )
    with p1, p2:
        r = dispatch_chat_turn(None, "build me a workflow that fetches github stars")
    assert r.status == "awaiting_plan_approval"
    assert r.assistant_text.startswith("好的")
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0]["name"] == "build_workflow"
    assert r.tool_calls[0]["status"] == "awaiting_plan_approval"
    assert r.plan == [{"step_id": "step_1"}]
    fake_tool.invoke.assert_called_once()


def test_dispatch_tool_call_updates_session_pending_state() -> None:
    first = _ai_tool(tool_name="build_workflow", args={"user_request": "x"})
    second = _ai_text("here is the plan")
    tool_result = {
        "ok": True,
        "status": "awaiting_plan_approval",
        "plan_summary": "1. fetch\n2. send",
    }
    p1, p2, *_ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    with p1, p2:
        r = dispatch_chat_turn("sess_aaaa1234", "build something")
    # Inspect the live session
    sess = ss_module.get_session_store().get(r.session_id)
    assert sess is not None
    assert sess.awaiting_plan_approval is True
    assert sess.pending_plan_summary == "1. fetch\n2. send"


def test_dispatch_multiple_tool_calls_first_wins(caplog) -> None:
    first = _ai_multi_tool(
        calls=[
            {"id": "a", "name": "build_workflow", "args": {"user_request": "build me one"}},
            {"id": "b", "name": "confirm_plan", "args": {"approved": True}},
        ],
    )
    second = _ai_text("ack")
    tool_result = {"ok": True, "status": "awaiting_plan_approval", "plan_summary": "1."}
    p1, p2, fake_tool, fake_confirm, *_ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    with caplog.at_level(logging.WARNING), p1, p2:
        r = dispatch_chat_turn(None, "trigger tools")
    # First (build_workflow) ran; confirm_plan did not.
    fake_tool.invoke.assert_called_once()
    fake_confirm.invoke.assert_not_called()
    assert r.tool_calls[0]["name"] == "build_workflow"
    # Warning emitted
    assert any(
        "first_wins" in rec.getMessage() or "multi_tool_call" in str(rec.__dict__)
        for rec in caplog.records
    )


def test_dispatch_tool_returns_not_ok_still_produces_friendly_text() -> None:
    first = _ai_tool(tool_name="build_workflow", args={"user_request": "do it"})
    second = _ai_text("Sorry, I couldn't build that — would you like to retry?")
    tool_result = {
        "ok": False,
        "status": "error",
        "error_category": "building_timeout",
        "error_message": "took too long",
    }
    p1, p2, *_ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    with p1, p2:
        r = dispatch_chat_turn(None, "try to build something")
    # Tool ok=False → status surfaces from tool_result
    assert r.status == "error"
    assert r.assistant_text.startswith("Sorry")
    assert r.error_message == "took too long"


def test_dispatch_second_llm_tool_calls_ignored(caplog) -> None:
    """If the second LLM call somehow returns tool_calls, they are ignored."""
    first = _ai_tool(tool_name="build_workflow", args={"user_request": "do something"})
    # Second LLM tries to call a tool — must be ignored.
    second = _ai_multi_tool(
        calls=[{"id": "z", "name": "build_workflow", "args": {"user_request": "again"}}],
        text="recap text",
    )
    tool_result = {"ok": True, "status": "awaiting_plan_approval", "plan_summary": "p"}
    p1, p2, fake_tool, *_ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    with caplog.at_level(logging.WARNING), p1, p2:
        r = dispatch_chat_turn(None, "build a thing")
    # Tool called exactly once (no recursion).
    fake_tool.invoke.assert_called_once()
    assert r.assistant_text == "recap text"


# ---------------------------------------------------------------------------
# CHAT-OBS-01 — log emission
# ---------------------------------------------------------------------------


def test_obs_log_chat_turn_end_emitted_with_count_only_keyword_hits(caplog) -> None:
    p1, p2, *_ = _patched_dispatcher(first_response=_ai_text("hi"))
    with caplog.at_level(logging.INFO), p1, p2:
        dispatch_chat_turn(None, "幫我建立 workflow")
    end_recs = [r for r in caplog.records if getattr(r, "event", "") == "chat_turn_end"]
    assert end_recs, "chat_turn_end log not emitted"
    rec = end_recs[0]
    # keyword_hits must be a dict of ints (counts), not strings.
    assert isinstance(rec.keyword_hits, dict)
    assert all(isinstance(v, int) for v in rec.keyword_hits.values())
    # And a build hit was counted.
    assert rec.keyword_hits["build"] >= 1


def test_obs_log_redact_trace_clears_tool_calls(monkeypatch, caplog) -> None:
    """REDACT_TRACE=1 zeroes tool_calls in logs (CHAT-SEC-01)."""
    monkeypatch.setenv("REDACT_TRACE", "1")
    first = _ai_tool(tool_name="build_workflow", args={"user_request": "do"})
    tool_result = {"ok": True, "status": "awaiting_plan_approval", "plan_summary": "p"}
    p1, p2, *_ = _patched_dispatcher(
        first_response=first,
        second_response=_ai_text("ok"),
        tool_result=tool_result,
    )
    with caplog.at_level(logging.INFO), p1, p2:
        dispatch_chat_turn(None, "build it")
    end_recs = [r for r in caplog.records if getattr(r, "event", "") == "chat_turn_end"]
    assert end_recs
    assert end_recs[0].tool_calls == []
    assert end_recs[0].user_message == "<redacted>"


# ---------------------------------------------------------------------------
# Catch-all: ChatTurnResult is what the API expects
# ---------------------------------------------------------------------------


def test_chatturnresult_default_shape() -> None:
    r = ChatTurnResult(session_id="abcd1234", assistant_text="hi")
    assert r.tool_calls == []
    assert r.status == "chat"
    assert r.workflow_url is None


# ---------------------------------------------------------------------------
# CHAT-DISP-01 — reject path
# ---------------------------------------------------------------------------


def test_dispatch_reject_path_calls_confirm_false() -> None:
    """CHAT-DISP-01: session.awaiting + 'not anymore' → confirm_plan(approved=False)."""
    first = _ai_tool(tool_name="confirm_plan", args={"approved": False, "feedback": "no"})
    second = _ai_text("已取消計畫")
    tool_result = {
        "ok": True,
        "status": "rejected",
        "message": "plan rejected; you can refine the request and try again.",
    }
    p1, p2, fake_tool, fake_confirm, *_ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    # Set up an awaiting session first
    with p1, p2:
        # Create a fresh session in awaiting state
        store = ss_module.get_session_store()
        sid = "sess_reject01"
        sess = store.create(sid)
        sess.awaiting_plan_approval = True
        sess.pending_plan_summary = "1. fetch\n2. send"
        store.update(sess)

        r = dispatch_chat_turn(sid, "不要了，取消")

    assert r.status == "rejected"
    assert r.assistant_text == "已取消計畫"
    fake_confirm.invoke.assert_called_once()


def test_dispatch_confirm_path_calls_confirm_true() -> None:
    """CHAT-DISP-01: session.awaiting + '確認' → confirm_plan(approved=True)."""
    first = _ai_tool(tool_name="confirm_plan", args={"approved": True})
    second = _ai_text("已部署成功!")
    tool_result = {
        "ok": True,
        "status": "deployed",
        "workflow_url": "https://n8n/w/5",
        "workflow_id": "wf5",
    }
    p1, p2, _, fake_confirm, _, _ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    with p1, p2:
        store = ss_module.get_session_store()
        sid = "sess_confirm01"
        sess = store.create(sid)
        sess.awaiting_plan_approval = True
        sess.pending_plan_summary = "1. trigger\n2. action"
        store.update(sess)

        r = dispatch_chat_turn(sid, "好,確認")

    assert r.status == "deployed"
    assert r.workflow_url == "https://n8n/w/5"
    fake_confirm.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# CHAT-DISP-01 — invalid session_id handling
# ---------------------------------------------------------------------------


def test_dispatch_invalid_session_id_short_raises() -> None:
    """CHAT-DISP-01: < 8 char explicit session_id → ValueError (→ 400 at HTTP layer)."""
    with pytest.raises(ValueError):
        dispatch_chat_turn("abc", "hello")


def test_dispatch_invalid_session_id_traversal_raises() -> None:
    """CHAT-SEC-01: path traversal session_id → ValueError."""
    with pytest.raises(ValueError):
        dispatch_chat_turn("../../etc/passwd", "hello")


# ---------------------------------------------------------------------------
# CHAT-OBS-01 — tool call latency in log
# ---------------------------------------------------------------------------


def test_obs_log_tool_call_latency_present(caplog) -> None:
    """CHAT-OBS-01: tool_calls[0].latency_ms is an integer when a tool runs."""
    first = _ai_tool(tool_name="build_workflow", args={"user_request": "build a wf"})
    second = _ai_text("done")
    tool_result = {"ok": True, "status": "awaiting_plan_approval", "plan_summary": "1."}
    p1, p2, *_ = _patched_dispatcher(
        first_response=first, second_response=second, tool_result=tool_result
    )
    with caplog.at_level(logging.INFO), p1, p2:
        dispatch_chat_turn(None, "build something for me")

    end_recs = [r for r in caplog.records if getattr(r, "event", "") == "chat_turn_end"]
    assert end_recs, "chat_turn_end not emitted"
    rec = end_recs[0]
    tool_calls = rec.tool_calls
    assert len(tool_calls) >= 1
    assert isinstance(tool_calls[0]["latency_ms"], int)


# ---------------------------------------------------------------------------
# CHAT-DISP-03 — additional truncation edge cases
# ---------------------------------------------------------------------------


def test_truncate_exactly_at_limit_noop() -> None:
    """_truncate_history with exactly max_len messages is a no-op."""
    h = [{"role": "user", "content": str(i)} for i in range(10)]
    out = _truncate_history(h, max_len=10)
    assert len(out) == 10
    assert out == h


def test_truncate_max_len_zero_returns_empty() -> None:
    """max_len=0 returns empty list."""
    h = [{"role": "user", "content": "hi"}]
    out = _truncate_history(h, max_len=0)
    assert out == []


# ---------------------------------------------------------------------------
# CHAT-DISP-02 — prompt assembly: reject hint with no pending plan
# ---------------------------------------------------------------------------


def test_prompt_reject_no_plan_suggests_natural_language() -> None:
    """CHAT-DISP-02: reject hit but no pending plan → no confirm_plan hint injected."""
    sess = SessionState(session_id="abc12345", awaiting_plan_approval=False)
    p = build_system_prompt(sess, KeywordHits(build=[], confirm=[], reject=["不要"]))
    # Should NOT suggest confirm_plan when no plan is pending
    assert "confirm_plan(approved=false)" not in p
    # Should still inject a keyword hint about the reject signal
    assert "<keyword_hint>" in p


def test_prompt_with_build_and_confirm_when_awaiting() -> None:
    """CHAT-DISP-02: both build and confirm keywords while awaiting → confirm hint only."""
    sess = SessionState(
        session_id="abc12345",
        awaiting_plan_approval=True,
        pending_plan_summary="(plan)",
    )
    p = build_system_prompt(
        sess, KeywordHits(build=["建立"], confirm=["確認"], reject=[])
    )
    # build hint suppressed while awaiting
    assert "They MAY want to build" not in p
    # confirm hint present
    assert "confirm_plan(approved=true)" in p
