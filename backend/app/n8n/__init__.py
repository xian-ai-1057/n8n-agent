"""n8n REST client package (Implements C1-3).

Public surface: `N8nClient`, typed exceptions, and `WorkflowDeployResult`.
"""

from .client import N8nClient
from .errors import (
    N8nApiError,
    N8nAuthError,
    N8nBadRequestError,
    N8nNotFoundError,
    N8nServerError,
    N8nUnavailable,
)
from .types import WorkflowDeployResult

__all__ = [
    "N8nApiError",
    "N8nAuthError",
    "N8nBadRequestError",
    "N8nClient",
    "N8nNotFoundError",
    "N8nServerError",
    "N8nUnavailable",
    "WorkflowDeployResult",
]
