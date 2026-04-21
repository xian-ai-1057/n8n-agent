"""One-shot bootstrap for the RAG layer (Implements C1-2).

Usage:

    python scripts/bootstrap_rag.py [--reset]

Steps:
1. Probe Ollama reachability and model presence.
2. Ingest discovery collection.
3. Ingest detailed collection.

Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `app.*` importable when called as a script from project root.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config import get_settings  # noqa: E402
from app.rag.embedder import OllamaEmbedder, OllamaUnavailable  # noqa: E402
from app.rag.ingest_detailed import ingest_detailed  # noqa: E402
from app.rag.ingest_discovery import ingest_discovery  # noqa: E402
from app.rag.store import (  # noqa: E402
    COLLECTION_DETAILED,
    COLLECTION_DISCOVERY,
    ChromaStore,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the RAG layer")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop both collections before re-ingesting",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    print(f"[bootstrap_rag] chroma_path={settings.chroma_path}")
    print(f"[bootstrap_rag] ollama={settings.ollama_base_url} model={settings.embed_model}")

    embedder = OllamaEmbedder()
    try:
        embedder.ping()
    except OllamaUnavailable as exc:
        print(f"[bootstrap_rag] FATAL: {exc}", file=sys.stderr)
        return 2

    store = ChromaStore(settings.chroma_path)

    try:
        disc_count = ingest_discovery(reset=args.reset, store=store, embedder=embedder)
        det_count = ingest_detailed(reset=args.reset, store=store, embedder=embedder)
    except OllamaUnavailable as exc:
        print(f"[bootstrap_rag] FATAL: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover
        print(f"[bootstrap_rag] FATAL: {exc}", file=sys.stderr)
        return 1

    print(
        f"[bootstrap_rag] DONE  "
        f"{COLLECTION_DISCOVERY}={store.count(COLLECTION_DISCOVERY)} "
        f"({disc_count} upserted)  "
        f"{COLLECTION_DETAILED}={store.count(COLLECTION_DETAILED)} "
        f"({det_count} upserted)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
