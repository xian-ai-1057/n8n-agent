"""RAG layer (Implements C1-2).

Dual-index retrieval over ChromaDB:
- `catalog_discovery` — all 500+ node entries (from xlsx) for Planner broad search.
- `catalog_detailed` — the curated ~30 nodes with full parameter schemas for Builder.

Public surface:
- `ChromaStore` — thin wrapper over a single `PersistentClient`.
- `OpenAIEmbedder` — wraps `langchain_openai.OpenAIEmbeddings`.
- `Retriever` — the only caller-facing entry point; returns typed Pydantic objects.
"""

from __future__ import annotations

from .embedder import EmbedderUnavailable, OpenAIEmbedder
from .retriever import Retriever
from .store import COLLECTION_DETAILED, COLLECTION_DISCOVERY, ChromaStore
from .vector_store import VectorStore, get_vector_store

__all__ = [
    "ChromaStore",
    "EmbedderUnavailable",
    "OpenAIEmbedder",
    "Retriever",
    "VectorStore",
    "get_vector_store",
    "COLLECTION_DISCOVERY",
    "COLLECTION_DETAILED",
]
