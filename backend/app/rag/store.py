"""ChromaStore — thin wrapper over a single persistent Chroma client (Implements C1-2 §1).

Two collections, both cosine-distance:
- `catalog_discovery`
- `catalog_detailed`

We pass pre-computed embeddings on upsert/query (no server-side embedding function),
so Chroma never needs to call the embeddings endpoint itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings

COLLECTION_DISCOVERY = "catalog_discovery"
COLLECTION_DETAILED = "catalog_detailed"

_ALLOWED = {COLLECTION_DISCOVERY, COLLECTION_DETAILED}


class ChromaStore:
    """Thin wrapper around a Chroma `PersistentClient` plus our two collections."""

    def __init__(self, chroma_path: str):
        Path(chroma_path).mkdir(parents=True, exist_ok=True)
        self._path = chroma_path
        self._client = chromadb.PersistentClient(
            path=chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        # Eagerly create both collections so `count()` on an empty store works.
        self._get_or_create(COLLECTION_DISCOVERY)
        self._get_or_create(COLLECTION_DETAILED)

    # ----- internals --------------------------------------------------------

    def _get_or_create(self, name: str) -> Collection:
        if name not in _ALLOWED:
            raise ValueError(f"Unknown collection: {name!r}")
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    # ----- public API -------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def collection(self, name: str) -> Collection:
        return self._get_or_create(name)

    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        """Upsert by id. Chroma will overwrite existing rows with the same id."""
        if not (len(ids) == len(documents) == len(metadatas) == len(embeddings)):
            raise ValueError(
                "upsert: ids/documents/metadatas/embeddings must all be same length"
            )
        if not ids:
            return
        col = self._get_or_create(collection)
        col.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a flat list of hits. Each hit: {id, document, metadata, distance, score}.

        `score` = 1 - distance (cosine), so higher is better.
        """
        col = self._get_or_create(collection)
        res = col.query(
            query_embeddings=[query_embedding],
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        hits: list[dict[str, Any]] = []
        ids_batch = res.get("ids") or [[]]
        docs_batch = res.get("documents") or [[]]
        metas_batch = res.get("metadatas") or [[]]
        dists_batch = res.get("distances") or [[]]

        ids = ids_batch[0] if ids_batch else []
        docs = docs_batch[0] if docs_batch else []
        metas = metas_batch[0] if metas_batch else []
        dists = dists_batch[0] if dists_batch else []

        for i, node_id in enumerate(ids):
            distance = float(dists[i]) if i < len(dists) and dists[i] is not None else 1.0
            hits.append(
                {
                    "id": node_id,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": dict(metas[i]) if i < len(metas) and metas[i] else {},
                    "distance": distance,
                    "score": 1.0 - distance,
                }
            )
        return hits

    def get_by_ids(
        self,
        collection: str,
        ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Exact-id lookup (no embedding math). Returns same shape as `query()`
        but without `distance`/`score`.
        """
        ids_list = list(ids)
        if not ids_list:
            return []
        col = self._get_or_create(collection)
        res = col.get(ids=ids_list, include=["documents", "metadatas"])
        out: list[dict[str, Any]] = []
        for i, node_id in enumerate(res.get("ids") or []):
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            out.append(
                {
                    "id": node_id,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": dict(metas[i]) if i < len(metas) and metas[i] else {},
                }
            )
        return out

    def count(self, collection: str) -> int:
        return self._get_or_create(collection).count()

    def reset(self, collection: str) -> None:
        """Drop and recreate the collection. Idempotent."""
        if collection not in _ALLOWED:
            raise ValueError(f"Unknown collection: {collection!r}")
        try:
            self._client.delete_collection(name=collection)
        except Exception:
            # Collection didn't exist — that's fine.
            pass
        self._get_or_create(collection)
