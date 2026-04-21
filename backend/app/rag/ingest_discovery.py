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
from app.rag.embedder import OllamaEmbedder, OllamaUnavailable
from app.rag.store import COLLECTION_DISCOVERY, ChromaStore

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CATALOG = _PROJECT_ROOT / "data" / "nodes" / "catalog_discovery.json"

_BATCH = 32
_PROGRESS_EVERY = 50


def _build_document(entry: dict[str, Any]) -> str:
    """Build the embeddable document string.

    We use embeddinggemma's document-side prompt (`title: ... | text: ...`) so
    retrieval aligns with the query prompt applied in `OllamaEmbedder.embed()`.
    The display_name is repeated in both title and text to anchor the vector.
    """
    display_name = entry.get("display_name") or ""
    category = entry.get("category") or ""
    description = entry.get("description") or ""
    keywords: list[str] = entry.get("keywords") or []
    text_body = (
        f"{display_name}\n"
        f"類別: {category}\n"
        f"{description}\n"
        f"關鍵字: {', '.join(keywords)}"
    )
    return f"title: {display_name} | text: {text_body}"


def _build_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": entry["type"],
        "display_name": entry.get("display_name", ""),
        "category": entry.get("category", ""),
        "description": entry.get("description", ""),
    }


def ingest_discovery(
    catalog_path: str | Path = _DEFAULT_CATALOG,
    *,
    reset: bool = False,
    store: ChromaStore | None = None,
    embedder: OllamaEmbedder | None = None,
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

    settings = get_settings()
    store = store or ChromaStore(settings.chroma_path)
    embedder = embedder or OllamaEmbedder()

    if reset:
        store.reset(COLLECTION_DISCOVERY)

    total = 0
    items = list(by_type.values())
    print(f"[ingest_discovery] source={catalog_path.name} unique_types={len(items)}")

    for start in range(0, len(items), _BATCH):
        chunk = items[start : start + _BATCH]
        ids = [e["type"] for e in chunk]
        docs = [_build_document(e) for e in chunk]
        metas = [_build_metadata(e) for e in chunk]
        embeddings = embedder.embed_batch(docs)
        store.upsert(COLLECTION_DISCOVERY, ids, docs, metas, embeddings)

        total += len(chunk)
        # Rough "every 50" progress (batches are 32 so we print as we cross thresholds).
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
        embedder = OllamaEmbedder()
        embedder.ping()
        ingest_discovery(args.catalog, reset=args.reset, embedder=embedder)
    except OllamaUnavailable as exc:
        print(f"[ingest_discovery] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ingest_discovery] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
