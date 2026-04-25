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
- **Session ID** (`C1-6:CHAT-UI-01`): `session_id` is written from the first
  server response and re-sent on every subsequent turn. Clearing history also
  resets the session (new conversation). A collapsed "Debug: Session ID"
  expander in the sidebar shows the current id.
- Main pane is a chat: each assistant turn shows `assistant_text` as the
  primary content, status, "Open in n8n" link (only when deployed), collapsed
  workflow JSON, collapsed tool-calls trace (debug), collapsed plan + retry
  count.
- **Plan approval card** (`C1-6:CHAT-UI-03`): when the backend returns
  `status="awaiting_plan_approval"`, a bordered card appears under the latest
  assistant message with "Confirm / Edit / Cancel" buttons. Confirm and Cancel
  inject a fixed phrase as the next user turn; Edit shows a free-text area.
- **Error handling** (`C1-6:CHAT-UI-02`):
  - HTTP 504 / client timeout → toast "處理逾時，請重試"; `session_id` preserved.
  - HTTP 404 → "session 已過期，將開新 session"; `session_id` cleared.
  - HTTP 400 → shows backend `error_message`.
  - `status="error"` in 200 response → `st.error` inline; chat continues.
- The HTTP client timeout is 200 s (backend budget is 180 s).
