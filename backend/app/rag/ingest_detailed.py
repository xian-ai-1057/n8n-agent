"""Ingest `data/nodes/definitions/*.json` into Chroma (Implements C1-2 §2).

CLI usage:

    python -m app.rag.ingest_detailed [--reset] [--dir PATH]

Each document carries the **full** `NodeDefinition` JSON in `metadata.raw`, so
`Retriever.get_detail()` can hydrate back without a filesystem re-read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.models.catalog import NodeDefinition
from app.rag.embedder import EmbedderUnavailable, OpenAIEmbedder
from app.rag.store import COLLECTION_DETAILED, ChromaStore

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DIR = _PROJECT_ROOT / "data" / "nodes" / "definitions"

_BATCH = 16
_PARAM_SUMMARY_LIMIT = 8


def _build_document(defn: NodeDefinition) -> str:
    """Build the embeddable document body.

    Returns a raw body whose first line is the display_name. Profile-specific
    prompt wrapping is applied by `OpenAIEmbedder.embed_batch()` (C1-2 §7).
    """
    required_params = [p.name for p in defn.parameters if p.required]
    summary_names = [p.name for p in defn.parameters[:_PARAM_SUMMARY_LIMIT]]
    return (
        f"{defn.display_name}\n"
        f"{defn.description}\n"
        f"必填參數: {', '.join(required_params)}\n"
        f"參數摘要: {', '.join(summary_names)}"
    )


def _build_metadata(defn: NodeDefinition, raw: dict[str, Any]) -> dict[str, Any]:
    # Strip non-schema fields like `_source` before serializing `raw`.
    clean = defn.model_dump(mode="json")
    return {
        "type": defn.type,
        "display_name": defn.display_name,
        "category": defn.category,
        "type_version": defn.type_version,
        "raw": json.dumps(clean, ensure_ascii=False),
    }


def ingest_detailed(
    definitions_dir: str | Path = _DEFAULT_DIR,
    *,
    reset: bool = False,
    store: ChromaStore | None = None,
    embedder: OpenAIEmbedder | None = None,
) -> int:
    """Upsert every definitions/*.json. Returns ingested count."""
    definitions_dir = Path(definitions_dir)
    if not definitions_dir.is_dir():
        raise FileNotFoundError(f"Definitions dir not found: {definitions_dir}")

    files = sorted(definitions_dir.glob("*.json"))
    print(f"[ingest_detailed] source_dir={definitions_dir} files={len(files)}")

    settings = get_settings()
    store = store or ChromaStore(settings.chroma_path)
    embedder = embedder or OpenAIEmbedder()

    if reset:
        store.reset(COLLECTION_DETAILED)

    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict[str, Any]] = []
    seen_types: dict[str, str] = {}

    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            defn = NodeDefinition.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            print(f"[ingest_detailed] WARN skip {path.name}: {exc}")
            continue

        if defn.type in seen_types:
            raise ValueError(
                f"Duplicate type {defn.type!r}: {seen_types[defn.type]} and {path.name}"
            )
        seen_types[defn.type] = path.name

        ids.append(defn.type)
        docs.append(_build_document(defn))
        metas.append(_build_metadata(defn, raw))

    # Embed in batches.
    total = 0
    for start in range(0, len(ids), _BATCH):
        end = start + _BATCH
        chunk_docs = docs[start:end]
        embeddings = embedder.embed_batch(chunk_docs)
        store.upsert(
            COLLECTION_DETAILED,
            ids[start:end],
            chunk_docs,
            metas[start:end],
            embeddings,
        )
        total += len(chunk_docs)
        print(f"[ingest_detailed] upserted {total}/{len(ids)}")

    print(f"[ingest_detailed] done: {total} entries in '{COLLECTION_DETAILED}'")
    return total


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest definitions/*.json into Chroma")
    parser.add_argument("--reset", action="store_true", help="Drop collection before ingest")
    parser.add_argument(
        "--dir",
        default=str(_DEFAULT_DIR),
        help=f"Path to definitions dir (default: {_DEFAULT_DIR})",
    )
    args = parser.parse_args(argv)

    try:
        embedder = OpenAIEmbedder()
        embedder.ping()
        ingest_detailed(args.dir, reset=args.reset, embedder=embedder)
    except EmbedderUnavailable as exc:
        print(f"[ingest_detailed] ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ingest_detailed] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
