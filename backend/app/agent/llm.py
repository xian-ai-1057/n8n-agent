"""ChatOpenAI factory (Implements C1-1).

Centralises LLM construction so callers can patch a single place in tests.
Targets any OpenAI-compatible endpoint — OpenAI itself, vllm's
`--served-model-name` server, LiteLLM, etc.
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from ..config import get_settings

DEFAULT_TEMPERATURE: float = 0.2
DEFAULT_INVOKE_TIMEOUT_SEC: float = 180.0


class LLMTimeoutError(TimeoutError):
    """Raised when a structured LLM call exceeds its timeout budget."""


def _base_chat(
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_INVOKE_TIMEOUT_SEC,
) -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        temperature=temperature,
        timeout=timeout,
    )


def get_llm(
    schema: type[BaseModel],
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_INVOKE_TIMEOUT_SEC,
) -> Any:
    """Return a ChatOpenAI bound to structured output for `schema`.

    Uses `method="json_schema"` — OpenAI's native Structured Outputs, which
    vllm also implements via `response_format={"type": "json_schema", ...}`.
    Do NOT switch to `method="function_calling"` — not every vllm-hosted
    model supports tool calling, but all of them accept JSON-schema guided
    decoding.
    """
    chat = _base_chat(temperature=temperature, timeout=timeout)
    return chat.with_structured_output(schema, method="json_schema")


def get_unstructured_llm(
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_INVOKE_TIMEOUT_SEC,
) -> ChatOpenAI:
    """Plain ChatOpenAI for fallbacks / free-form completions."""
    return _base_chat(temperature=temperature, timeout=timeout)


def invoke_with_timeout(
    llm: Any,
    prompt: Any,
    *,
    timeout: float = DEFAULT_INVOKE_TIMEOUT_SEC,
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
