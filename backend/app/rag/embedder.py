"""OpenAIEmbedder — wraps `langchain_openai.OpenAIEmbeddings` (Implements C1-2).

Exposes `embed` / `embed_batch` and raises `EmbedderUnavailable` on connect
failure, so callers can distinguish "the embeddings endpoint is down" from
other errors. Targets any OpenAI-compatible endpoint — OpenAI itself, vllm,
LiteLLM, etc.

Prompt wrapping is routed through embedding-prompt profiles (C1-2 §7): the
hardcoded embeddinggemma wrapper used to apply uniformly, which actively hurt
retrieval for BGE/OpenAI models. Callers pass raw text; the profile decides
whether to add asymmetric prompt shells.
"""

from __future__ import annotations

import httpx
from langchain_openai import OpenAIEmbeddings

from app.config import get_settings

VALID_PROFILES = frozenset({"auto", "embeddinggemma", "bge", "openai", "none"})


class EmbedderUnavailable(RuntimeError):
    """Raised when the embeddings HTTP endpoint is unreachable."""


class OpenAIEmbedder:
    """Thin wrapper over `OpenAIEmbeddings` bound to an OpenAI-compatible server."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        profile: str | None = None,
        connect_timeout_s: float = 8.0,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.embed_model
        self.base_url = base_url or settings.effective_embed_base_url  # C1-2:R-CONF-01
        self.api_key = api_key or settings.openai_api_key
        raw_profile = profile or settings.embed_prompt_profile
        if raw_profile not in VALID_PROFILES:
            raise ValueError(
                f"Unknown EMBED_PROMPT_PROFILE={raw_profile!r}; "
                f"expected one of {sorted(VALID_PROFILES)}"
            )
        self.profile = _resolve_profile(raw_profile, self.model)
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
        """Embed a *query*. Profile-dependent prompt wrapping is applied here."""
        try:
            return self._client.embed_query(_wrap_query(text, self.profile))
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover — network
            raise EmbedderUnavailable(
                f"Embed call failed at {self.base_url}: {exc}"
            ) from exc

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of *documents*. Each text is wrapped per the active profile."""
        if not texts:
            return []
        wrapped = [_wrap_document(t, self.profile) for t in texts]
        try:
            return self._client.embed_documents(wrapped)
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover — network
            raise EmbedderUnavailable(
                f"Embed batch call failed at {self.base_url}: {exc}"
            ) from exc


# ---- profile resolution & wrappers (C1-2 §7) -------------------------------


def _resolve_profile(profile: str, embed_model: str) -> str:
    """Resolve `auto` to a concrete profile by inspecting the model id."""
    if profile != "auto":
        return profile
    model = embed_model.lower()
    if "embeddinggemma" in model or "gemma" in model:
        return "embeddinggemma"
    if "bge" in model:
        return "bge"
    if "text-embedding" in model:
        return "openai"
    return "none"


def _wrap_query(text: str, profile: str) -> str:
    if profile == "embeddinggemma":
        return f"task: search result | query: {text}"
    return text


def _wrap_document(text: str, profile: str) -> str:
    if profile == "embeddinggemma":
        # embeddinggemma was trained on "title: ... | text: ..." documents.
        # Ingest hands us a body whose first line is the node display_name,
        # so we split on the first newline to recover the title slot.
        first_line = text.split("\n", 1)[0]
        return f"title: {first_line} | text: {text}"
    return text
