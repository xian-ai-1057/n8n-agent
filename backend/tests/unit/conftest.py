"""Unit-test conftest — stubs optional heavy deps.

Stubs langchain_openai so unit tests can import app.agent.* without a live
langchain_openai installation.  No-op when the real package is present.
"""

import sys
from unittest.mock import MagicMock

if "langchain_openai" not in sys.modules:
    sys.modules["langchain_openai"] = MagicMock()
