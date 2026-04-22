"""Ingest `data/nodes/catalog_discovery.json` into Chroma (Implements C1-2 §2).

CLI usage:

    python -m app.rag.ingest_discovery [--reset] [--catalog PATH]

Discovery documents capture: display_name, category, description, keywords.
`keywords` is stripped from the model (Pydantic), so we read the raw JSON here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.rag.embedder import EmbedderUnavailable, OpenAIEmbedder
from app.rag.store import COLLECTION_DISCOVERY
from app.rag.vector_store import VectorStore, get_vector_store

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CATALOG = _PROJECT_ROOT / "data" / "nodes" / "catalog_discovery.json"
_DEFAULT_DEFINITIONS = _PROJECT_ROOT / "data" / "nodes" / "definitions"

_PROGRESS_EVERY = 50


def _scan_detail_slugs(definitions_dir: Path) -> set[str]:
    """Return the set of filename stems present in definitions_dir.

    R2-2 §6: slug rule is the part after the dot in `type`
    (e.g. `n8n-nodes-base.httpRequest` → `httpRequest.json`;
    `@n8n/n8n-nodes-langchain.agent` → `agent.json`). Callers derive the slug
    for each discovery entry via `type.rpartition('.')[-1]` and test membership.
    """
    if not definitions_dir.is_dir():
        return set()
    return {p.stem for p in definitions_dir.glob("*.json")}


def _slug_for(node_type: str) -> str:
    return node_type.rpartition(".")[-1]


def _build_document(entry: dict[str, Any]) -> str:
    """Build the embeddable document body.

    Returns a raw body whose first line is the display_name. The prompt
    wrapping (e.g. embeddinggemma's `title: ... | text: ...`) is applied by
    `OpenAIEmbedder.embed_batch()` based on the active embedding profile
    (C1-2 §7). Ingest must not re-wrap here.
    """
    display_name = entry.get("display_name") or ""
    category = entry.get("category") or ""
    description = entry.get("description") or ""
    keywords: list[str] = entry.get("keywords") or []
    return (
        f"{display_name}\n"
        f"類別: {category}\n"
        f"{description}\n"
        f"關鍵字: {', '.join(keywords)}"
    )


def _build_metadata(entry: dict[str, Any], *, has_detail: bool) -> dict[str, Any]:
    return {
        "type": entry["type"],
        "display_name": entry.get("display_name", ""),
        "category": entry.get("category", ""),
        "description": entry.get("description", ""),
        "has_detail": has_detail,
    }


def ingest_discovery(
    catalog_path: str | Path = _DEFAULT_CATALOG,
    *,
    reset: bool = False,
    store: VectorStore | None = None,
    embedder: OpenAIEmbedder | None = None,
    definitions_dir: str | Path | None = None,
    batch_size: int | None = None,
) -> int:
    """Upsert all discovery entries. Returns ingested count."""
    catalog_path = Path(catalog_path)
    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"catalog_discovery.json must be a list; got {type(raw).__name__}")

    # Deduplicate by `type` (final write wins).
    by_type: dict[str, dict[str, Any]] = {}
    for entry in raw:
        node_type = entry.get("type")
        if not node_type:
            continue
        by_type[node_type] = entry

    detail_slugs = _scan_detail_slugs(
        Path(definitions_dir) if definitions_dir else _DEFAULT_DEFINITIONS
    )

    settings = get_settings()
    store = store or get_vector_store(settings)
    embedder = embedder or OpenAIEmbedder()
    batch = batch_size or settings.embed_batch_size

    if reset:
        store.reset(COLLECTION_DISCOVERY)

    total = 0
    items = list(by_type.values())
    detail_hits = sum(1 for e in items if _slug_for(e["type"]) in detail_slugs)
    print(
        f"[ingest_discovery] source={catalog_path.name} "
        f"unique_types={len(items)} has_detail={detail_hits} batch={batch}"
    )

    for start in range(0, len(items), batch):
        chunk = items[start : start + batch]
        ids = [e["type"] for e in chunk]
        docs = [_build_document(e) for e in chunk]
        metas = [
            _build_metadata(e, has_detail=_slug_for(e["type"]) in detail_slugs)
            for e in chunk
        ]
        embeddings = embedder.embed_batch(docs)
        store.upsert(COLLECTION_DISCOVERY, ids, docs, metas, embeddings)

        total += len(chunk)
        if total // _PROGRESS_EVERY > (total - len(chunk)) // _PROGRESS_EVERY:
            print(f"[ingest_discovery] upserted {total}/{len(items)}")

    print(f"[ingest_discovery] done: {total} entries in '{COLLECTION_DISCOVERY}'")
    return total


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest catalog_discovery.json into Chroma")
    parser.add_argument("--reset", action="store_true", help="Drop collection before ingest")
    parser.add_argument(
        "--catalog",
        default=str(_DEFAULT_CATALOG),
        help=f"Path to catalog_discovery.json (default: {_DEFAULT_CATALOG})",
    )
    args = parser.parse_args(argv)

    try:
        embedder = OpenAIEmbedder()
        embedder.ping()
        ingest_discovery(args.catalog, reset=args.reset, embedder=embedder)
    except EmbedderUnavailable as exc:
        print(f"[ingest_discovery] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ingest_discovery] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
