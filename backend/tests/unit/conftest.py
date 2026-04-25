"""Unit-test conftest — stubs optional heavy deps.

Stubs langchain_openai so unit tests can import app.agent.* without a live
langchain_openai installation.  No-op when the real package is present.

Also provides a no-op stub for ``app.agent.completeness`` when the real
module is not yet present on disk (the B-COMP-01 work landed the import in
graph.py before the implementation file was committed). Stub returns a node
function that emits an empty delta — a true no-op so completeness_check is
transparent to the existing graph_wiring tests and to the HITL helpers
defined for C1-1:HITL-SHIP-01.
"""

import sys
import types
from unittest.mock import MagicMock

if "langchain_openai" not in sys.modules:
    sys.modules["langchain_openai"] = MagicMock()

# Stub completeness module if missing (defensive — no-op when real impl lands).
try:  # pragma: no cover — exercised only when real module exists
    import app.agent.completeness  # noqa: F401
except ModuleNotFoundError:
    _stub = types.ModuleType("app.agent.completeness")

    def _make_completeness_check_node(retriever):  # type: ignore[no-redef]
        def _node(state):
            return {}

        return _node

    _stub._make_completeness_check_node = _make_completeness_check_node  # type: ignore[attr-defined]
    sys.modules["app.agent.completeness"] = _stub
