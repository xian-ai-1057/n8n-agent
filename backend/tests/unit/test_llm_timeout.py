"""Tests for `invoke_with_timeout` (C1-1: builder/planner stall guard).

The timeout itself is enforced by the underlying ``ChatOpenAI`` HTTP client;
``invoke_with_timeout`` only translates the HTTP timeout exception type into
the internal ``LLMTimeoutError`` the graph's retry path expects.
"""

from __future__ import annotations

import httpx
import pytest

from app.agent.llm import LLMTimeoutError, invoke_with_timeout


class _FakeLLM:
    def __init__(self, result: object = "ok", exc: Exception | None = None):
        self._result = result
        self._exc = exc

    def invoke(self, _prompt):  # noqa: ANN001
        if self._exc is not None:
            raise self._exc
        return self._result


def test_invoke_returns_result():
    assert invoke_with_timeout(_FakeLLM(result={"k": 1}), "x") == {"k": 1}


def test_invoke_translates_httpx_timeout_to_llm_timeout():
    boom = httpx.ReadTimeout("slow upstream")
    with pytest.raises(LLMTimeoutError):
        invoke_with_timeout(_FakeLLM(exc=boom), "x")


def test_invoke_translates_builtin_timeout_error():
    with pytest.raises(LLMTimeoutError):
        invoke_with_timeout(_FakeLLM(exc=TimeoutError("budget exceeded")), "x")


def test_invoke_propagates_underlying_exception():
    boom = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        invoke_with_timeout(_FakeLLM(exc=boom), "x")
