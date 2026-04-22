"""Vector store abstraction (Implements C1-2).

Defines the duck-typed `VectorStore` protocol consumed by the Retriever and
ingest scripts, plus a `get_vector_store()` factory that picks the concrete
implementation based on `Settings.vector_store_backend`.

Today the only implementation is `ChromaStore` (in `store.py`). The factory
exists so a deployment can swap in Qdrant / Pinecone / Weaviate / Milvus by
adding a class that satisfies the protocol and registering it below — no
caller changes required.

Any new backend MUST:
- Use pre-computed embeddings (no server-side embedding function).
- Return hits shaped as `{id, document, metadata, distance, score}` with
  `score = 1 - distance` under cosine.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from ..config import Settings, get_settings

_VALID_METRICS = frozenset({"cosine", "l2", "ip"})


@runtime_checkable
class VectorStore(Protocol):
    """Minimum surface area a vector store must expose."""

    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None: ...

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_by_ids(
        self,
        collection: str,
        ids: Iterable[str],
    ) -> list[dict[str, Any]]: ...

    def count(self, collection: str) -> int: ...

    def reset(self, collection: str) -> None: ...


def validate_distance_metric(metric: str) -> str:
    """Return `metric` if valid, else raise ValueError listing the options."""
    if metric not in _VALID_METRICS:
        raise ValueError(
            f"Unknown RAG_DISTANCE_METRIC={metric!r}; "
            f"expected one of {sorted(_VALID_METRICS)}"
        )
    return metric


def get_vector_store(settings: Settings | None = None) -> VectorStore:
    """Return a VectorStore matching `Settings.vector_store_backend`.

    Importing concrete backends is deferred until selected, so a deployment
    that never enables (say) Pinecone won't pay the import cost.
    """
    s = settings or get_settings()
    backend = s.vector_store_backend.lower().strip()
    metric = validate_distance_metric(s.rag_distance_metric)

    if backend == "chroma":
        from .store import ChromaStore

        return ChromaStore(s.chroma_path, distance_metric=metric)

    raise ValueError(
        f"Unsupported VECTOR_STORE_BACKEND={backend!r}. "
        f"Register a new implementation in app/rag/vector_store.py."
    )


__all__ = [
    "VectorStore",
    "get_vector_store",
    "validate_distance_metric",
]
