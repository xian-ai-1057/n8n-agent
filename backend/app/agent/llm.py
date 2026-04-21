"""ChatOllama factory (Implements C1-1).

Centralises LLM construction so callers can patch a single place in tests.
"""

from __future__ import annotations

import threading
from typing import Any

from langchain_ollama import ChatOllama
from pydantic import BaseModel

from ..config import get_settings

DEFAULT_TEMPERATURE: float = 0.2
DEFAULT_INVOKE_TIMEOUT_SEC: float = 180.0


class LLMTimeoutError(TimeoutError):
    """Raised when a structured LLM call exceeds its timeout budget."""


def _base_chat(*, temperature: float = DEFAULT_TEMPERATURE) -> ChatOllama:
    settings = get_settings()
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=temperature,
    )


def get_llm(
    schema: type[BaseModel],
    *,
    temperature: float = DEFAULT_TEMPERATURE,
) -> Any:
    """Return a ChatOllama bound to structured output for `schema`.

    Uses `method="json_schema"` — measured 17s on qwen3:8b with the real
    BuilderOutput schema. `function_calling` returned None on local thinking
    models (qwen3/qwen3.5 both have the `thinking` capability).
    Do NOT switch to `format="json"` (weaker schema guarantee).
    """
    chat = _base_chat(temperature=temperature)
    return chat.with_structured_output(schema, method="json_schema")


def get_unstructured_llm(*, temperature: float = DEFAULT_TEMPERATURE) -> ChatOllama:
    """Plain ChatOllama for fallbacks / free-form completions."""
    return _base_chat(temperature=temperature)


def invoke_with_timeout(
    llm: Any,
    prompt: Any,
    *,
    timeout: float = DEFAULT_INVOKE_TIMEOUT_SEC,
) -> Any:
    """Invoke a (structured) LLM with a hard wall-clock timeout.

    Why hand-rolled instead of `concurrent.futures.ThreadPoolExecutor`:
    ThreadPoolExecutor's workers are non-daemon, and interpreter shutdown
    hooks wait for them — a stalled `ChatOllama.invoke` would then block
    `python` from exiting. We use a raw daemon `threading.Thread` so:
      1. Result via shared container + Event.
      2. If the worker overruns, we give up on it — the daemon thread dies
         when the process exits. Socket cleanup is delegated to the OS.
    Caller must surface the `LLMTimeoutError` as a recoverable failure
    (e.g. empty nodes → validator error) so the graph's retry path runs.
    """
    container: dict[str, Any] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            container["result"] = llm.invoke(prompt)
        except BaseException as exc:  # noqa: BLE001
            container["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(
        target=_target, name="llm-invoke", daemon=True
    )
    worker.start()
    finished = done.wait(timeout=timeout)
    if not finished:
        raise LLMTimeoutError(f"LLM invoke exceeded {timeout:.0f}s")
    if "error" in container:
        raise container["error"]
    return container["result"]
