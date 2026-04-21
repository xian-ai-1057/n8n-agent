"""Request-scoped context for log correlation.

`/chat` sets `request_id_var` at the start of a request; a `logging.Filter`
injects it into every `LogRecord` so the format string can use `%(rid)s`.
Non-request callers (CLI, tests, startup) see the default `"-"`.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Attach `rid` to every LogRecord for the formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.rid = request_id_var.get()
        return True
