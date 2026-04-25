"""HTTP route handlers (Implements C1-5).

Factored out of ``app.main`` so that file stays short and we can unit-test
individual handlers via FastAPI's TestClient with monkeypatches.

Rules followed from C1-5:
- ``POST /chat`` is sync-over-HTTP. We wrap the (blocking) LangGraph invocation
  in ``asyncio.to_thread`` and enforce a 180 s budget with ``asyncio.wait_for``.
- ``GET /health`` probes the OpenAI-compat endpoint / n8n / Chroma, each under
  a 3 s budget. Top level ``ok`` is the AND of the three. Response is always
  HTTP 200 so external probes can introspect the details themselves.
- We deploy whenever ``N8N_API_KEY`` is set; otherwise ``workflow_url`` stays
  ``None`` and only ``workflow_json`` is returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..agent.graph import SessionNotFound, resume_graph_with_confirmation
from ..chat.dispatcher import ChatTurnResult, dispatch_chat_turn  # C1-9:CHAT-API-01
from ..chat.session_store import SESSION_ID_PATTERN  # P1-6: canonical pattern
from ..config import Settings, get_settings
from ..models.agent_state import AgentState
from ..models.api import ChatRequest, ChatResponse, ConfirmPlanRequest
from ..n8n.client import (  # noqa: F401  # used by _state_to_response
    N8nClient,
    _connections_list_to_map,
)
from ..rag.store import COLLECTION_DETAILED, COLLECTION_DISCOVERY
from ..rag.vector_store import get_vector_store
from ..request_context import request_id_var

logger = logging.getLogger(__name__)

router = APIRouter()

# Wall-clock budget for a full /chat pipeline; overridable via
# `CHAT_REQUEST_TIMEOUT_SEC` env var (see Settings).
HEALTH_CHECK_TIMEOUT: float = 3.0


# ----------------------------------------------------------------------
# /
# ----------------------------------------------------------------------


@router.get("/")
async def root() -> dict[str, str]:
    return {"service": "n8n-workflow-builder", "version": "0.1.0"}


# ----------------------------------------------------------------------
# /health
# ----------------------------------------------------------------------


async def _probe_models_endpoint(
    base_url: str, api_key: str, expected: tuple[str, ...]
) -> dict[str, Any]:
    """GET `{base_url}/models` and verify each id in `expected` is served."""
    t0 = time.monotonic()
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as c:
            r = await c.get(url, headers=headers)
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"ok": False, "latency_ms": latency, "error": f"status {r.status_code}"}
        # Some OpenAI-compat providers list IDs with a prefix (e.g. Gemini's
        # `models/gemini-3.1-flash-lite-preview`) while users configure the
        # bare name. Accept either form.
        have: set[str] = set()
        for m in r.json().get("data") or []:
            mid = m.get("id", "")
            if mid:
                have.add(mid)
                if "/" in mid:
                    have.add(mid.rsplit("/", 1)[1])
        missing = [m for m in expected if m not in have]
        if missing:
            return {
                "ok": False,
                "latency_ms": latency,
                "error": f"missing models: {missing}",
            }
        return {"ok": True, "latency_ms": latency}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": str(exc),
        }


async def _check_openai(settings: Settings) -> dict[str, Any]:
    """Probe the LLM (and, when split, the embedding) OpenAI-compat endpoint.

    Works for vllm, OpenAI, LiteLLM, and any other server that implements the
    OpenAI models endpoint.

    R-CONF-01 / R-CONF-02 — when ``EMBED_BASE_URL`` is set to a value other
    than ``OPENAI_BASE_URL``, the embedding endpoint is probed separately
    using ``effective_embed_api_key``. The combined result is reported as a
    single ``checks.openai`` dict with an ``embed`` sub-entry; top-level
    ``ok`` is AND of both probes.
    """
    embed_url = settings.effective_embed_base_url
    split = bool(settings.embed_base_url) and embed_url != settings.openai_base_url
    if not split:
        # Shared endpoint — single probe verifies both models.
        return await _probe_models_endpoint(
            settings.openai_base_url,
            settings.openai_api_key,
            (settings.llm_model, settings.embed_model),
        )
    # Split endpoint — probe LLM and embed independently.
    llm_check, embed_check = await asyncio.gather(
        _probe_models_endpoint(
            settings.openai_base_url,
            settings.openai_api_key,
            (settings.llm_model,),
        ),
        _probe_models_endpoint(
            embed_url,
            settings.effective_embed_api_key,
            (settings.embed_model,),
        ),
    )
    combined: dict[str, Any] = {
        "ok": bool(llm_check.get("ok") and embed_check.get("ok")),
        "latency_ms": llm_check.get("latency_ms"),
        "embed": embed_check,
    }
    if not llm_check.get("ok"):
        combined["error"] = f"llm endpoint: {llm_check.get('error')}"
    elif not embed_check.get("ok"):
        combined["error"] = f"embed endpoint: {embed_check.get('error')}"
    return combined


async def _check_n8n(settings: Settings) -> dict[str, Any]:
    if not settings.n8n_api_key:
        return {"ok": False, "detail": "no api key"}
    t0 = time.monotonic()
    try:
        # Run sync client in thread so we don't block the event loop.
        def _probe() -> bool:
            with N8nClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                return client.health()

        ok = await asyncio.wait_for(asyncio.to_thread(_probe), timeout=HEALTH_CHECK_TIMEOUT + 1)
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": bool(ok), "latency_ms": latency}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": str(exc),
        }


async def _check_chroma(settings: Settings) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        def _probe() -> tuple[int, int]:
            store = get_vector_store(settings)
            return store.count(COLLECTION_DISCOVERY), store.count(COLLECTION_DETAILED)

        discovery, detailed = await asyncio.wait_for(
            asyncio.to_thread(_probe), timeout=HEALTH_CHECK_TIMEOUT + 1
        )
        latency = int((time.monotonic() - t0) * 1000)
        if discovery <= 0:
            return {
                "ok": False,
                "latency_ms": latency,
                "detail": f"discovery={discovery},detailed={detailed}",
                "error": "discovery collection empty",
            }
        return {
            "ok": True,
            "latency_ms": latency,
            "detail": f"discovery={discovery},detailed={detailed}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": str(exc),
        }


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    openai_, n8n_, chroma = await asyncio.gather(
        _check_openai(settings),
        _check_n8n(settings),
        _check_chroma(settings),
    )
    all_ok = bool(openai_.get("ok") and n8n_.get("ok") and chroma.get("ok"))
    # Return flat shape requested in deliverables as well as the `checks`
    # nested shape from C1-5 §2 — tests consume either form.
    return {
        "ok": all_ok,
        "openai": bool(openai_.get("ok")),
        "n8n": bool(n8n_.get("ok")),
        "chroma": bool(chroma.get("ok")),
        "checks": {
            "openai": openai_,
            "n8n": n8n_,
            "chroma": chroma,
        },
    }


# ----------------------------------------------------------------------
# /chat
# ----------------------------------------------------------------------


def _state_to_response(state: AgentState, settings: Settings) -> ChatResponse:
    """Convert a final AgentState into the HTTP ChatResponse.

    Retained for the ``confirm-plan`` endpoint (C1-5:HITL-SHIP-01) which still
    speaks AgentState directly. The chat-layer ``POST /chat`` flow uses
    ``_chat_turn_to_response`` instead (C1-9:CHAT-API-01).
    """
    workflow_json: dict[str, Any] | None = None
    if state.draft is not None:
        raw = state.draft.model_dump(by_alias=True, exclude_none=True)
        raw["connections"] = _connections_list_to_map(state.draft.connections)
        workflow_json = raw

    validator_failed = (
        state.validation is not None
        and not state.validation.ok
        and state.workflow_url is None
    )

    ok = state.workflow_url is not None or (
        # If n8n key isn't set, deploy is skipped and we still consider "ok"
        # when validation passed.
        not settings.n8n_api_key
        and state.validation is not None
        and state.validation.ok
    )

    errors = list(state.validation.errors) if state.validation else []

    error_message: str | None = None
    if state.error:
        error_message = state.error
    elif validator_failed:
        error_message = f"validator failed after {state.retry_count} retries"

    plan = [s.model_dump(mode="json") for s in state.plan]  # C1-5:A-RESP-01

    return ChatResponse(
        ok=ok,
        workflow_url=state.workflow_url,
        workflow_id=state.workflow_id,
        workflow_json=workflow_json,
        retry_count=state.retry_count,
        errors=errors,
        plan=plan,
        error_message=error_message,
        session_id=state.session_id,
    )


def _status_for(response: ChatResponse) -> int:
    if response.ok:
        return 200
    if response.errors:
        return 422
    return 500


# C1-9:CHAT-API-01
# C1-9:CHAT-SEC-01
def _chat_turn_to_response(turn: ChatTurnResult) -> ChatResponse:
    """Convert a dispatcher ``ChatTurnResult`` into the HTTP ChatResponse.

    REDACT_TRACE=1 clears ``tool_calls`` (CHAT-SEC-01) — note that the
    dispatcher already strips them from logs; we additionally suppress them in
    the over-the-wire response so frontends can't accidentally surface the
    args to operators in redacted environments.
    """
    redact = os.environ.get("REDACT_TRACE", "0") == "1"
    tool_calls = [] if redact else list(turn.tool_calls)

    ok = turn.status in {"chat", "awaiting_plan_approval", "deployed", "completed"}
    if turn.status == "rejected":
        # Plan rejection isn't an error per se but neither is it a success;
        # match the API spec which keeps ok=true for "the turn ran cleanly"
        # and surfaces the decision through ``status`` instead.
        ok = True
    if turn.status == "error":
        ok = False

    return ChatResponse(
        ok=ok,
        workflow_url=turn.workflow_url,
        workflow_id=turn.workflow_id,
        workflow_json=None,
        retry_count=0,
        errors=[],
        plan=list(turn.plan or []),
        error_message=turn.error_message,
        session_id=turn.session_id,
        assistant_text=turn.assistant_text,
        status=turn.status,
        tool_calls=tool_calls,
    )


# C1-9:CHAT-API-01
@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> JSONResponse:
    """Chat-layer entry point — see C1-9:CHAT-DISP-01 / CHAT-API-01.

    The handler is intentionally thin: it sets up request-id tracing,
    enforces a wall-clock budget around the (sync) dispatcher, and converts
    the resulting ``ChatTurnResult`` into the HTTP envelope. All conversation
    logic lives in ``backend.app.chat.dispatcher``.
    """
    settings = get_settings()
    rid = uuid.uuid4().hex[:8]
    rid_token = request_id_var.set(rid)
    t0 = time.monotonic()
    deploy = bool(settings.n8n_api_key)

    logger.info(
        "chat[%s] start len=%d deploy=%s session=%s",
        rid,
        len(req.message),
        deploy,
        req.session_id or "<new>",
    )

    try:
        return await _run_chat(req, settings, rid, t0, deploy)
    finally:
        request_id_var.reset(rid_token)


# C1-9:CHAT-API-01
async def _run_chat(
    req: ChatRequest,
    settings: Settings,
    rid: str,
    t0: float,
    deploy: bool,
) -> JSONResponse:
    chat_timeout = settings.chat_request_timeout_sec
    try:
        turn: ChatTurnResult = await asyncio.wait_for(
            asyncio.to_thread(
                dispatch_chat_turn,
                req.session_id,
                req.message,
                retriever=None,
                deploy_enabled=deploy,
            ),
            timeout=chat_timeout,
        )
    except TimeoutError:
        elapsed = time.monotonic() - t0
        logger.error("chat[%s] TIMEOUT elapsed=%.1fs", rid, elapsed)
        return JSONResponse(
            status_code=504,
            content={
                "ok": False,
                "status": "error",
                "session_id": req.session_id,
                "assistant_text": "",
                "tool_calls": [],
                "error_message": f"timeout after {chat_timeout:.0f}s",
                "retry_count": 0,
                "errors": [],
            },
        )
    except ValueError as exc:
        # C1-9:CHAT-SEC-01 — invalid session_id pattern from the dispatcher.
        # (Pydantic catches the schema-level pattern; this guards the
        # internal _validate_session_id path.)
        logger.warning("chat[%s] invalid session_id: %s", rid, exc)
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_session_id"},
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        logger.exception("chat[%s] FAILED elapsed=%.1fs: %s", rid, elapsed, exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "status": "error",
                "session_id": req.session_id,
                "assistant_text": "",
                "tool_calls": [],
                "error_message": "internal error",
                "retry_count": 0,
                "errors": [],
            },
        )

    elapsed = time.monotonic() - t0
    response = _chat_turn_to_response(turn)
    # C1-9:CHAT-API-01 — all dispatcher outcomes (chat / awaiting /
    # deployed / rejected / completed / error) return HTTP 200 because the
    # *turn* itself completed; the semantic outcome is carried in ``status``.
    # Only timeouts (504) and pre-dispatch validation errors escape this rule;
    # those are handled by the except blocks above.
    http_status = 200
    logger.info(
        "chat[%s] done status=%s http=%d session=%s elapsed=%.2fs",
        rid,
        turn.status,
        http_status,
        turn.session_id,
        elapsed,
    )

    return JSONResponse(
        status_code=http_status, content=response.model_dump(mode="json")
    )


# ----------------------------------------------------------------------
# C1-5:HITL-SHIP-01 — POST /chat/{session_id}/confirm-plan
# ----------------------------------------------------------------------

# P1-6: use canonical pattern from session_store to avoid drift
_SESSION_ID_PATTERN = SESSION_ID_PATTERN


# C1-5:HITL-SHIP-01
def _do_confirm_plan(
    session_id: str,
    body: ConfirmPlanRequest,
    *,
    deploy_enabled: bool = True,
) -> dict[str, Any]:
    """Shared sync logic for the confirm-plan endpoint and in-process chat tool callable.

    Returns a plain dict whose structure matches ``resume_graph_with_confirmation``
    output so the async handler can post-process it uniformly.

    Raises
    ------
    SessionNotFound
        When ``session_id`` does not correspond to any live HITL session.
    ValueError
        When ``approved=True`` and ``edited_plan`` is present but invalid
        (empty list / missing trigger step).
    """
    # Validate edited_plan if provided under approval: must be non-empty
    if body.approved and body.edited_plan is not None:
        if len(body.edited_plan) == 0:
            raise ValueError("invalid_edited_plan: edited_plan must not be empty")

    # C1-9:CHAT-TOOL-02 — pass feedback so graph writes "plan_rejected: <reason>"
    result = resume_graph_with_confirmation(
        session_id,
        body.approved,
        edited_plan=body.edited_plan,
        feedback=body.feedback,
        deploy_enabled=deploy_enabled,
    )
    return result


# C1-5:HITL-SHIP-01
@router.post("/chat/{session_id}/confirm-plan")
async def confirm_plan(
    session_id: str,
    body: ConfirmPlanRequest,
    request: Request,
) -> JSONResponse:
    """Resume the HITL graph after user plan review.

    Returns 200 (ChatResponse) on completion, 404 on unknown session,
    409 on stage mismatch, 400 on invalid edited_plan.
    See C1-5 §4.
    """
    settings = get_settings()
    deploy_enabled = bool(settings.n8n_api_key)
    chat_timeout = settings.chat_request_timeout_sec

    # session_id path param format check — invalid format is treated as 404
    # per spec security note: avoid revealing internal id structure.
    if not re.match(_SESSION_ID_PATTERN, session_id):
        return JSONResponse(
            status_code=404,
            content={"error": "session_not_found"},
        )

    try:
        result: dict[str, Any] = await asyncio.wait_for(
            asyncio.to_thread(
                _do_confirm_plan,
                session_id,
                body,
                deploy_enabled=deploy_enabled,
            ),
            timeout=chat_timeout,
        )
    except TimeoutError:
        logger.error("confirm_plan[%s] TIMEOUT", session_id)
        return JSONResponse(
            status_code=504,
            content={
                "ok": False,
                "error_message": f"timeout after {chat_timeout:.0f}s",
                "retry_count": 0,
                "errors": [],
            },
        )
    except SessionNotFound:
        logger.warning("confirm_plan[%s] session not found", session_id)
        return JSONResponse(
            status_code=404,
            content={"error": "session_not_found"},
        )
    except ValueError as exc:
        err_str = str(exc)
        logger.warning("confirm_plan[%s] invalid request: %s", session_id, err_str)
        return JSONResponse(
            status_code=400,
            content={"error": err_str},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("confirm_plan[%s] FAILED: %s", session_id, exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error_message": "internal error",
                "retry_count": 0,
                "errors": [],
            },
        )

    state: AgentState = result["state"]
    response = _state_to_response(state, settings)
    status_code = 200
    logger.info(
        "confirm_plan[%s] done status=%d approved=%s",
        session_id,
        status_code,
        body.approved,
    )
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))
