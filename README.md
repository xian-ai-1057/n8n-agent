# n8n Workflow Builder Agent (MVP)

A conversational n8n workflow builder: describe what you want in natural
language, an LLM agent drafts a valid n8n workflow JSON, validates it with
deterministic rules, deploys it to a local n8n instance, and returns an
editor URL. See the approved plan
`/Users/kee/.claude/plans/n8n-workflow-builder-agent-snazzy-otter.md` and the
spec set under `docs/` for the full design.

## Prerequisites

- Docker Desktop (tested with Docker 28.x)
- Python 3.11+ (backend runs on host during MVP for fast iteration)
- Ollama running on the host with the following models already pulled:
  - `qwen3.5:9b`       (generation LLM)
  - `embeddinggemma:latest` (embeddings for RAG)

Verify with:

```bash
docker --version
python3.11 --version
ollama list
```

## Quickstart

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Start n8n:

   ```bash
   docker compose up -d
   ```

3. Open <http://localhost:5678>, create the owner account, then go to
   **Settings -> n8n API -> Create an API key**, copy the value, and paste it
   into `.env` as `N8N_API_KEY=...`.

4. Confirm the Ollama models are available on the host:

   ```bash
   ollama list | grep -E 'qwen3\.5:9b|embeddinggemma'
   # If either is missing:
   #   ollama pull qwen3.5:9b
   #   ollama pull embeddinggemma:latest
   ```

5. Run the backend + frontend.

   **Backend** (FastAPI on :8000) — note the `--app-dir backend` flag; without
   it you'll hit `ModuleNotFoundError: app`:

   ```bash
   OLLAMA_BASE_URL=http://localhost:11434 \
   /opt/miniconda3/envs/agent/bin/python -m uvicorn app.main:app \
       --app-dir backend --host 0.0.0.0 --port 8000 --reload
   ```

   Smoke-check once it's up:

   ```bash
   curl -s http://localhost:8000/health | jq
   curl -s -X POST http://localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"message":"每小時抓 https://api.github.com/zen 存到 Google Sheet"}' \
     --max-time 200 | jq .ok
   ```

   The `/chat` call runs the full LangGraph pipeline (plan → build →
   assemble → validate → deploy) synchronously. Budget: **180 s**. If you
   reverse-proxy, set proxy read timeout to at least 200 s.

   **Frontend** (Streamlit on :8501):

   ```bash
   /opt/miniconda3/envs/agent/bin/pip install -r frontend/requirements.txt
   cd frontend
   /opt/miniconda3/envs/agent/bin/streamlit run app.py \
       --server.headless true --server.port 8501
   ```

   Then open <http://localhost:8501>.

## Project layout

```
n8n_agent/
├── docker-compose.yml              # this phase — n8n only
├── .env.example                    # this phase
├── README.md                       # this phase
├── docs/                           # L0 / L1 / L2 specs (Phase 0)
├── data/
│   └── nodes/
│       ├── catalog_discovery.json  # 529-node discovery index (Phase 1-B)
│       └── definitions/            # ~30 detailed node JSONs (Phase 1-B)
├── scripts/
│   ├── xlsx_to_catalog.py          # Phase 1-B
│   └── bootstrap_rag.py            # Phase 2-A
├── backend/                        # Phase 1-B / 1-C / 2 / 3
│   └── app/
│       ├── main.py                 # FastAPI
│       ├── models/                 # Pydantic SSOT
│       ├── agent/                  # LangGraph: planner/builder/assembler/validator/deployer
│       ├── rag/                    # Chroma ingest + retriever
│       ├── n8n/                    # n8n REST client
│       └── llm/                    # Ollama wrapper
├── frontend/                       # Streamlit UI (Phase 3)
└── tests/                          # unit + e2e (Phase 1-C onward)
```

## Further reading

- Full spec index: [`docs/README.md`](docs/README.md)
