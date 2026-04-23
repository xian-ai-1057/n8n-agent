"""Application settings (Implements D0-3).

Reads environment variables (optionally from a `.env` file at the project root)
using `pydantic-settings`. Exposes a cached `get_settings()` factory.

Layered design:
- Core endpoint / model identity (`LLM_MODEL`, `EMBED_MODEL`, `OPENAI_BASE_URL`)
  still carries the "default everything" role — if nothing else is set, all
  agent stages use these values.
- Per-stage overrides (`PLANNER_MODEL`, `BUILDER_MODEL`, `FIX_MODEL`,
  `CRITIC_MODEL` + matching `*_TEMPERATURE`) let operators swap in a stronger
  model for hard stages (fix/critic) without touching code.
- Knobs that used to be hard-coded in agent / RAG code (timeouts, retry
  count, retrieval `k`, prompt char budget, ingest batch size, vector-store
  distance metric) are now env-configurable so the same image runs in very
  different deployment profiles (local dev, small cloud, large cloud).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# project_root/backend/app/config.py -> project_root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration for the backend service."""

    # ------------------------------------------------------------------
    # n8n target
    # ------------------------------------------------------------------
    n8n_url: str = Field(default="http://localhost:5678", description="n8n base URL.")
    n8n_api_key: str = Field(default="", description="X-N8N-API-KEY value.")

    # ------------------------------------------------------------------
    # OpenAI-compatible inference endpoint
    # Works with OpenAI, vllm --served-model-name, LiteLLM, OpenRouter, etc.
    # ------------------------------------------------------------------
    openai_base_url: str = Field(
        default="http://localhost:8000/v1",
        description="Base URL for the OpenAI-compatible chat/embeddings API.",
    )
    openai_api_key: str = Field(
        default="EMPTY",
        description="API key sent as `Authorization: Bearer`. vllm accepts any value.",
    )
    # C1-2:R-CONF-01
    embed_base_url: str = Field(
        default="",
        description=(
            "Embeddings endpoint base URL. Empty → fall back to openai_base_url. "
            "See C1-2 §10 / R-CONF-01."
        ),
    )
    # C1-2:R-CONF-02
    embed_api_key: str = Field(
        default="",
        description=(
            "API key for the embeddings endpoint. Empty → fall back to openai_api_key. "
            "See C1-2 §11 / R-CONF-02."
        ),
    )
    llm_model: str = Field(
        default="Qwen/Qwen2.5-7B-Instruct",
        description="Default chat model. Per-stage overrides below take precedence.",
    )
    embed_model: str = Field(
        default="BAAI/bge-m3",
        description="Embedding model name (must match the backend's served model).",
    )
    embed_prompt_profile: str = Field(
        default="auto",
        description=(
            "Embedding prompt profile: auto|embeddinggemma|bge|openai|none. "
            "`auto` infers from embed_model. See C1-2 §7."
        ),
    )

    # ------------------------------------------------------------------
    # Per-stage model overrides (D0-3 §2.1)
    # Empty string means "fall back to llm_model".
    # ------------------------------------------------------------------
    planner_model: str = Field(
        default="",
        description="Planner-stage chat model. Empty = use llm_model.",
    )
    builder_model: str = Field(
        default="",
        description="Builder-stage chat model. Empty = use llm_model.",
    )
    fix_model: str = Field(
        default="",
        description=(
            "Fix-retry chat model. Empty = use llm_model. "
            "Operators often point this at a stronger model than builder."
        ),
    )
    critic_model: str = Field(
        default="",
        description="Optional critic-stage chat model. Empty = use llm_model.",
    )

    # ------------------------------------------------------------------
    # LLM sampling & timeout
    # ------------------------------------------------------------------
    llm_temperature: float = Field(
        default=0.2,
        description="Default sampling temperature for chat stages.",
    )
    llm_timeout_sec: float = Field(
        default=180.0,
        description="HTTP timeout (seconds) for a single LLM invocation.",
    )
    chat_request_timeout_sec: float = Field(
        default=180.0,
        description=(
            "Wall-clock budget (seconds) for a full /chat pipeline run "
            "(plan → build → validate → optional fix → optional deploy). "
            "Raise this when swapping in slow remote LLMs."
        ),
    )
    planner_temperature: float | None = Field(
        default=None,
        description="Planner-stage temperature. None = llm_temperature.",
    )
    builder_temperature: float | None = Field(
        default=None,
        description="Builder-stage temperature. None = llm_temperature.",
    )
    fix_temperature: float | None = Field(
        default=0.0,
        description=(
            "Fix-retry temperature. Defaults to 0.0 for deterministic repair. "
            "Set explicitly to override."
        ),
    )
    critic_temperature: float | None = Field(
        default=0.0,
        description="Critic-stage temperature. Defaults to 0.0.",
    )

    # ------------------------------------------------------------------
    # Agent graph control
    # ------------------------------------------------------------------
    agent_max_retries: int = Field(
        default=2,
        ge=0,
        description="Max fix/rebuild attempts after a validation failure.",
    )
    builder_prompt_char_budget: int = Field(
        default=12000,
        gt=0,
        description=(
            "Rough char budget for the builder/fix prompt. Above this, "
            "the builder drops trailing definitions before calling the LLM."
        ),
    )

    # ------------------------------------------------------------------
    # RAG / vector store
    # ------------------------------------------------------------------
    vector_store_backend: str = Field(
        default="chroma",
        description=(
            "Vector store backend. Currently: `chroma`. "
            "Extension points for `qdrant` / `pinecone` / `weaviate` live in "
            "app/rag/vector_store.py — add an implementation and register it "
            "in the factory."
        ),
    )
    chroma_path: str = Field(
        default=str(_PROJECT_ROOT / ".chroma"),
        description="Local filesystem path for the Chroma persistent store.",
    )
    rag_distance_metric: str = Field(
        default="cosine",
        description=(
            "Vector similarity metric: `cosine` | `l2` | `ip`. "
            "Chroma maps this onto its `hnsw:space` collection metadata."
        ),
    )
    rag_discovery_k: int = Field(
        default=8,
        gt=0,
        description="Top-k hits returned by the planner-facing discovery search.",
    )
    rag_detailed_k: int = Field(
        default=3,
        gt=0,
        description="Top-k hits for the builder-facing fallback detailed search.",
    )
    embed_batch_size: int = Field(
        default=32,
        gt=0,
        description="Batch size for embedding calls during ingest.",
    )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO", description="Python logging level.")

    # ------------------------------------------------------------------
    # Derived accessors
    # ------------------------------------------------------------------
    def model_for(self, stage: str) -> str:
        """Resolve a per-stage chat model, falling back to `llm_model`."""
        override = {
            "planner": self.planner_model,
            "builder": self.builder_model,
            "fix": self.fix_model,
            "critic": self.critic_model,
        }.get(stage, "")
        return override or self.llm_model

    def temperature_for(self, stage: str) -> float:
        """Resolve a per-stage temperature, falling back to `llm_temperature`."""
        override = {
            "planner": self.planner_temperature,
            "builder": self.builder_temperature,
            "fix": self.fix_temperature,
            "critic": self.critic_temperature,
        }.get(stage)
        return self.llm_temperature if override is None else override

    @property
    def effective_embed_base_url(self) -> str:
        """Fall back to openai_base_url when embed_base_url is empty."""
        return self.embed_base_url or self.openai_base_url

    @property
    def effective_embed_api_key(self) -> str:
        """R-CONF-02: fall back to openai_api_key when embed_api_key is empty."""
        return self.embed_api_key or self.openai_api_key

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached `Settings` instance."""
    return Settings()
