"""Unit tests for the RAG Retriever (Implements C1-2 §3).

We stub both ChromaStore and OllamaEmbedder so nothing touches disk or network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.catalog import NodeCatalogEntry, NodeDefinition
from app.rag.retriever import Retriever
from app.rag.store import COLLECTION_DETAILED, COLLECTION_DISCOVERY


class _StubEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1, 0.2, 0.3]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


class _StubStore:
    """Minimal in-memory stand-in for ChromaStore."""

    def __init__(
        self,
        query_hits: dict[str, list[dict[str, Any]]] | None = None,
        by_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._query_hits = query_hits or {}
        self._by_id = by_id or {}
        self.query_calls: list[tuple[str, int]] = []

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.query_calls.append((collection, k))
        return self._query_hits.get(collection, [])[:k]

    def get_by_ids(self, collection: str, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node_id in ids:
            row = self._by_id.get((collection, node_id))
            if row:
                out.append(row)
        return out

    def count(self, collection: str) -> int:
        return len(self._query_hits.get(collection, []))


def _discovery_hit(type_: str, display: str, category: str, desc: str, score: float) -> dict:
    doc = f"title: {display} | text: {display}\n類別: {category}\n{desc}\n關鍵字: "
    return {
        "id": type_,
        "document": doc,
        "metadata": {
            "type": type_,
            "display_name": display,
            "category": category,
            "description": desc,
        },
        "distance": 1.0 - score,
        "score": score,
    }


def _detailed_metadata(type_: str, display: str, category: str, tv: float) -> dict:
    defn = NodeDefinition(
        type=type_,
        display_name=display,
        description=f"Full def for {display}",
        category=category,
        type_version=tv,
        parameters=[],
    )
    return {
        "type": type_,
        "display_name": display,
        "category": category,
        "type_version": tv,
        "raw": json.dumps(defn.model_dump(mode="json")),
    }


# ============================================================================
# Tests
# ============================================================================


def test_search_discovery_returns_typed_entries_in_score_order() -> None:
    hits = [
        _discovery_hit("n8n-nodes-base.slack", "Slack", "Communication", "Send Slack", 0.88),
        _discovery_hit("n8n-nodes-base.gmail", "Gmail", "Communication", "Send Gmail", 0.72),
    ]
    store = _StubStore(query_hits={COLLECTION_DISCOVERY: hits})
    retriever = Retriever(store, _StubEmbedder())

    result = retriever.search_discovery("發 Slack 訊息", k=2)

    assert len(result) == 2
    assert all(isinstance(r, NodeCatalogEntry) for r in result)
    assert result[0].type == "n8n-nodes-base.slack"
    assert result[0].display_name == "Slack"
    assert result[0].category == "Communication"
    assert "Send Slack" in result[0].description
    assert result[1].type == "n8n-nodes-base.gmail"


def test_search_discovery_empty_query_returns_empty_list() -> None:
    store = _StubStore(
        query_hits={
            COLLECTION_DISCOVERY: [
                _discovery_hit("x.y", "X", "Cat", "desc", 0.9),
            ]
        }
    )
    embedder = _StubEmbedder()
    retriever = Retriever(store, embedder)

    assert retriever.search_discovery("", k=5) == []
    assert retriever.search_discovery("   ", k=5) == []
    # Should not have called embedder.
    assert embedder.calls == []


def test_search_discovery_respects_k() -> None:
    hits = [
        _discovery_hit(f"n.t{i}", f"T{i}", "Cat", "desc", 0.9 - i * 0.1) for i in range(5)
    ]
    store = _StubStore(query_hits={COLLECTION_DISCOVERY: hits})
    retriever = Retriever(store, _StubEmbedder())

    result = retriever.search_discovery("anything", k=3)

    assert len(result) == 3
    assert store.query_calls == [(COLLECTION_DISCOVERY, 3)]


def test_get_detail_returns_populated_definition() -> None:
    meta = _detailed_metadata("n8n-nodes-base.httpRequest", "HTTP Request", "Core", 4.2)
    store = _StubStore(
        by_id={
            (COLLECTION_DETAILED, "n8n-nodes-base.httpRequest"): {
                "id": "n8n-nodes-base.httpRequest",
                "document": "HTTP Request\n...",
                "metadata": meta,
            }
        }
    )
    retriever = Retriever(store, _StubEmbedder())

    defn = retriever.get_detail("n8n-nodes-base.httpRequest")

    assert defn is not None
    assert isinstance(defn, NodeDefinition)
    assert defn.type == "n8n-nodes-base.httpRequest"
    assert defn.type_version == 4.2
    assert defn.display_name == "HTTP Request"


def test_get_detail_returns_none_for_missing_type() -> None:
    store = _StubStore(by_id={})
    retriever = Retriever(store, _StubEmbedder())

    assert retriever.get_detail("n8n-nodes-base.doesNotExist") is None


def test_get_detail_returns_none_when_metadata_has_no_raw_field() -> None:
    store = _StubStore(
        by_id={
            (COLLECTION_DETAILED, "n.x"): {
                "id": "n.x",
                "document": "",
                "metadata": {"type": "n.x", "display_name": "X"},  # no 'raw'
            }
        }
    )
    retriever = Retriever(store, _StubEmbedder())

    assert retriever.get_detail("n.x") is None


def test_search_detailed_hydrates_definitions() -> None:
    hits = [
        {
            "id": "n8n-nodes-base.httpRequest",
            "document": "HTTP Request...",
            "metadata": _detailed_metadata(
                "n8n-nodes-base.httpRequest", "HTTP Request", "Core", 4.2
            ),
            "distance": 0.1,
            "score": 0.9,
        },
        {
            "id": "n8n-nodes-base.webhook",
            "document": "Webhook...",
            "metadata": _detailed_metadata(
                "n8n-nodes-base.webhook", "Webhook", "Trigger", 2.0
            ),
            "distance": 0.2,
            "score": 0.8,
        },
    ]
    store = _StubStore(query_hits={COLLECTION_DETAILED: hits})
    retriever = Retriever(store, _StubEmbedder())

    result = retriever.search_detailed("http", k=2)

    assert len(result) == 2
    assert all(isinstance(r, NodeDefinition) for r in result)
    assert result[0].type == "n8n-nodes-base.httpRequest"
    assert result[1].type == "n8n-nodes-base.webhook"


def test_search_detailed_skips_hits_with_corrupt_raw() -> None:
    good = _detailed_metadata("n.good", "Good", "Cat", 1.0)
    bad = {"type": "n.bad", "display_name": "Bad", "category": "Cat", "type_version": 1.0,
           "raw": "{not valid json"}
    hits = [
        {"id": "n.bad", "document": "", "metadata": bad, "distance": 0.1, "score": 0.9},
        {"id": "n.good", "document": "", "metadata": good, "distance": 0.2, "score": 0.8},
    ]
    store = _StubStore(query_hits={COLLECTION_DETAILED: hits})
    retriever = Retriever(store, _StubEmbedder())

    result = retriever.search_detailed("anything", k=2)

    assert len(result) == 1
    assert result[0].type == "n.good"


def test_search_detailed_empty_query_returns_empty_list() -> None:
    store = _StubStore()
    embedder = _StubEmbedder()
    retriever = Retriever(store, embedder)

    assert retriever.search_detailed("") == []
    assert embedder.calls == []
