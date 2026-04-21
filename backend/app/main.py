"""FastAPI application entrypoint (Implements C1-5).

Run locally:

    OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY=EMPTY \\
    /opt/miniconda3/envs/agent/bin/python -m uvicorn app.main:app \\
        --app-dir backend --host 0.0.0.0 --port 8000 --reload

The ``--app-dir backend`` flag is important: app imports like
``app.agent.graph`` resolve relative to ``backend/`` (not the project root).

Keep-alive / read timeout: the ``/chat`` handler takes up to 180 s for the
LangGraph pipeline; if you reverse-proxy, set proxy read timeout >= 200 s.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import get_settings
from .request_context import RequestIdFilter

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(rid)s] %(name)s:%(lineno)d - %(message)s",
    )
    _install_rid_filter()

    app = FastAPI(
        title="n8n Workflow Builder",
        version="0.1.0",
        description="Conversational n8n workflow builder backend.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8501"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    logger.info(
        "backend up: n8n=%s openai=%s llm=%s embed=%s chroma=%s deploy_enabled=%s",
        settings.n8n_url,
        settings.openai_base_url,
        settings.llm_model,
        settings.embed_model,
        settings.chroma_path,
        bool(settings.n8n_api_key),
    )
    return app


def _install_rid_filter() -> None:
    """Attach RequestIdFilter to root + uvicorn handlers so every log has %(rid)s."""
    f = RequestIdFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(f)


app = create_app()
