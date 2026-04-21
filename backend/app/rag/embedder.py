"""OllamaEmbedder — wraps `langchain_ollama.OllamaEmbeddings` (Implements C1-2).

Exposes `embed` / `embed_batch` and raises `OllamaUnavailable` on connect failure,
so callers can distinguish "Ollama is down" from other errors.
"""

from __future__ import annotations

import httpx
from langchain_ollama import OllamaEmbeddings

from app.config import get_settings


class OllamaUnavailable(RuntimeError):
    """Raised when the Ollama HTTP endpoint is unreachable."""


class OllamaEmbedder:
    """Embeddinggemma (via Ollama) wrapper."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        connect_timeout_s: float = 8.0,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.embed_model
        self.base_url = base_url or settings.ollama_base_url
        self._connect_timeout_s = connect_timeout_s
        # `langchain_ollama.OllamaEmbeddings` owns its own http client;
        # we don't get to configure the connect timeout directly, but it
        # honours Ollama's default (60s) which is acceptable for batch work.
        self._client = OllamaEmbeddings(
            model=self.model,
            base_url=self.base_url,
        )

    # ----- availability probe ----------------------------------------------

    def ping(self) -> None:
        """Quick reachability probe. Raises OllamaUnavailable on failure."""
        try:
            with httpx.Client(timeout=self._connect_timeout_s) as http:
                resp = http.get(f"{self.base_url.rstrip('/')}/api/tags")
                resp.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise OllamaUnavailable(
                f"Ollama not reachable at {self.base_url}: {exc}"
            ) from exc

    # ----- embedding API ----------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Embed a *query* text. Wraps in the embeddinggemma search prompt."""
        try:
            return self._client.embed_query(_as_query(text))
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover — network
            raise OllamaUnavailable(
                f"Ollama embed failed at {self.base_url}: {exc}"
            ) from exc

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of *documents*. Does not apply the search prompt.

        Callers should pass doc strings already in the `title: ... | text: ...`
        shape if they want the embeddinggemma document-side prompt; we leave
        the document wrapping to the ingest scripts so they can include titles.
        """
        if not texts:
            return []
        try:
            return self._client.embed_documents(texts)
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover — network
            raise OllamaUnavailable(
                f"Ollama embed_batch failed at {self.base_url}: {exc}"
            ) from exc


# embeddinggemma was trained with asymmetric prompts for retrieval.
# The canonical query prompt is "task: search result | query: {q}".
# See: https://ai.google.dev/gemma/docs/embeddinggemma#prompts
_QUERY_PROMPT = "task: search result | query: {text}"


def _as_query(text: str) -> str:
    return _QUERY_PROMPT.format(text=text)
