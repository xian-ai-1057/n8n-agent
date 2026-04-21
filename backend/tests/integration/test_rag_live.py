"""Live integration tests for the RAG layer (Implements C1-2 §6).

Skipped when the OpenAI-compatible endpoint isn't reachable. Uses a temp
`chroma_path` so the production `.chroma/` is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from app.models.catalog import NodeDefinition

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "nodes"
_CATALOG = _DATA_DIR / "catalog_discovery.json"
_DEFS = _DATA_DIR / "definitions"


def _openai_reachable(base_url: str, api_key: str) -> bool:
    try:
        with httpx.Client(timeout=3.0) as http:
            resp = http.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
    except Exception:
        return False
    return True


_OPENAI_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1")
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
_SKIP = not _openai_reachable(_OPENAI_URL, _OPENAI_KEY)

pytestmark = pytest.mark.skipif(
    _SKIP,
    reason=f"OpenAI-compat endpoint not reachable at {_OPENAI_URL}",
)


@pytest.fixture(scope="module")
def live_retriever(tmp_path_factory, monkeypatch_module):
    """Ingest into a throwaway chroma_path, return a Retriever over it."""
    chroma_dir = tmp_path_factory.mktemp("chroma")

    monkeypatch_module.setenv("CHROMA_PATH", str(chroma_dir))
    monkeypatch_module.setenv("OPENAI_BASE_URL", _OPENAI_URL)
    monkeypatch_module.setenv("OPENAI_API_KEY", _OPENAI_KEY)

    # Clear settings cache so the new env var is picked up.
    from app.config import get_settings

    get_settings.cache_clear()

    from app.rag.embedder import OpenAIEmbedder
    from app.rag.ingest_detailed import ingest_detailed
    from app.rag.ingest_discovery import ingest_discovery
    from app.rag.retriever import Retriever
    from app.rag.store import ChromaStore

    store = ChromaStore(str(chroma_dir))
    embedder = OpenAIEmbedder()
    embedder.ping()

    ingest_discovery(_CATALOG, reset=True, store=store, embedder=embedder)
    ingest_detailed(_DEFS, reset=True, store=store, embedder=embedder)

    yield Retriever(store, embedder)

    get_settings.cache_clear()


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch (pytest's default is function-scoped)."""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


def _top3_types(entries) -> list[str]:
    return [e.type for e in entries[:3]]


def test_search_slack_in_top3(live_retriever) -> None:
    entries = live_retriever.search_discovery("發送 Slack 訊息", k=5)
    assert "n8n-nodes-base.slack" in _top3_types(entries), (
        f"slack missing from top3: {[e.type for e in entries]}"
    )


def test_search_schedule_trigger_in_top3(live_retriever) -> None:
    entries = live_retriever.search_discovery("排程觸發", k=5)
    assert "n8n-nodes-base.scheduleTrigger" in _top3_types(entries), (
        f"scheduleTrigger missing from top3: {[e.type for e in entries]}"
    )


def test_search_http_in_top3(live_retriever) -> None:
    entries = live_retriever.search_discovery("呼叫 HTTP API", k=5)
    assert "n8n-nodes-base.httpRequest" in _top3_types(entries), (
        f"httpRequest missing from top3: {[e.type for e in entries]}"
    )


def test_get_detail_http_request(live_retriever) -> None:
    defn = live_retriever.get_detail("n8n-nodes-base.httpRequest")
    assert defn is not None
    assert isinstance(defn, NodeDefinition)
    assert defn.type == "n8n-nodes-base.httpRequest"
    assert defn.parameters  # non-empty
    # Must include url + method.
    names = {p.name for p in defn.parameters}
    assert "url" in names
    assert "method" in names


def test_get_detail_missing_returns_none(live_retriever) -> None:
    assert live_retriever.get_detail("n8n-nodes-base.doesNotExist") is None


def test_counts_after_ingest(live_retriever) -> None:
    from app.rag.store import COLLECTION_DETAILED, COLLECTION_DISCOVERY

    # Access the underlying store via a back-channel on the retriever.
    store = live_retriever._store  # type: ignore[attr-defined]
    assert store.count(COLLECTION_DISCOVERY) >= 400
    assert store.count(COLLECTION_DETAILED) == 30
