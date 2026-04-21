"""Application settings (Implements D0-3).

Reads environment variables (optionally from a `.env` file at the project root)
using `pydantic-settings`. Exposes a cached `get_settings()` factory.
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

    n8n_url: str = Field(default="http://localhost:5678", description="n8n base URL.")
    n8n_api_key: str = Field(default="", description="X-N8N-API-KEY value.")

    # OpenAI-compatible inference endpoint. Works with:
    # - OpenAI API (https://api.openai.com/v1)
    # - vllm `--served-model-name` deployments (http://host:8000/v1)
    # - Any other OpenAI-compatible gateway (LiteLLM, OpenRouter, etc.)
    openai_base_url: str = Field(
        default="http://localhost:8000/v1",
        description="Base URL for the OpenAI-compatible chat/embeddings API.",
    )
    openai_api_key: str = Field(
        default="EMPTY",
        description="API key sent as `Authorization: Bearer`. vllm accepts any value.",
    )
    llm_model: str = Field(
        default="Qwen/Qwen2.5-7B-Instruct",
        description="Chat completion model name (must match the backend's served model).",
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

    chroma_path: str = Field(
        default=str(_PROJECT_ROOT / ".chroma"),
        description="Local filesystem path for the Chroma persistent store.",
    )
    log_level: str = Field(default="INFO", description="Python logging level.")

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
