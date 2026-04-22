"""Retriever — caller-facing RAG API (Implements C1-2 §3).

Returns typed Pydantic objects only; never leaks raw Chroma dicts.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import get_settings
from app.models.catalog import NodeCatalogEntry, NodeDefinition
from app.rag.embedder import OpenAIEmbedder
from app.rag.store import COLLECTION_DETAILED, COLLECTION_DISCOVERY
from app.rag.vector_store import VectorStore


class Retriever:
    """High-level query surface over the two RAG collections.

    Default `k` values come from `Settings.rag_discovery_k` and
    `Settings.rag_detailed_k`. Callers can still override per-call.
    """

    def __init__(self, store: VectorStore, embedder: OpenAIEmbedder) -> None:
        self._store = store
        self._embedder = embedder

    # ----- discovery -------------------------------------------------------

    def search_discovery(
        self, query: str, k: int | None = None
    ) -> list[NodeCatalogEntry]:
        """Planner-facing: semantic top-k over `catalog_discovery`.

        Score = 1 - cosine_distance. Results are already ordered descending by
        score (Chroma returns ascending distance).
        """
        if k is None:
            k = get_settings().rag_discovery_k
        if not query.strip():
            return []
        embedding = self._embedder.embed(query)
        hits = self._store.query(COLLECTION_DISCOVERY, embedding, k=k)
        out: list[NodeCatalogEntry] = []
        for hit in hits:
            meta: dict[str, Any] = hit["metadata"]
            out.append(
                NodeCatalogEntry(
                    type=meta.get("type", hit["id"]),
                    display_name=meta.get("display_name", ""),
                    category=meta.get("category", ""),
                    description=meta.get("description") or _extract_description_from_doc(
                        hit.get("document", "")
                    ),
                    default_type_version=None,
                    has_detail=bool(meta.get("has_detail", False)),
                )
            )
        return out

    # ----- detailed --------------------------------------------------------

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        """Builder-facing: exact lookup by `type`. Returns None if not indexed."""
        rows = self._store.get_by_ids(COLLECTION_DETAILED, [node_type])
        if not rows:
            return None
        return _hydrate_definition(rows[0].get("metadata") or {})

    def search_detailed(
        self, query: str, k: int | None = None
    ) -> list[NodeDefinition]:
        """Fallback semantic search over the detailed index."""
        if k is None:
            k = get_settings().rag_detailed_k
        if not query.strip():
            return []
        embedding = self._embedder.embed(query)
        hits = self._store.query(COLLECTION_DETAILED, embedding, k=k)
        out: list[NodeDefinition] = []
        for hit in hits:
            hydrated = _hydrate_definition(hit.get("metadata") or {})
            if hydrated is not None:
                out.append(hydrated)
        return out


# ---------- helpers --------------------------------------------------------


def _extract_description_from_doc(doc: str) -> str:
    """Pull the description line back out of the discovery document string.

    Discovery doc shape:
        <display_name>\n類別: <category>\n<description>\n關鍵字: ...
    """
    if not doc:
        return ""
    lines = doc.split("\n")
    # line 0 = display_name, line 1 = 類別: ..., line 2 = description.
    if len(lines) >= 3:
        return lines[2]
    return ""


def _hydrate_definition(metadata: dict[str, Any]) -> NodeDefinition | None:
    raw_json = metadata.get("raw")
    if not raw_json:
        return None
    try:
        return NodeDefinition.model_validate(json.loads(raw_json))
    except (json.JSONDecodeError, ValueError):
        return None
