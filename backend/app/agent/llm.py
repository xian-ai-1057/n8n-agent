"""ChatOpenAI factory (Implements C1-1 + D0-3 §2.1).

Centralises LLM construction so callers can patch a single place in tests.
Targets any OpenAI-compatible endpoint — OpenAI itself, vllm's
`--served-model-name` server, LiteLLM, etc.

Per-stage model / temperature overrides come from `Settings.model_for(stage)`
and `Settings.temperature_for(stage)`. A caller either:
- passes `stage="planner" | "builder" | "fix" | "critic"` to pick up the
  matching env-driven overrides, or
- passes explicit `model` / `temperature` / `timeout` kwargs, which win over
  everything else (used by tests).
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from ..config import get_settings


class LLMTimeoutError(TimeoutError):
    """Raised when a structured LLM call exceeds its timeout budget."""


def _base_chat(
    *,
    stage: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
) -> ChatOpenAI:
    settings = get_settings()
    resolved_model = model if model is not None else settings.model_for(stage or "")
    resolved_temp = (
        temperature if temperature is not None else settings.temperature_for(stage or "")
    )
    resolved_timeout = timeout if timeout is not None else settings.llm_timeout_sec
    return ChatOpenAI(
        model=resolved_model,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        temperature=resolved_temp,
        timeout=resolved_timeout,
    )


def get_llm(
    schema: type[BaseModel],
    *,
    stage: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
) -> Any:
    """Return a ChatOpenAI bound to structured output for `schema`.

    Uses `method="json_schema"` — OpenAI's native Structured Outputs, which
    vllm also implements via `response_format={"type": "json_schema", ...}`.
    Do NOT switch to `method="function_calling"` — not every vllm-hosted
    model supports tool calling, but all of them accept JSON-schema guided
    decoding.

    Pass `stage=` to let env vars pick the model/temperature for that stage.
    """
    chat = _base_chat(stage=stage, model=model, temperature=temperature, timeout=timeout)
    return chat.with_structured_output(schema, method="json_schema")


def get_unstructured_llm(
    *,
    stage: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
) -> ChatOpenAI:
    """Plain ChatOpenAI for fallbacks / free-form completions."""
    return _base_chat(stage=stage, model=model, temperature=temperature, timeout=timeout)


def invoke_with_timeout(
    llm: Any,
    prompt: Any,
    *,
    timeout: float | None = None,
) -> Any:
    """Invoke a (structured) LLM, translating HTTP-level timeouts.

    Timeout is enforced at the HTTP client layer (configured on the
    ``ChatOpenAI`` instance), so a stalled request is actually cancelled at
    the socket — no background threads accumulate. This function just
    normalises the timeout exception type so callers can ``except
    LLMTimeoutError`` and trigger the graph's retry path.

    The ``timeout`` kwarg is retained for API compatibility with callers, but
    the effective budget is the one baked into the LLM client.
    """
    del timeout  # enforced by the underlying ChatOpenAI client
    try:
        return llm.invoke(prompt)
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError(f"LLM invoke timed out: {exc}") from exc
    except TimeoutError as exc:
        raise LLMTimeoutError(f"LLM invoke timed out: {exc}") from exc
