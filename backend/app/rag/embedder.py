"""OpenAIEmbedder — wraps `langchain_openai.OpenAIEmbeddings` (Implements C1-2).

Exposes `embed` / `embed_batch` and raises `EmbedderUnavailable` on connect
failure, so callers can distinguish "the embeddings endpoint is down" from
other errors. Targets any OpenAI-compatible endpoint — OpenAI itself, vllm,
LiteLLM, etc.
"""

from __future__ import annotations

import httpx
from langchain_openai import OpenAIEmbeddings

from app.config import get_settings


class EmbedderUnavailable(RuntimeError):
    """Raised when the embeddings HTTP endpoint is unreachable."""


class OpenAIEmbedder:
    """Thin wrapper over `OpenAIEmbeddings` bound to an OpenAI-compatible server."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        connect_timeout_s: float = 8.0,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.embed_model
        self.base_url = base_url or settings.openai_base_url
        self.api_key = api_key or settings.openai_api_key
        self._connect_timeout_s = connect_timeout_s
        # `OpenAIEmbeddings` owns its own HTTPX client. `check_embedding_ctx_length`
        # must be False for vllm: the client otherwise tries to tokenise with
        # tiktoken using an OpenAI model name, which doesn't exist for e.g.
        # BGE/E5.
        self._client = OpenAIEmbeddings(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            check_embedding_ctx_length=False,
        )

    # ----- availability probe ----------------------------------------------

    def ping(self) -> None:
        """Quick reachability probe. Raises EmbedderUnavailable on failure."""
        url = f"{self.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with httpx.Client(timeout=self._connect_timeout_s) as http:
                resp = http.get(url, headers=headers)
                resp.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise EmbedderUnavailable(
                f"Embeddings endpoint not reachable at {self.base_url}: {exc}"
            ) from exc

    # ----- embedding API ----------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Embed a *query* text. Wraps in the embeddinggemma search prompt.

        Leaves the prompt wrapper in place because it's cheap no-op noise for
        models that don't need it (BGE/E5/OpenAI) and still wins when users
        point at an embeddinggemma-compatible server.
        """
        try:
            return self._client.embed_query(_as_query(text))
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover — network
            raise EmbedderUnavailable(
                f"Embed call failed at {self.base_url}: {exc}"
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
            raise EmbedderUnavailable(
                f"Embed batch call failed at {self.base_url}: {exc}"
            ) from exc


# embeddinggemma was trained with asymmetric prompts for retrieval.
# The canonical query prompt is "task: search result | query: {q}".
# See: https://ai.google.dev/gemma/docs/embeddinggemma#prompts
_QUERY_PROMPT = "task: search result | query: {text}"


def _as_query(text: str) -> str:
    return _QUERY_PROMPT.format(text=text)
