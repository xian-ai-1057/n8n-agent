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

    ollama_base_url: str = Field(
        default="http://host.docker.internal:11434",
        description="Base URL for the Ollama HTTP API.",
    )
    llm_model: str = Field(default="qwen3.5:9b", description="Generation model tag.")
    embed_model: str = Field(default="embeddinggemma:latest", description="Embedding model tag.")

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
