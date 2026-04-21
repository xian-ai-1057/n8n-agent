"""RAG layer (Implements C1-2).

Dual-index retrieval over ChromaDB:
- `catalog_discovery` — all 500+ node entries (from xlsx) for Planner broad search.
- `catalog_detailed` — the curated ~30 nodes with full parameter schemas for Builder.

Public surface:
- `ChromaStore` — thin wrapper over a single `PersistentClient`.
- `OllamaEmbedder` — wraps `langchain_ollama.OllamaEmbeddings`.
- `Retriever` — the only caller-facing entry point; returns typed Pydantic objects.
"""

from __future__ import annotations

from .embedder import OllamaEmbedder
from .retriever import Retriever
from .store import ChromaStore, COLLECTION_DETAILED, COLLECTION_DISCOVERY

__all__ = [
    "ChromaStore",
    "OllamaEmbedder",
    "Retriever",
    "COLLECTION_DISCOVERY",
    "COLLECTION_DETAILED",
]
