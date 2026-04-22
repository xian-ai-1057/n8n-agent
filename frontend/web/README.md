# Frontend — Web UI (n8n Workflow Builder)

Static HTML/CSS/JSX React-in-browser frontend based on the Claude Design
handoff (`n8n Workflow Builder UI.html`). Three-column layout: sidebar
(history · recent deploys · backend health) · chat · live workflow preview
(diagram / JSON / 問題 tabs).

Talks to the FastAPI backend at `POST /chat` and `GET /health`.

## Features

- ChatGPT/Claude-style three-column layout, warm soft-violet accent
- Light / dark theme toggle (persisted in localStorage)
- Comfortable / compact density toggle (persisted)
- Live backend health strip (OpenAI · n8n · Chroma), auto-refresh every 30s
- Inline Backend URL editor (persisted)
- Workflow diagram with drag-to-pan, node inspector drawer, kind-colored nodes
- JSON view with copy-to-clipboard
- Validator "問題" tab with jump-to-node buttons
- Pipeline pill track (plan → build → assemble → validate → deploy)
- Staged thinking animation while the backend 180s call is in flight

## Prerequisites

- Backend running at `http://localhost:8000` (FastAPI; see project root README).
- CORS in the backend already allows `http://localhost:8501`.
- Modern browser (Chrome, Safari, Firefox). The page uses React + Babel
  served from unpkg — no build step.

## Run

From the project root:

```bash
python3 -m http.server 8501 --directory frontend/web
```

Then open <http://localhost:8501>.

To point at a different backend, either edit it in the sidebar, or pass a
query string:

```
http://localhost:8501/?backend=http://192.168.1.50:8000
```

## Configuration

| Storage key                     | Default                   | Notes                          |
|---------------------------------|---------------------------|--------------------------------|
| `n8n_builder_backend_url`       | `http://localhost:8000`   | Backend base URL               |
| `n8n_builder_theme`             | `light`                   | `light` or `dark`              |
| `n8n_builder_density`           | `comfortable`             | `comfortable` or `compact`     |

## File layout

```
frontend/web/
├── index.html              # production entry (no design-canvas wrapper)
├── README.md
└── src/
    ├── styles-base.css         # tokens, buttons, node cards, diagram
    ├── styles-conservative.css # three-column app + production overrides
    ├── mock-data.jsx           # icons, pipeline stages, sample prompts
    ├── api.jsx                 # fetch wrappers for /chat and /health
    ├── ui-primitives.jsx       # Icon, StagePill, PipelineTrack, NodeCard…
    ├── workflow-diagram.jsx    # SVG layout + pan
    ├── workflow-preview.jsx    # diagram / JSON / errors tabs + inspector
    ├── conservative-parts.jsx  # Sidebar, EmptyState, Message, Composer
    └── conservative-app.jsx    # orchestrator (calls /chat, /health)
```

## Known limits

- Backend does not stream stages, so the pipeline pill animation during the
  request is approximated on the client (timings tuned for the 180s budget).
- Chat history / recent-deploys are kept in client memory only (backend has
  no persistent history endpoint yet).
- The legacy Streamlit UI (`frontend/app.py`) is retained as-is for users
  who prefer it.
