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
- An OpenAI-compatible inference endpoint that serves both a chat model and
  an embedding model. Any of these work:
  - vllm (`vllm serve --served-model-name ...`) — recommended for local
  - OpenAI (`https://api.openai.com/v1`)
  - LiteLLM / OpenRouter / any other OpenAI-compatible gateway

Verify with:

```bash
docker --version
python3.11 --version
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" | jq
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

4. Point the backend at your inference server. Edit `.env`:

   ```bash
   OPENAI_BASE_URL=http://localhost:8000/v1   # e.g. local vllm
   OPENAI_API_KEY=EMPTY                       # any non-empty string for vllm
   LLM_MODEL=Qwen/Qwen2.5-7B-Instruct         # must match server's served model
   EMBED_MODEL=BAAI/bge-m3                    # must match server's served model
   ```

   Confirm the server is serving both models:

   ```bash
   curl -s http://localhost:8000/v1/models | jq '.data[].id'
   ```

5. Run the backend + frontend.

   **Backend** (FastAPI on :8000) — note the `--app-dir backend` flag; without
   it you'll hit `ModuleNotFoundError: app`:

   ```bash
   OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY=EMPTY \
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
│       │   └── llm.py              # OpenAI-compatible chat wrapper
│       ├── rag/                    # Chroma ingest + retriever (OpenAI-compat embeddings)
│       └── n8n/                    # n8n REST client
├── frontend/                       # Streamlit UI (Phase 3)
└── tests/                          # unit + e2e (Phase 1-C onward)
```

## Further reading

- Full spec index: [`docs/README.md`](docs/README.md)
