"""Unit tests for embedding prompt profile routing (Implements C1-2 §7)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.rag.embedder import (
    OpenAIEmbedder,
    VALID_PROFILES,
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
