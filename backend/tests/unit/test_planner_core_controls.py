"""Tests for the planner core-control augmentation (C1-1 §2.1)."""

from __future__ import annotations

from app.agent.planner import _augment_with_core_controls, _load_core_catalog
from app.agent.retriever_protocol import make_stub_retriever
from app.models.catalog import NodeCatalogEntry


def test_core_catalog_has_if_and_switch():
    core = _load_core_catalog()
    assert "n8n-nodes-base.if" in core
    assert "n8n-nodes-base.switch" in core


def test_augment_appends_missing_core_types():
    r = make_stub_retriever()
    # Start with hits that deliberately do NOT include `if` or `switch`.
    hits: list[NodeCatalogEntry] = [
        e for e in r.search_discovery("webhook", 3)
        if e.type not in ("n8n-nodes-base.if", "n8n-nodes-base.switch")
    ]
    augmented_types = {h.type for h in _augment_with_core_controls(hits, r)}
    assert "n8n-nodes-base.if" in augmented_types
    assert "n8n-nodes-base.switch" in augmented_types


def test_augment_preserves_original_ranking():
    r = make_stub_retriever()
    hits = r.search_discovery("schedule trigger http", 3)
    augmented = _augment_with_core_controls(hits, r)
    # Retrieved entries must appear *before* appended core entries.
    assert augmented[: len(hits)] == hits
