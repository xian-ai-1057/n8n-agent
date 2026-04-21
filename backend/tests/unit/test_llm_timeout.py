"""Tests for `invoke_with_timeout` (C1-1: builder/planner stall guard)."""

from __future__ import annotations

import time

import pytest

from app.agent.llm import LLMTimeoutError, invoke_with_timeout


class _FakeLLM:
    def __init__(self, sleep_s: float, result: object = "ok", exc: Exception | None = None):
        self._sleep = sleep_s
        self._result = result
        self._exc = exc

    def invoke(self, _prompt):  # noqa: ANN001
        time.sleep(self._sleep)
        if self._exc is not None:
            raise self._exc
        return self._result


def test_invoke_returns_result_within_budget():
    assert invoke_with_timeout(_FakeLLM(0.01, result={"k": 1}), "x", timeout=1.0) == {"k": 1}


def test_invoke_raises_timeout_when_overrun():
    with pytest.raises(LLMTimeoutError):
        invoke_with_timeout(_FakeLLM(2.0), "x", timeout=0.1)


def test_invoke_propagates_underlying_exception():
    boom = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        invoke_with_timeout(_FakeLLM(0.01, exc=boom), "x", timeout=1.0)
