# Frontend — Streamlit UI

Minimal Streamlit chat UI for the n8n workflow builder. Sends `POST /chat` to
the FastAPI backend and renders the resulting workflow JSON + n8n editor link.

## Prerequisites

- Backend running at `http://localhost:8000` (see project root README).
- Python environment with `streamlit`, `httpx`, `python-dotenv`.

## Install

```bash
/opt/miniconda3/envs/agent/bin/pip install -r frontend/requirements.txt
```

## Run

```bash
cd frontend
/opt/miniconda3/envs/agent/bin/streamlit run app.py \
    --server.headless true --server.port 8501
```

Then open <http://localhost:8501>.

## Env vars

| Var           | Default                     | Notes                                     |
|---------------|-----------------------------|-------------------------------------------|
| `BACKEND_URL` | `http://localhost:8000`     | Used as sidebar default; override inline. |
| `N8N_URL`     | `http://localhost:5678`     | Used to build "Open in n8n" link when the backend only returns an id. |

## UX

- Sidebar exposes backend URL, n8n URL, health-check button, clear-history.
- Main pane is a chat: each assistant turn shows status, "Open in n8n" link
  button, collapsed workflow JSON, collapsed plan + retry count.
- The HTTP client timeout is 200 s (backend budget is 180 s).
