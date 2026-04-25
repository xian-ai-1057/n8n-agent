"""Chat dispatcher — entry point for the chat-first pipeline.

Implements C1-9:
- ``CHAT-DISP-01`` — main 8-step turn pipeline (sanitize → resolve session →
  keyword match → append history → truncate → system prompt → LLM (with at most
  one tool dispatch) → second LLM to humanise tool result → update session →
  return).
- ``CHAT-DISP-02`` — system-prompt assembly (base prompt + plan-pending block +
  keyword hint).
- ``CHAT-DISP-03`` — history truncation that respects ``tool_call`` /
  ``tool_result`` pairs.
- ``CHAT-OBS-01`` — structured chat_turn_start / chat_turn_end log emission.
- ``CHAT-SEC-01`` — ``session_id`` pattern validation, ``REDACT_TRACE`` redaction.

Design notes
------------
* The dispatcher is **synchronous**. The HTTP handler wraps it in
  ``asyncio.to_thread`` + ``asyncio.wait_for`` so a stalled LLM cannot pin the
  event loop.
* Two LLM invocations per tool turn: the first decides whether to call a tool;
  the second turns the tool's structured result into user-facing prose. The
  second invocation is called with ``tool_choice="none"`` so the model cannot
  recurse into another tool call (preventing tool loops).
* Only the **first** tool call from the LLM is honoured per turn (CHAT-DISP-01
  contract). Any extras are logged and discarded.
* The dispatcher never raises into the HTTP handler. Any unexpected exception
  is caught and converted to a ``status="error"`` ``ChatTurnResult``.

Closure of the 5 sub-modules
----------------------------
- ``session_store.get_session_store()`` — durable per-session chat history +
  HITL state flags.
- ``keywords.match_keywords`` — soft hint signal injected into the prompt.
- ``tools.make_chat_tools`` — produces the two ``StructuredTool`` instances
  bound to the resolved ``session_id`` and the in-process
  ``do_confirm_plan`` callable.
- ``agent.graph.run_graph_until_interrupt`` — invoked **indirectly** by the
  ``build_workflow`` tool when the LLM decides to call it.
- ``api.do_confirm_plan`` — invoked **indirectly** by the ``confirm_plan``
  tool when the LLM decides to call it.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from ..config import Settings, get_settings
from ..models.planning import StepPlan
from ..request_context import request_id_var
from .keywords import KeywordHits, match_keywords
from .session_store import SessionState, _validate_session_id, get_session_store
from .tools import make_chat_tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


# C1-9:CHAT-DISP-01
@dataclass
class ChatTurnResult:
    """Outcome of a single dispatcher turn — see CHAT-DISP-01 spec."""

    session_id: str
    assistant_text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # P1-4: typed Literal mirrors ChatResponse.status so mypy can check cross-layer assignment
    status: Literal[
        "chat", "awaiting_plan_approval", "deployed", "rejected", "completed", "error"
    ] = "chat"
    workflow_url: str | None = None
    workflow_id: str | None = None
    error_message: str | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# System-prompt loading + assembly (CHAT-DISP-02)
# ---------------------------------------------------------------------------


_BASE_PROMPT_PATH: Path = Path(__file__).parent / "prompts" / "chat_system.md"

# Hardcoded fallback so the dispatcher never fails to construct a prompt even if
# the on-disk template was removed by accident in some deployment.
_FALLBACK_BASE_PROMPT = (
    "You are an n8n workflow assistant. You have two tools: build_workflow and "
    "confirm_plan. Call them only when the user request is unambiguous and the "
    "appropriate gate (no plan / plan pending) is active. Otherwise reply in "
    "natural language."
)


# C1-9:CHAT-DISP-02
@lru_cache(maxsize=1)
def _load_base_prompt() -> str:
    """Read ``chat_system.md`` from disk once and cache it."""
    try:
        return _BASE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error(
            "chat_system_prompt_load_failed path=%s error=%s — using fallback",
            _BASE_PROMPT_PATH,
            exc,
        )
        return _FALLBACK_BASE_PROMPT


# C1-9:CHAT-DISP-02
def build_system_prompt(session: SessionState, kws: KeywordHits) -> str:
    """Compose the system prompt: base + plan_pending block + keyword hint.

    Spec rules (CHAT-DISP-02):
    - Plan-pending block is injected when ``session.awaiting_plan_approval``;
      it carries the cached ``pending_plan_summary`` so the LLM can re-cite it.
    - Build-keyword hint is suppressed while a plan is pending (the user is
      replying to the plan, not asking for a new one).
    - Confirm-keyword hint is only injected while a plan is pending.
    - Reject-keyword hint:
        * pending plan → suggest ``confirm_plan(approved=false)``;
        * no plan     → suggest a plain reply.
    """
    parts: list[str] = [_load_base_prompt()]

    if session.awaiting_plan_approval:
        summary = session.pending_plan_summary or "(plan summary unavailable)"
        parts.append(
            "<plan_pending>\n"
            f"{summary}\n"
            "</plan_pending>\n"
            "The user has been shown the above plan. If their next message is a "
            "yes / no / edit decision, call confirm_plan with the appropriate "
            "arguments."
        )

    # Build hint: only when not already in a pending-plan loop.
    if kws.has_build() and not session.awaiting_plan_approval:
        parts.append(
            "<keyword_hint>\n"
            f"The user's message contains build-intent keywords ({', '.join(kws.build)}). "
            "They MAY want to build a workflow, but verify the request is "
            "unambiguous (trigger, source, destination, frequency) before "
            "calling build_workflow.\n"
            "</keyword_hint>"
        )

    # Confirm hint: only meaningful when a plan is pending.
    if kws.has_confirm() and session.awaiting_plan_approval:
        parts.append(
            "<keyword_hint>\n"
            f"The user's message contains confirm keywords ({', '.join(kws.confirm)}). "
            "If they're confirming the pending plan, call "
            "confirm_plan(approved=true).\n"
            "</keyword_hint>"
        )

    # Reject hint: behaviour differs based on whether a plan is pending.
    if kws.has_reject():
        if session.awaiting_plan_approval:
            parts.append(
                "<keyword_hint>\n"
                f"The user's message contains reject keywords ({', '.join(kws.reject)}). "
                "If they're rejecting the pending plan, call "
                "confirm_plan(approved=false) with their feedback.\n"
                "</keyword_hint>"
            )
        else:
            parts.append(
                "<keyword_hint>\n"
                f"The user's message contains reject keywords ({', '.join(kws.reject)}). "
                "There is no pending plan, so simply acknowledge their decision "
                "in natural language; do NOT call any tool.\n"
                "</keyword_hint>"
            )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# History truncation (CHAT-DISP-03)
# ---------------------------------------------------------------------------


def _is_assistant_with_tool_calls(msg: dict[str, Any]) -> bool:
    """Return True if msg is an assistant message that issued tool calls."""
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))


def _is_tool_result(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "tool"


# C1-9:CHAT-DISP-03
def _truncate_history(
    history: list[dict[str, Any]], *, max_len: int
) -> list[dict[str, Any]]:
    """Drop oldest messages until ``len(history) <= max_len``.

    Preserves ``assistant(tool_calls)`` + subsequent ``tool`` messages as a
    single atomic group: either drop the entire group together or keep the
    entire group together. May leave the list slightly above ``max_len`` (by
    at most one group's worth) when keeping a group is the only way to avoid
    an orphan tool message — this matches the spec rationale ("整對保留優先,
    即多保留 1 條").

    Edge case: a tool message at the very front with no preceding
    ``assistant(tool_calls)`` is an orphan (should not normally happen). We
    drop orphan tool messages aggressively as a single-element group so the
    LLM provider doesn't reject the request later.

    Time complexity: O(n) over history length. Space: O(n).
    """
    if max_len <= 0:
        return []
    if len(history) <= max_len:
        return list(history)

    # Step 1: partition the history into "atomic groups" walking from oldest to
    # newest. A group is either:
    #   - a single non-pair message (user / assistant-without-tool / system / lone tool*)
    #   - or an assistant-with-tool-calls followed by 1+ consecutive tool messages.
    #
    # *Lone tool messages (orphans) become their own one-element group; safer
    # to drop than keep.
    groups: list[list[dict[str, Any]]] = []
    i = 0
    n = len(history)
    while i < n:
        msg = history[i]
        if _is_assistant_with_tool_calls(msg):
            grp = [msg]
            j = i + 1
            while j < n and _is_tool_result(history[j]):
                grp.append(history[j])
                j += 1
            groups.append(grp)
            i = j
        else:
            groups.append([msg])
            i += 1

    # Step 2: drop oldest groups until the remaining flattened list is
    # ``<= max_len``. We never split a group, so we may end up with up to
    # ``max(group_size) - 1`` items above max_len, which is acceptable per
    # spec (and far better than emitting an orphan tool message).
    total = sum(len(g) for g in groups)
    drop_idx = 0
    while total > max_len and drop_idx < len(groups):
        total -= len(groups[drop_idx])
        drop_idx += 1

    kept_groups = groups[drop_idx:]
    out: list[dict[str, Any]] = []
    for g in kept_groups:
        out.extend(g)
    return out


# ---------------------------------------------------------------------------
# History → LangChain message conversion
# ---------------------------------------------------------------------------


def _history_to_messages(history: list[dict[str, Any]]) -> list[BaseMessage]:
    """Convert dispatcher history dicts into LangChain ``BaseMessage`` objects.

    Mapping:
    - ``role=user``      → ``HumanMessage``
    - ``role=assistant`` → ``AIMessage`` (carrying any ``tool_calls``)
    - ``role=tool``      → ``ToolMessage`` (with ``tool_call_id``)
    - other roles        → ``HumanMessage`` (defensive fallback)
    """
    out: list[BaseMessage] = []
    for m in history:
        role = m.get("role")
        content = m.get("content", "")
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                out.append(AIMessage(content=content, tool_calls=tool_calls))
            else:
                out.append(AIMessage(content=content))
        elif role == "tool":
            tool_call_id = m.get("tool_call_id", "") or ""
            out.append(ToolMessage(content=content, tool_call_id=tool_call_id))
        else:
            out.append(HumanMessage(content=content))
    return out


# ---------------------------------------------------------------------------
# LLM factory (chat-specific)
# ---------------------------------------------------------------------------


def _make_chat_llm(*, settings: Settings) -> ChatOpenAI:
    """Build a plain ``ChatOpenAI`` for the chat layer.

    Uses ``effective_chat_model`` and ``chat_temperature`` from CHAT-CFG-01,
    sharing the OpenAI base URL / API key with the rest of the agent.
    """
    return ChatOpenAI(
        model=settings.effective_chat_model,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        temperature=settings.chat_temperature,
        timeout=settings.llm_timeout_sec,
    )


# ---------------------------------------------------------------------------
# Observability (CHAT-OBS-01) + security (CHAT-SEC-01)
# ---------------------------------------------------------------------------


def _redact_trace_enabled() -> bool:
    """REDACT_TRACE=1 → security-tightened logging + response shaping."""
    return os.environ.get("REDACT_TRACE", "0") == "1"


# C1-9:CHAT-OBS-01
def _log_chat_turn_start(
    *,
    chat_turn_id: str,
    session_id: str,
    user_message: str,
    keyword_hits: KeywordHits,
    history_len_before: int,
) -> None:
    """Emit a structured ``chat_turn_start`` log."""
    redacted = _redact_trace_enabled()
    logger.info(
        "chat_turn_start",
        extra={
            "event": "chat_turn_start",
            "chat_turn_id": chat_turn_id,
            "session_id": session_id,
            "request_id": request_id_var.get(),
            "user_message_len": len(user_message),
            "user_message": "<redacted>" if redacted else user_message[:200],
            "keyword_hits": {
                "build": len(keyword_hits.build),
                "confirm": len(keyword_hits.confirm),
                "reject": len(keyword_hits.reject),
            },
            "chat_history_len_before": history_len_before,
        },
    )


# C1-9:CHAT-OBS-01
# C1-9:CHAT-SEC-01
def _log_chat_turn_end(
    *,
    chat_turn_id: str,
    session_id: str,
    user_message: str,
    keyword_hits: KeywordHits,
    tool_calls: list[dict[str, Any]],
    history_len_before: int,
    history_len_after: int,
    awaiting_plan_approval: bool,
    latency_ms: int,
    tokens_prompt: int | None,
    tokens_completion: int | None,
    status: str,
) -> None:
    """Emit a structured ``chat_turn_end`` log.

    ``keyword_hits`` is logged as **counts** (CHAT-OBS-01). The raw matched
    strings stay in memory for the prompt only — they never reach the log to
    avoid leaking sensitive fragments.

    When ``REDACT_TRACE=1`` (CHAT-SEC-01):
    - ``user_message`` is replaced with ``<redacted>``.
    - ``tool_calls`` is logged as an empty list.
    """
    redacted = _redact_trace_enabled()
    payload: dict[str, Any] = {
        "event": "chat_turn_end",
        "chat_turn_id": chat_turn_id,
        "session_id": session_id,
        "request_id": request_id_var.get(),
        "user_message_len": len(user_message),
        "user_message": "<redacted>" if redacted else user_message[:200],
        "keyword_hits": {
            "build": len(keyword_hits.build),
            "confirm": len(keyword_hits.confirm),
            "reject": len(keyword_hits.reject),
        },
        "tool_calls": [] if redacted else list(tool_calls),
        "chat_history_len_before": history_len_before,
        "chat_history_len_after": history_len_after,
        "awaiting_plan_approval": awaiting_plan_approval,
        "latency_ms": latency_ms,
        "status": status,
    }
    if tokens_prompt is not None:
        payload["tokens_prompt"] = tokens_prompt
    if tokens_completion is not None:
        payload["tokens_completion"] = tokens_completion
    logger.info("chat_turn_end", extra=payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_token_usage(ai_msg: AIMessage) -> tuple[int | None, int | None]:
    """Best-effort extraction of token counts from an ``AIMessage``.

    Different LLM providers expose usage under different keys; we try the
    common ones and fall back to ``(None, None)`` so the dispatcher never
    fails on missing telemetry.
    """
    meta = getattr(ai_msg, "usage_metadata", None)
    if isinstance(meta, dict):
        return (meta.get("input_tokens"), meta.get("output_tokens"))
    response_meta = getattr(ai_msg, "response_metadata", {}) or {}
    usage = response_meta.get("token_usage") or response_meta.get("usage") or {}
    if isinstance(usage, dict):
        return (
            usage.get("prompt_tokens") or usage.get("input_tokens"),
            usage.get("completion_tokens") or usage.get("output_tokens"),
        )
    return (None, None)


def _summarise_tool_args(args: Any) -> str:
    """Compact, log-friendly summary of tool arguments for observability."""
    if not isinstance(args, dict):
        return str(args)[:200]
    keys = list(args.keys())
    return f"keys={keys}"


def _normalise_tool_call(tc: Any) -> dict[str, Any] | None:
    """Normalise a LangChain tool_call (which may be a dict or model object)."""
    if isinstance(tc, dict):
        name = tc.get("name")
        args = tc.get("args") or tc.get("arguments")
        tc_id = tc.get("id")
    else:  # pragma: no cover — defensive
        name = getattr(tc, "name", None)
        args = getattr(tc, "args", None)
        tc_id = getattr(tc, "id", None)
    if not name:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return {"name": name, "args": args, "id": tc_id or uuid.uuid4().hex[:16]}


_TurnStatus = Literal[
    "chat", "awaiting_plan_approval", "deployed", "rejected", "completed", "error"
]
_VALID_TURN_STATUSES: frozenset[str] = frozenset(
    ["chat", "awaiting_plan_approval", "deployed", "rejected", "completed", "error"]
)


def _status_from_tool_result(result: dict[str, Any]) -> _TurnStatus:
    """Map a tool-result dict to a ``ChatTurnResult.status`` value.

    Tool dicts (per CHAT-TOOL-01/02) carry their own ``status`` field; we
    pass it through verbatim when it is a recognised value so the API
    surface mirrors the tool's classification exactly. An unrecognised or
    missing ``status`` defaults to ``"chat"`` (ok) or ``"error"`` (not ok).
    """
    raw = result.get("status")
    if raw and str(raw) in _VALID_TURN_STATUSES:
        return str(raw)  # type: ignore[return-value]  # membership check guarantees Literal
    return "chat" if result.get("ok") else "error"


# ---------------------------------------------------------------------------
# Public dispatcher entry point (CHAT-DISP-01)
# ---------------------------------------------------------------------------


# C1-9:CHAT-DISP-01
# C1-9:CHAT-SEC-01
def dispatch_chat_turn(
    session_id: str | None,
    user_message: str,
    *,
    retriever: Any | None = None,
    deploy_enabled: bool = True,
) -> ChatTurnResult:
    """Process a single chat turn end-to-end.

    See module docstring for the 8-step pipeline outline.

    The function never raises. Internal errors are caught and surfaced as a
    ``ChatTurnResult`` with ``status="error"`` so the HTTP handler can
    convert them to a 200/500 response uniformly.

    Parameters
    ----------
    session_id:
        ``None`` to allocate a fresh server-generated id; otherwise a
        client-supplied id matching ``^[A-Za-z0-9_-]{8,64}$``.
    user_message:
        The raw user message. Length / sanitisation is the caller's job
        (the HTTP handler validates against ``ChatRequest`` schema and runs
        any C1-8 sanitiser). The dispatcher trusts what it receives.
    retriever, deploy_enabled:
        Forwarded to the ``build_workflow`` tool's graph helper.
    """
    settings = get_settings()
    chat_turn_id = uuid.uuid4().hex[:16]
    t_start = time.monotonic()

    store = get_session_store()

    # ----- Step 1: resolve session_id -----------------------------------
    # CHAT-SEC-01: validate explicit ids; on mismatch raise ValueError so the
    # HTTP handler can map to 400/404. New ids bypass validation because the
    # store generates them deterministically via uuid4().hex[:16].
    try:
        if session_id is None:
            session = store.create()
            resolved_session_id = session.session_id
        else:
            _validate_session_id(session_id)
            existing = store.get(session_id)
            session = existing if existing is not None else store.create(session_id)
            resolved_session_id = session.session_id
    except ValueError:
        # Re-raise so the HTTP handler can produce a 400 cleanly.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat_dispatch_session_resolve_failed: %s", exc)
        # Last-resort fallback: synthesise a fresh session so we still return
        # *something* to the user instead of a 500.
        session = store.create()
        resolved_session_id = session.session_id

    history_len_before = len(session.history)

    # ----- Step 2: keyword match ----------------------------------------
    kws = match_keywords(user_message)

    _log_chat_turn_start(
        chat_turn_id=chat_turn_id,
        session_id=resolved_session_id,
        user_message=user_message,
        keyword_hits=kws,
        history_len_before=history_len_before,
    )

    # Run the rest of the pipeline inside a try-block so any unexpected error
    # surfaces as a structured ``status=error`` result rather than an HTTP 500.
    final_status: _TurnStatus = "chat"
    final_assistant_text = ""
    final_tool_calls_log: list[dict[str, Any]] = []
    workflow_url: str | None = None
    workflow_id: str | None = None
    error_message: str | None = None
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    plan_for_response: list[dict[str, Any]] = []

    try:
        # ----- Step 3: append user message ------------------------------
        session.history.append({"role": "user", "content": user_message})

        # ----- Step 4: truncate history ---------------------------------
        session.history = _truncate_history(
            session.history, max_len=settings.chat_max_history
        )

        # ----- Step 5: build system prompt ------------------------------
        system_text = build_system_prompt(session, kws)

        # ----- Step 6: first LLM invocation -----------------------------
        # The pending_plan we hand to the tool factory is intentionally None
        # here — full plan re-hydration from the LangGraph checkpointer is
        # deferred (see CHAT-DISP-01 §rationale + CHAT-API-02). The
        # ``confirm_plan`` tool falls back to standalone-mode validation when
        # the LLM passes edits without a base plan.
        tools = make_chat_tools(
            resolved_session_id,
            retriever=retriever,
            deploy_enabled=deploy_enabled,
            pending_plan=None,
        )
        tool_by_name = {t.name: t for t in tools}

        llm = _make_chat_llm(settings=settings)
        llm_with_tools = llm.bind_tools(tools)

        messages: list[BaseMessage] = [SystemMessage(content=system_text)]
        messages.extend(_history_to_messages(session.history))

        try:
            first_response: AIMessage = llm_with_tools.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chat_dispatch_llm_first_invoke_failed: %s", exc)
            final_status = "error"
            error_message = f"chat_llm_unavailable: {exc}"
            final_assistant_text = (
                "Sorry, the chat service is temporarily unavailable. "
                "Please try again in a moment."
            )
            session.history.append(
                {"role": "assistant", "content": final_assistant_text}
            )
            store.update(session)
            return ChatTurnResult(
                session_id=resolved_session_id,
                assistant_text=final_assistant_text,
                tool_calls=[],
                status=final_status,
                error_message=error_message,
            )

        tp, tc = _extract_token_usage(first_response)
        if tp is not None:
            tokens_prompt = tp
        if tc is not None:
            tokens_completion = tc

        raw_tool_calls = list(first_response.tool_calls or [])
        normalised: list[dict[str, Any]] = []
        for tc_ in raw_tool_calls:
            n = _normalise_tool_call(tc_)
            if n is not None:
                normalised.append(n)

        # ----- Step 7: tool dispatch (first-wins) -----------------------
        if not normalised:
            # Pure text turn.
            # P1-3: content may be list[str|dict] on multi-modal responses; coerce.
            assistant_text = str(first_response.content or "").strip()
            if not assistant_text:
                # Some local models occasionally produce empty strings; be
                # defensive so the user always gets some reply.
                assistant_text = "(no reply)"
            session.history.append(
                {"role": "assistant", "content": assistant_text}
            )
            final_assistant_text = assistant_text
            final_status = "chat"
        else:
            # First-wins: any extra tool calls are dropped with a warning.
            chosen = normalised[0]
            if len(normalised) > 1:
                logger.warning(
                    "chat_dispatch_multiple_tool_calls_first_wins",
                    extra={
                        "event": "chat_dispatch_multi_tool_call",
                        "chat_turn_id": chat_turn_id,
                        "session_id": resolved_session_id,
                        "n_tool_calls": len(normalised),
                        "chosen": chosen["name"],
                        "ignored": [tc_["name"] for tc_ in normalised[1:]],
                    },
                )

            tool_name = chosen["name"]
            tool_args = chosen["args"]
            tool_call_id = chosen["id"]

            tool_started = time.monotonic()
            tool = tool_by_name.get(tool_name)
            if tool is None:
                # Hallucinated tool name — synthesise a tool error instead of
                # crashing so the LLM can apologise to the user on the second
                # invocation.
                tool_result: dict[str, Any] = {
                    "ok": False,
                    "status": "error",
                    "error": f"unknown_tool:{tool_name}",
                    "error_message": f"tool {tool_name!r} is not available",
                }
            else:
                try:
                    invoked = tool.invoke(tool_args)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "chat_dispatch_tool_invoke_raised name=%s: %s",
                        tool_name,
                        exc,
                    )
                    invoked = {
                        "ok": False,
                        "status": "error",
                        "error": f"tool_invoke_raised:{type(exc).__name__}",
                        "error_message": str(exc),
                    }
                # Tool functions are contracted (CHAT-TOOL-01/02) to always
                # return dicts — coerce defensively in case a future tool
                # returns a string.
                if isinstance(invoked, dict):
                    tool_result = invoked
                else:
                    tool_result = {"ok": True, "status": "chat", "result": invoked}

            tool_latency_ms = int((time.monotonic() - tool_started) * 1000)

            # Append assistant message that requested the tool (carries tool_calls)
            # so the second LLM call sees a coherent conversation.
            session.history.append(
                {
                    "role": "assistant",
                    "content": first_response.content or "",
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "name": tool_name,
                            "args": tool_args,
                        }
                    ],
                }
            )
            session.history.append(
                {
                    "role": "tool",
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                    "tool_call_id": tool_call_id,
                }
            )

            # Capture log-friendly trace of the tool call.
            tool_log_entry = {
                "name": tool_name,
                "args_summary": _summarise_tool_args(tool_args),
                "status": tool_result.get("status", "unknown"),
                "ok": bool(tool_result.get("ok")),
                "latency_ms": tool_latency_ms,
            }
            final_tool_calls_log.append(tool_log_entry)

            # ----- Update session HITL state from tool result ---------
            tool_status = tool_result.get("status")
            if tool_status == "awaiting_plan_approval":
                session.awaiting_plan_approval = True
                session.pending_plan_summary = tool_result.get("plan_summary")
                if isinstance(tool_result.get("plan"), list):
                    plan_for_response = list(tool_result["plan"])
            elif tool_status in {"deployed", "rejected", "completed"}:
                session.awaiting_plan_approval = False
                session.pending_plan_summary = None

            if tool_status == "deployed":
                workflow_url = tool_result.get("workflow_url")
                workflow_id = tool_result.get("workflow_id")

            # ----- Step 7b: second LLM invocation to humanise --------
            # tool_choice="none" prevents an infinite tool-call loop. If the
            # provider doesn't honour that hint we still defensively ignore
            # any tool_calls returned in the second response.
            try:
                second_messages: list[BaseMessage] = [
                    SystemMessage(content=system_text)
                ]
                second_messages.extend(_history_to_messages(session.history))
                try:
                    no_tool_llm = llm.bind_tools(tools, tool_choice="none")
                    second_response: AIMessage = no_tool_llm.invoke(second_messages)
                except (TypeError, ValueError):
                    # Provider doesn't support tool_choice="none" — fall back
                    # to plain LLM (no tools bound).
                    second_response = llm.invoke(second_messages)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "chat_dispatch_llm_second_invoke_failed: %s", exc
                )
                # Synthesise a graceful reply derived from the tool status so
                # the user still sees something useful.
                if tool_result.get("ok"):
                    second_text = (
                        f"The tool {tool_name} finished with status "
                        f"{tool_result.get('status')}, but I had trouble "
                        "phrasing the reply. Please ask me to recap."
                    )
                else:
                    second_text = (
                        "Sorry — I tried to take an action but it failed: "
                        f"{tool_result.get('error_message', 'unknown error')}."
                    )
                session.history.append(
                    {"role": "assistant", "content": second_text}
                )
                final_assistant_text = second_text
                final_status = _status_from_tool_result(tool_result)
                if not tool_result.get("ok"):
                    error_message = tool_result.get("error_message")
            else:
                tp2, tc2 = _extract_token_usage(second_response)
                if tp2 is not None:
                    tokens_prompt = (tokens_prompt or 0) + tp2
                if tc2 is not None:
                    tokens_completion = (tokens_completion or 0) + tc2

                # Defensive: ignore any tool_calls in the second response so
                # we cannot loop.
                if second_response.tool_calls:
                    logger.warning(
                        "chat_dispatch_second_llm_emitted_tool_calls_ignored",
                        extra={
                            "chat_turn_id": chat_turn_id,
                            "session_id": resolved_session_id,
                            "n": len(second_response.tool_calls),
                        },
                    )

                # P1-3: content may be list[str|dict] on multi-modal responses; coerce.
                second_text = str(second_response.content or "").strip()
                if not second_text:
                    # Fall back to a status-derived sentence rather than an
                    # empty bubble.
                    second_text = (
                        f"({tool_name} returned status "
                        f"{tool_result.get('status')})"
                    )
                session.history.append(
                    {"role": "assistant", "content": second_text}
                )
                final_assistant_text = second_text
                final_status = _status_from_tool_result(tool_result)
                if not tool_result.get("ok"):
                    error_message = tool_result.get("error_message")

        # ----- Step 8: persist session + truncate again ----------------
        session.history = _truncate_history(
            session.history, max_len=settings.chat_max_history
        )
        store.update(session)

        latency_ms = int((time.monotonic() - t_start) * 1000)
        _log_chat_turn_end(
            chat_turn_id=chat_turn_id,
            session_id=resolved_session_id,
            user_message=user_message,
            keyword_hits=kws,
            tool_calls=final_tool_calls_log,
            history_len_before=history_len_before,
            history_len_after=len(session.history),
            awaiting_plan_approval=session.awaiting_plan_approval,
            latency_ms=latency_ms,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            status=final_status,
        )

        return ChatTurnResult(
            session_id=resolved_session_id,
            assistant_text=final_assistant_text,
            tool_calls=final_tool_calls_log,
            status=final_status,
            workflow_url=workflow_url,
            workflow_id=workflow_id,
            error_message=error_message,
            plan=plan_for_response,
        )
    except Exception as exc:  # noqa: BLE001
        # Catch-all so the dispatcher never propagates raw exceptions to the
        # HTTP handler. We *intentionally* re-raise ValueError above (only the
        # session_id case) before reaching this block.
        logger.exception("chat_dispatch_unexpected_failure: %s", exc)
        latency_ms = int((time.monotonic() - t_start) * 1000)
        _log_chat_turn_end(
            chat_turn_id=chat_turn_id,
            session_id=resolved_session_id,
            user_message=user_message,
            keyword_hits=kws,
            tool_calls=final_tool_calls_log,
            history_len_before=history_len_before,
            history_len_after=len(session.history),
            awaiting_plan_approval=session.awaiting_plan_approval,
            latency_ms=latency_ms,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            status="error",
        )
        return ChatTurnResult(
            session_id=resolved_session_id,
            assistant_text="Sorry — an internal error occurred while handling your message.",
            tool_calls=final_tool_calls_log,
            status="error",
            error_message=f"internal_error: {type(exc).__name__}",
        )


# Re-export StepPlan so test fixtures can build pending_plan ergonomically.
__all__ = [
    "ChatTurnResult",
    "build_system_prompt",
    "dispatch_chat_turn",
    "_truncate_history",
    "StepPlan",
]
