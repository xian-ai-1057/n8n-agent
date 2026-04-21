"""Validate `catalog_discovery.json` and all `definitions/*.json` against the
Pydantic models in `app.models`.

Usage:

    python scripts/validate_catalogs.py

Exits non-zero on the first validation error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.models import NodeCatalogEntry, NodeDefinition  # noqa: E402

DISCOVERY_PATH = _PROJECT_ROOT / "data" / "nodes" / "catalog_discovery.json"
DEFINITIONS_DIR = _PROJECT_ROOT / "data" / "nodes" / "definitions"


def validate_discovery() -> int:
    with DISCOVERY_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list), "discovery catalog must be a JSON array"
    for i, row in enumerate(data):
        # keep only fields known to the pydantic model; we preserve `keywords`
        # as a sibling field on disk for RAG use.
        row = {k: v for k, v in row.items() if k != "keywords"}
        try:
            NodeCatalogEntry.model_validate(row)
        except Exception as exc:
            print(f"[fail] discovery row {i}: {exc}", file=sys.stderr)
            raise
    return len(data)


def validate_definitions() -> int:
    files = sorted(DEFINITIONS_DIR.glob("*.json"))
    for f in files:
        with f.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        # strip our meta-only keys before pydantic validation
        clean = {k: v for k, v in raw.items() if not k.startswith("_")}
        try:
            NodeDefinition.model_validate(clean)
        except Exception as exc:
            print(f"[fail] {f.name}: {exc}", file=sys.stderr)
            raise
    return len(files)


def main() -> int:
    dcount = validate_discovery()
    fcount = validate_definitions()
    print(f"[ok] discovery entries: {dcount}")
    print(f"[ok] detailed definitions: {fcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
