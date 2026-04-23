"""Retriever protocol + fallback stub for Phase 2-B.

Phase 2-A ships `app.rag.retriever.Retriever` with the same shape. If that
module is not yet importable, `get_retriever()` falls back to
`_FilesystemStubRetriever` which reads `data/nodes/catalog_discovery.json`
and `data/nodes/definitions/*.json` directly — this keeps Phase 2-B's CLI
runnable in isolation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..models.catalog import NodeCatalogEntry, NodeDefinition

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CATALOG_PATH = _PROJECT_ROOT / "data" / "nodes" / "catalog_discovery.json"
_DEFINITIONS_DIR = _PROJECT_ROOT / "data" / "nodes" / "definitions"


@runtime_checkable
class RetrieverProtocol(Protocol):
    """Duck-typed retriever interface consumed by planner/builder nodes."""

    def search_discovery(self, query: str, k: int = 8) -> list[NodeCatalogEntry]: ...

    def get_detail(self, node_type: str) -> NodeDefinition | None: ...

    def search_detailed(
        self, query: str, k: int = 4
    ) -> list[NodeDefinition]:  # pragma: no cover - optional
        ...

    def get_definitions_by_types(
        self, types: list[str]
    ) -> dict[str, NodeDefinition | None]:
        """Batch-fetch definitions. Returns {type: def_or_None} for each input type.
        Deduplication is the caller's responsibility or done internally.
        """  # C1-1:B-CAND-01
        ...


class _FilesystemStubRetriever:
    """In-memory fallback — keyword scoring, no embeddings.

    For discovery: scores catalog entries by naive case-insensitive token
    overlap against query tokens across (type, display_name, category,
    description, keywords). Good enough for exercise + tests; Phase 2-A's
    real retriever replaces this.
    """

    def __init__(
        self,
        *,
        catalog_path: Path = _CATALOG_PATH,
        definitions_dir: Path = _DEFINITIONS_DIR,
    ) -> None:
        self._entries: list[NodeCatalogEntry] = []
        self._entry_haystacks: list[str] = []
        self._definitions: dict[str, NodeDefinition] = {}

        if catalog_path.is_file():
            with open(catalog_path, encoding="utf-8") as f:
                for raw in json.load(f):
                    try:
                        entry = NodeCatalogEntry.model_validate(raw)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("skipping bad catalog entry: %s", exc)
                        continue
                    self._entries.append(entry)
                    keywords = " ".join(raw.get("keywords", []) or [])
                    self._entry_haystacks.append(
                        " ".join(
                            [
                                entry.type,
                                entry.display_name,
                                entry.category,
                                entry.description,
                                keywords,
                            ]
                        ).lower()
                    )
        else:
            logger.warning("catalog file not found: %s", catalog_path)

        if definitions_dir.is_dir():
            for path in sorted(definitions_dir.glob("*.json")):
                try:
                    with open(path, encoding="utf-8") as f:
                        raw = json.load(f)
                    defn = NodeDefinition.model_validate(raw)
                    self._definitions[defn.type] = defn
                except Exception as exc:  # noqa: BLE001
                    logger.debug("skipping bad definition %s: %s", path.name, exc)
        else:
            logger.warning("definitions dir not found: %s", definitions_dir)

    def search_discovery(self, query: str, k: int = 8) -> list[NodeCatalogEntry]:
        tokens = [t for t in query.lower().split() if t]
        if not tokens or not self._entries:
            return []

        scored: list[tuple[int, int, NodeCatalogEntry]] = []
        for idx, (entry, hay) in enumerate(zip(self._entries, self._entry_haystacks, strict=True)):
            score = sum(hay.count(tok) for tok in tokens)
            if score:
                scored.append((-score, idx, entry))
        scored.sort()
        return [e for _, _, e in scored[:k]]

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        return self._definitions.get(node_type)

    # C1-1:B-CAND-01
    def get_definitions_by_types(self, types: list[str]) -> dict[str, NodeDefinition | None]:
        if not types:
            return {}
        return {t: self._definitions.get(t) for t in types}

    def search_detailed(self, query: str, k: int = 4) -> list[NodeDefinition]:
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return list(self._definitions.values())[:k]
        scored: list[tuple[int, NodeDefinition]] = []
        for defn in self._definitions.values():
            hay = f"{defn.type} {defn.display_name} {defn.description}".lower()
            score = sum(hay.count(tok) for tok in tokens)
            if score:
                scored.append((-score, defn))
        scored.sort(key=lambda t: t[0])
        return [d for _, d in scored[:k]]


def get_retriever() -> RetrieverProtocol:
    """Return a retriever — prefer Phase 2-A's real class, fall back to stub.

    Phase 2-A's `Retriever(store, embedder)` requires the Chroma + OpenAI-compat
    embedder deps. We only wire it up if both are importable AND the Chroma
    collections already exist on disk — otherwise we degrade to the stub.
    """
    try:
        from ..config import get_settings
        from ..rag.embedder import OpenAIEmbedder
        from ..rag.retriever import Retriever
        from ..rag.vector_store import get_vector_store
    except ImportError as exc:
        logger.info("Phase 2-A rag module not importable (%s); using stub", exc)
        return _FilesystemStubRetriever()

    try:
        from ..rag.store import COLLECTION_DISCOVERY
        settings = get_settings()
        store = get_vector_store(settings)
        # Cheap sanity: if the discovery collection is empty the retriever is
        # useless — fall back immediately rather than serving zero hits.
        try:
            if store.count(COLLECTION_DISCOVERY) == 0:
                logger.info("Chroma discovery collection empty; using stub")
                return _FilesystemStubRetriever()
        except Exception:  # noqa: BLE001
            logger.info("Chroma discovery collection missing; using stub")
            return _FilesystemStubRetriever()
        # C1-2:R-CONF-01,R-CONF-02 — let embedder default to effective_embed_*
        # so EMBED_BASE_URL / EMBED_API_KEY are honoured here too.
        embedder = OpenAIEmbedder(model=settings.embed_model)
        return Retriever(store, embedder)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Retriever() init failed (%s); using filesystem stub", exc)
        return _FilesystemStubRetriever()


def make_stub_retriever(
    *,
    catalog_path: Path | None = None,
    definitions_dir: Path | None = None,
) -> RetrieverProtocol:
    """Explicit stub constructor for tests."""
    return _FilesystemStubRetriever(
        catalog_path=catalog_path or _CATALOG_PATH,
        definitions_dir=definitions_dir or _DEFINITIONS_DIR,
    )


__all__ = [
    "RetrieverProtocol",
    "get_retriever",
    "make_stub_retriever",
]


def format_discovery_hits(hits: list[NodeCatalogEntry]) -> str:
    """Render hits as the compact per-line form used in the planner prompt."""
    lines = []
    for h in hits:
        lines.append(f"- {h.type} | {h.display_name} | {h.category} | {h.description}")
    return "\n".join(lines)


def definitions_as_trimmed_json(defs: list[NodeDefinition]) -> list[dict[str, Any]]:
    """Trim NodeDefinition to only the fields the Builder prompt needs.

    Keeps type, type_version, display_name, parameters (with {name, type,
    required, default}). Drops verbose description to keep prompts under the
    4k token budget.
    """
    out: list[dict[str, Any]] = []
    for d in defs:
        out.append(
            {
                "type": d.type,
                "display_name": d.display_name,
                "type_version": d.type_version,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "required": p.required,
                        "default": p.default,
                    }
                    for p in d.parameters
                ],
            }
        )
    return out
