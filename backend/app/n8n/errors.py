"""Typed exceptions for the n8n client (Implements C1-3 §5).

Base class is `N8nApiError`; subclasses map one-to-one to HTTP conditions so
callers can branch on type instead of status codes.
"""

from __future__ import annotations

from typing import Any


class N8nApiError(Exception):
    """Base class for all n8n client errors.

    Carries optional `status_code` and `payload` so generic handlers can still
    inspect the response. Specific subclasses below encode the taxonomy the
    agent graph depends on.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class N8nAuthError(N8nApiError):
    """401 / 403 — API key missing, wrong, or insufficient."""


class N8nNotFoundError(N8nApiError):
    """404 — workflow id (or other resource) does not exist."""


class N8nBadRequestError(N8nApiError):
    """400 — n8n rejected the payload (schema / validation error).

    The upstream message is surfaced on `.detail` for easy display in the
    deployer / API response (C1-5).
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        payload: Any | None = None,
    ) -> None:
        super().__init__(message, status_code=400, payload=payload)
        self.detail = detail or message


class N8nServerError(N8nApiError):
    """5xx — upstream n8n internal error."""


class N8nUnavailable(N8nApiError):
    """Connection refused / DNS failure / timeout — n8n not reachable."""
