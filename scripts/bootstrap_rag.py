"""One-shot bootstrap for the RAG layer (Implements C1-2).

Usage:

    python scripts/bootstrap_rag.py [--reset]

Steps:
1. Probe the OpenAI-compatible endpoint's reachability and model presence.
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
from app.rag.embedder import EmbedderUnavailable, OpenAIEmbedder  # noqa: E402
from app.rag.ingest_detailed import ingest_detailed  # noqa: E402
from app.rag.ingest_discovery import ingest_discovery  # noqa: E402
from app.rag.store import (  # noqa: E402
    COLLECTION_DETAILED,
    COLLECTION_DISCOVERY,
)
from app.rag.vector_store import get_vector_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the RAG layer")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop both collections before re-ingesting",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    print(
        f"[bootstrap_rag] backend={settings.vector_store_backend} "
        f"path={settings.chroma_path} metric={settings.rag_distance_metric}"
    )
    embed_url = settings.effective_embed_base_url
    embed_url_tag = (
        "split"
        if settings.embed_base_url and embed_url != settings.openai_base_url
        else "shared"
    )
    print(
        f"[bootstrap_rag] openai={settings.openai_base_url} "
        f"embed_url={embed_url} ({embed_url_tag}) "
        f"model={settings.embed_model}"
    )

    embedder = OpenAIEmbedder()
    try:
        embedder.ping()
    except EmbedderUnavailable as exc:
        print(f"[bootstrap_rag] FATAL: {exc}", file=sys.stderr)
        return 2

    store = get_vector_store(settings)

    try:
        disc_count = ingest_discovery(reset=args.reset, store=store, embedder=embedder)
        det_count = ingest_detailed(reset=args.reset, store=store, embedder=embedder)
    except EmbedderUnavailable as exc:
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
