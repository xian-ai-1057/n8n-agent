"""Unit tests for _check_openai split-endpoint behaviour (R-CONF-01 / R-CONF-02).

When EMBED_BASE_URL is set to a value other than OPENAI_BASE_URL, the health
check must probe both endpoints independently and use effective_embed_api_key
for the embed-side Authorization header.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from app.api import routes as routes_mod


def _make_settings(
    *,
    openai_base_url: str = "http://llm:8000/v1",
    openai_api_key: str = "vllm-key",
    embed_base_url: str = "",
    embed_api_key: str = "",
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct",
    embed_model: str = "BAAI/bge-m3",
) -> SimpleNamespace:
    """Minimal settings stub exposing the fields _check_openai reads."""
    effective_embed_base_url = embed_base_url or openai_base_url
    effective_embed_api_key = embed_api_key or openai_api_key
    return SimpleNamespace(
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        embed_base_url=embed_base_url,
        embed_api_key=embed_api_key,
        effective_embed_base_url=effective_embed_base_url,
        effective_embed_api_key=effective_embed_api_key,
        llm_model=llm_model,
        embed_model=embed_model,
    )


def test_check_openai_shared_endpoint_runs_one_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When embed_base_url is empty, only one /models probe is made."""
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def _fake_probe(base_url: str, api_key: str, expected: tuple[str, ...]) -> dict[str, Any]:
        calls.append((base_url, api_key, expected))
        return {"ok": True, "latency_ms": 1}

    monkeypatch.setattr(routes_mod, "_probe_models_endpoint", _fake_probe)

    result = asyncio.run(routes_mod._check_openai(_make_settings()))

    assert result == {"ok": True, "latency_ms": 1}
    assert len(calls) == 1
    base_url, api_key, expected = calls[0]
    assert base_url == "http://llm:8000/v1"
    assert api_key == "vllm-key"
    # Shared endpoint verifies both LLM + embed model in a single call.
    assert set(expected) == {"Qwen/Qwen2.5-7B-Instruct", "BAAI/bge-m3"}


def test_check_openai_split_endpoint_runs_two_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When embed_base_url is set and differs, both endpoints are probed."""
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def _fake_probe(base_url: str, api_key: str, expected: tuple[str, ...]) -> dict[str, Any]:
        calls.append((base_url, api_key, expected))
        return {"ok": True, "latency_ms": 2}

    monkeypatch.setattr(routes_mod, "_probe_models_endpoint", _fake_probe)

    settings = _make_settings(
        embed_base_url="http://ollama:11434/v1",
        embed_api_key="sk-embed-only",
    )
    result = asyncio.run(routes_mod._check_openai(settings))

    assert result["ok"] is True
    assert "embed" in result
    assert result["embed"] == {"ok": True, "latency_ms": 2}
    assert len(calls) == 2
    by_url = {c[0]: c for c in calls}
    llm_call = by_url["http://llm:8000/v1"]
    embed_call = by_url["http://ollama:11434/v1"]
    # LLM probe uses openai_api_key, embed probe uses effective_embed_api_key.
    assert llm_call[1] == "vllm-key"
    assert llm_call[2] == ("Qwen/Qwen2.5-7B-Instruct",)
    assert embed_call[1] == "sk-embed-only"
    assert embed_call[2] == ("BAAI/bge-m3",)


def test_check_openai_split_embed_failure_marks_overall_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only the embed endpoint fails, top-level ok is False and error names embed."""

    async def _fake_probe(base_url: str, api_key: str, expected: tuple[str, ...]) -> dict[str, Any]:
        if "ollama" in base_url:
            return {"ok": False, "latency_ms": 3, "error": "status 503"}
        return {"ok": True, "latency_ms": 1}

    monkeypatch.setattr(routes_mod, "_probe_models_endpoint", _fake_probe)

    settings = _make_settings(embed_base_url="http://ollama:11434/v1")
    result = asyncio.run(routes_mod._check_openai(settings))

    assert result["ok"] is False
    assert "embed endpoint" in result["error"]
    assert result["embed"]["ok"] is False


def test_check_openai_split_llm_failure_marks_overall_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only the LLM endpoint fails, top-level ok is False and error names llm."""

    async def _fake_probe(base_url: str, api_key: str, expected: tuple[str, ...]) -> dict[str, Any]:
        if "llm" in base_url:
            return {"ok": False, "latency_ms": 3, "error": "status 500"}
        return {"ok": True, "latency_ms": 1}

    monkeypatch.setattr(routes_mod, "_probe_models_endpoint", _fake_probe)

    settings = _make_settings(embed_base_url="http://ollama:11434/v1")
    result = asyncio.run(routes_mod._check_openai(settings))

    assert result["ok"] is False
    assert "llm endpoint" in result["error"]


# ----------------------------------------------------------------------
# _probe_models_endpoint — provider prefix tolerance
# ----------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._payload)


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(
        routes_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(payload)
    )


def test_probe_models_endpoint_accepts_provider_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini's /models lists IDs as `models/<name>`; bare configured name must still match."""
    payload = {
        "data": [
            {"id": "models/gemini-3.1-flash-lite-preview"},
            {"id": "models/embeddinggemma:latest"},
        ]
    }
    _patch_httpx(monkeypatch, payload)

    result = asyncio.run(
        routes_mod._probe_models_endpoint(
            "https://gemini.example/v1",
            "key",
            ("gemini-3.1-flash-lite-preview", "embeddinggemma:latest"),
        )
    )
    assert result["ok"] is True


def test_probe_models_endpoint_accepts_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ID match (no prefix) still works for vLLM/OpenAI-style providers."""
    payload = {"data": [{"id": "Qwen/Qwen2.5-7B-Instruct"}]}
    _patch_httpx(monkeypatch, payload)

    result = asyncio.run(
        routes_mod._probe_models_endpoint(
            "http://llm:8000/v1", "k", ("Qwen/Qwen2.5-7B-Instruct",)
        )
    )
    assert result["ok"] is True


def test_probe_models_endpoint_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model neither listed exactly nor as the suffix of a prefixed ID is reported missing."""
    payload = {"data": [{"id": "models/gemini-3.1-flash-lite-preview"}]}
    _patch_httpx(monkeypatch, payload)

    result = asyncio.run(
        routes_mod._probe_models_endpoint(
            "https://gemini.example/v1", "key", ("nonexistent-model",)
        )
    )
    assert result["ok"] is False
    assert "missing models" in result["error"]
