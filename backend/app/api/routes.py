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
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..agent.graph import run_cli
from ..config import get_settings
from ..models.agent_state import AgentState
from ..models.api import ChatRequest, ChatResponse
from ..request_context import request_id_var
from ..n8n.client import N8nClient, _connections_list_to_map
from ..n8n.errors import (
    N8nAuthError,
    N8nBadRequestError,
    N8nServerError,
    N8nUnavailable,
)
from ..rag.store import COLLECTION_DETAILED, COLLECTION_DISCOVERY, ChromaStore

logger = logging.getLogger(__name__)

router = APIRouter()

CHAT_TIMEOUT_SECONDS: float = 180.0
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


async def _check_openai(settings) -> dict[str, Any]:
    """Probe the OpenAI-compatible endpoint's `GET /models`.

    Works for vllm, OpenAI, LiteLLM, and any other server that implements the
    OpenAI models endpoint. Confirms both chat + embedding models are served.
    """
    t0 = time.monotonic()
    url = f"{settings.openai_base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as c:
            r = await c.get(url, headers=headers)
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"ok": False, "latency_ms": latency, "error": f"status {r.status_code}"}
        models = r.json().get("data") or []
        have = {m.get("id", "") for m in models}
        missing = [
            m for m in (settings.llm_model, settings.embed_model)
            if m not in have
        ]
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


async def _check_n8n(settings) -> dict[str, Any]:
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


async def _check_chroma(settings) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        def _probe() -> tuple[int, int]:
            store = ChromaStore(settings.chroma_path)
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


def _state_to_response(state: AgentState, settings) -> ChatResponse:
    """Convert a final AgentState into the HTTP ChatResponse."""
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

    return ChatResponse(
        ok=ok,
        workflow_url=state.workflow_url,
        workflow_id=state.workflow_id,
        workflow_json=workflow_json,
        retry_count=state.retry_count,
        errors=errors,
        error_message=error_message,
    )


def _status_for(response: ChatResponse) -> int:
    if response.ok:
        return 200
    if response.errors:
        return 422
    return 500


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> JSONResponse:
    settings = get_settings()
    rid = uuid.uuid4().hex[:8]
    rid_token = request_id_var.set(rid)
    t0 = time.monotonic()
    deploy = bool(settings.n8n_api_key)

    logger.info(
        "chat[%s] start len=%d deploy=%s", rid, len(req.message), deploy
    )

    try:
        return await _run_chat(req, settings, rid, t0, deploy)
    finally:
        request_id_var.reset(rid_token)


async def _run_chat(
    req: ChatRequest,
    settings,
    rid: str,
    t0: float,
    deploy: bool,
) -> JSONResponse:
    try:
        state: AgentState = await asyncio.wait_for(
            asyncio.to_thread(run_cli, req.message, deploy=deploy),
            timeout=CHAT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        logger.error("chat[%s] TIMEOUT elapsed=%.1fs", rid, elapsed)
        return JSONResponse(
            status_code=504,
            content={
                "ok": False,
                "error_message": f"timeout after {CHAT_TIMEOUT_SECONDS:.0f}s",
                "retry_count": 0,
                "errors": [],
            },
        )
    except N8nAuthError as exc:
        logger.error("chat[%s] n8n auth failed: %s", rid, exc)
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error_message": "n8n auth failed",
                "retry_count": 0,
                "errors": [],
            },
        )
    except N8nBadRequestError as exc:
        logger.error("chat[%s] n8n 400: %s", rid, exc)
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error_message": f"n8n rejected payload: {exc.detail}",
                "retry_count": 0,
                "errors": [],
            },
        )
    except N8nUnavailable as exc:
        logger.error("chat[%s] n8n unavailable: %s", rid, exc)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_message": f"upstream unavailable: n8n ({exc})",
                "retry_count": 0,
                "errors": [],
            },
        )
    except N8nServerError as exc:
        logger.error("chat[%s] n8n %s: %s", rid, exc.status_code, exc)
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error_message": f"n8n upstream error {exc.status_code}",
                "retry_count": 0,
                "errors": [],
            },
        )
    except ValueError as exc:
        logger.error("chat[%s] bad payload: %s", rid, exc)
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error_message": str(exc),
                "retry_count": 0,
                "errors": [],
            },
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        logger.exception("chat[%s] FAILED elapsed=%.1fs: %s", rid, elapsed, exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error_message": "internal error",
                "retry_count": 0,
                "errors": [],
            },
        )

    elapsed = time.monotonic() - t0
    response = _state_to_response(state, settings)
    status = _status_for(response)
    stages = []
    if state.plan:
        stages.append(f"plan({len(state.plan)})")
    if state.built_nodes:
        stages.append(f"build({len(state.built_nodes)})")
    if state.draft is not None:
        stages.append("assemble")
    if state.validation is not None:
        stages.append("validate" + ("_ok" if state.validation.ok else "_fail"))
    if state.workflow_url:
        stages.append("deploy")
    logger.info(
        "chat[%s] done status=%d stages=%s elapsed=%.1fs retry=%d",
        rid,
        status,
        ",".join(stages),
        elapsed,
        state.retry_count,
    )

    return JSONResponse(status_code=status, content=response.model_dump(mode="json"))
