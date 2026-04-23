"""Unit tests for embedding prompt profile routing (Implements C1-2 §7)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.rag.embedder import (
    VALID_PROFILES,
    OpenAIEmbedder,
    _resolve_profile,
    _wrap_document,
    _wrap_query,
)

# ---- _resolve_profile ------------------------------------------------------


@pytest.mark.parametrize(
    "profile, model, expected",
    [
        # auto → infer
        ("auto", "BAAI/bge-m3", "bge"),
        ("auto", "baai/bge-large-en-v1.5", "bge"),
        ("auto", "google/embeddinggemma-300m", "embeddinggemma"),
        ("auto", "google/gemma-embedding", "embeddinggemma"),
        ("auto", "text-embedding-3-small", "openai"),
        ("auto", "text-embedding-ada-002", "openai"),
        ("auto", "custom/whatever", "none"),
        # explicit pass-through
        ("bge", "BAAI/bge-m3", "bge"),
        ("embeddinggemma", "BAAI/bge-m3", "embeddinggemma"),
        ("openai", "text-embedding-3-small", "openai"),
        ("none", "anything", "none"),
    ],
)
def test_resolve_profile(profile: str, model: str, expected: str) -> None:
    assert _resolve_profile(profile, model) == expected


# ---- _wrap_query / _wrap_document -----------------------------------------


def test_wrap_query_embeddinggemma_adds_prompt() -> None:
    assert _wrap_query("hello", "embeddinggemma") == (
        "task: search result | query: hello"
    )


@pytest.mark.parametrize("profile", ["bge", "openai", "none"])
def test_wrap_query_identity_for_non_gemma(profile: str) -> None:
    assert _wrap_query("hello", profile) == "hello"


def test_wrap_document_embeddinggemma_splits_first_line_as_title() -> None:
    body = "Slack\n類別: Communication\n發訊息\n關鍵字: slack, message"
    wrapped = _wrap_document(body, "embeddinggemma")
    assert wrapped.startswith("title: Slack | text: Slack\n")
    assert wrapped.endswith("關鍵字: slack, message")


def test_wrap_document_embeddinggemma_single_line_fallback() -> None:
    assert _wrap_document("Slack", "embeddinggemma") == "title: Slack | text: Slack"


@pytest.mark.parametrize("profile", ["bge", "openai", "none"])
def test_wrap_document_identity_for_non_gemma(profile: str) -> None:
    body = "Slack\n描述"
    assert _wrap_document(body, profile) == body


# ---- OpenAIEmbedder profile wiring ----------------------------------------


@patch("app.rag.embedder.OpenAIEmbeddings")
def test_embedder_reads_profile_from_settings(mock_embeddings) -> None:
    embedder = OpenAIEmbedder(
        model="BAAI/bge-m3",
        base_url="http://x/v1",
        api_key="k",
        profile="auto",
    )
    assert embedder.profile == "bge"


@patch("app.rag.embedder.OpenAIEmbeddings")
def test_embedder_rejects_unknown_profile(mock_embeddings) -> None:
    with pytest.raises(ValueError, match="Unknown EMBED_PROMPT_PROFILE"):
        OpenAIEmbedder(
            model="x",
            base_url="http://x/v1",
            api_key="k",
            profile="bogus",
        )


@patch("app.rag.embedder.OpenAIEmbeddings")
def test_embed_applies_query_wrapper(mock_embeddings) -> None:
    fake = mock_embeddings.return_value
    fake.embed_query.return_value = [0.1, 0.2]
    embedder = OpenAIEmbedder(
        model="google/embeddinggemma-300m",
        base_url="http://x/v1",
        api_key="k",
        profile="auto",
    )
    embedder.embed("發訊息")
    fake.embed_query.assert_called_once_with("task: search result | query: 發訊息")


@patch("app.rag.embedder.OpenAIEmbeddings")
def test_embed_batch_applies_document_wrapper(mock_embeddings) -> None:
    fake = mock_embeddings.return_value
    fake.embed_documents.return_value = [[0.1], [0.2]]
    embedder = OpenAIEmbedder(
        model="google/embeddinggemma-300m",
        base_url="http://x/v1",
        api_key="k",
        profile="auto",
    )
    embedder.embed_batch(["Slack\nbody", "If\nbody"])
    fake.embed_documents.assert_called_once_with(
        ["title: Slack | text: Slack\nbody", "title: If | text: If\nbody"]
    )


@patch("app.rag.embedder.OpenAIEmbeddings")
def test_embed_batch_identity_for_bge(mock_embeddings) -> None:
    fake = mock_embeddings.return_value
    fake.embed_documents.return_value = [[0.1]]
    embedder = OpenAIEmbedder(
        model="BAAI/bge-m3",
        base_url="http://x/v1",
        api_key="k",
        profile="auto",
    )
    embedder.embed_batch(["Slack\nbody"])
    fake.embed_documents.assert_called_once_with(["Slack\nbody"])


@patch("app.rag.embedder.OpenAIEmbeddings")
def test_embed_batch_empty_returns_empty(mock_embeddings) -> None:
    fake = mock_embeddings.return_value
    embedder = OpenAIEmbedder(
        model="BAAI/bge-m3", base_url="http://x/v1", api_key="k", profile="none"
    )
    assert embedder.embed_batch([]) == []
    fake.embed_documents.assert_not_called()


def test_valid_profiles_contract() -> None:
    assert VALID_PROFILES == frozenset(
        {"auto", "embeddinggemma", "bge", "openai", "none"}
    )


# ---- R-CONF-01: embed_base_url fallback logic ------------------------------


def test_r_conf_01_effective_embed_base_url_falls_back_to_openai_base_url() -> None:
    """When embed_base_url is empty, effective_embed_base_url returns openai_base_url."""
    from app.config import Settings

    s = Settings(openai_base_url="http://llm:8000/v1", embed_base_url="")
    assert s.effective_embed_base_url == "http://llm:8000/v1"


def test_r_conf_01_effective_embed_base_url_uses_dedicated_url_when_set() -> None:
    """When embed_base_url is set, effective_embed_base_url returns it, not openai_base_url."""
    from app.config import Settings

    s = Settings(
        openai_base_url="http://llm:8000/v1",
        embed_base_url="http://ollama:11434/v1",
    )
    assert s.effective_embed_base_url == "http://ollama:11434/v1"


@patch("app.rag.embedder.get_settings")
@patch("app.rag.embedder.OpenAIEmbeddings")
def test_r_conf_01_embedder_uses_effective_embed_base_url_from_settings(
    mock_embeddings,
    mock_settings,
) -> None:
    """OpenAIEmbedder falls back to effective_embed_base_url when no explicit base_url given."""
    mock_settings.return_value.embed_model = "BAAI/bge-m3"
    mock_settings.return_value.effective_embed_base_url = "http://ollama:11434/v1"
    mock_settings.return_value.openai_api_key = "EMPTY"
    mock_settings.return_value.embed_prompt_profile = "none"

    embedder = OpenAIEmbedder()
    assert embedder.base_url == "http://ollama:11434/v1"


# C1-2:R-CONF-01 — scenario 4b: no-arg embedder picks up fallback URL (embed_base_url empty)
@patch("app.rag.embedder.OpenAIEmbeddings")
def test_r_conf_01_embedder_no_args_falls_back_to_openai_base_url(
    mock_embeddings,
) -> None:
    """OpenAIEmbedder() with no args uses openai_base_url when embed_base_url is empty."""
    from unittest.mock import patch as _patch

    with _patch("app.rag.embedder.get_settings") as mock_settings:
        # embed_base_url is empty → effective_embed_base_url == openai_base_url
        mock_settings.return_value.embed_model = "BAAI/bge-m3"
        mock_settings.return_value.effective_embed_base_url = "http://llm:8000/v1"
        mock_settings.return_value.openai_api_key = "EMPTY"
        mock_settings.return_value.embed_prompt_profile = "none"

        embedder = OpenAIEmbedder()
        assert embedder.base_url == "http://llm:8000/v1"


# C1-2:R-CONF-01 — scenario 5: explicit base_url arg wins over settings
@patch("app.rag.embedder.OpenAIEmbeddings")
def test_r_conf_01_embedder_explicit_base_url_overrides_settings(
    mock_embeddings,
) -> None:
    """When base_url is passed explicitly to OpenAIEmbedder, it takes precedence over settings."""
    from unittest.mock import patch as _patch

    with _patch("app.rag.embedder.get_settings") as mock_settings:
        mock_settings.return_value.embed_model = "BAAI/bge-m3"
        mock_settings.return_value.effective_embed_base_url = "http://ollama:11434/v1"
        mock_settings.return_value.openai_api_key = "EMPTY"
        mock_settings.return_value.embed_prompt_profile = "none"

        embedder = OpenAIEmbedder(base_url="http://override:9999/v1")
        assert embedder.base_url == "http://override:9999/v1"


# C1-2:R-CONF-01 — scenario 6: LLM (ChatOpenAI) base_url remains openai_base_url
# when embed_base_url is set to a different value (asymmetry guard)
def test_r_conf_01_llm_base_url_unaffected_by_embed_base_url() -> None:
    """Setting embed_base_url must not change the base_url used by ChatOpenAI in llm.py."""
    from unittest.mock import MagicMock, patch as _patch

    with _patch("app.agent.llm.get_settings") as mock_settings, \
         _patch("app.agent.llm.ChatOpenAI") as mock_chat:
        mock_settings.return_value.openai_base_url = "http://llm:8000/v1"
        mock_settings.return_value.openai_api_key = "EMPTY"
        mock_settings.return_value.llm_timeout_sec = 60.0
        mock_settings.return_value.model_for.return_value = "Qwen/Qwen2.5-7B-Instruct"
        mock_settings.return_value.temperature_for.return_value = 0.2
        # embed_base_url set to a completely different host
        mock_settings.return_value.embed_base_url = "http://ollama:11434/v1"
        mock_settings.return_value.effective_embed_base_url = "http://ollama:11434/v1"

        mock_chat.return_value = MagicMock()

        from app.agent.llm import _base_chat
        _base_chat()

        # ChatOpenAI must have been called with openai_base_url, not embed_base_url
        call_kwargs = mock_chat.call_args.kwargs
        assert call_kwargs["base_url"] == "http://llm:8000/v1"
        assert call_kwargs["base_url"] != "http://ollama:11434/v1"
