# D0-3：Dev / Ops

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-1, D0-2

## Purpose

描述本機開發、啟動、環境變數、run 指令與測試策略。Phase 1-A 依本文寫 `docker-compose.yml` 與 `.env.example`；Phase 1-B/C 依本文 run 指令驗收。

## Inputs

- D0-1 技術決策
- 使用者本機狀態：macOS / Linux、已有 OpenAI 相容推論伺服器（vllm / OpenAI / LiteLLM 等）可服務指定的 chat + embedding 模型、Docker Desktop 已安裝。

## Outputs

- 本機 bootstrap 指令序列。
- `.env.example` 欄位表。
- Phase-wise run 指令。
- 測試策略 matrix。

## Contracts

### 1. 目錄結構（對應 D0-1）

```
n8n_agent/
├── docker-compose.yml
├── .env.example
├── docs/                            # 本 spec 集
├── data/
│   ├── nodes/
│   │   ├── catalog_discovery.json   # 從 xlsx 轉出（529 筆）
│   │   └── definitions/             # 30 筆詳細參數 JSON
│   └── chroma/                      # ChromaDB persistent（gitignore）
├── scripts/
│   ├── xlsx_to_catalog.py
│   └── bootstrap_rag.py
├── backend/
│   ├── pyproject.toml
│   └── app/
│       ├── main.py                  # FastAPI
│       ├── config.py                # pydantic-settings
│       ├── models/                  # 來自 D0-2
│       ├── agent/                   # 對應 C1-1
│       │   ├── graph.py
│       │   ├── planner.py  builder.py  assembler.py
│       │   ├── validator.py  deployer.py
│       │   └── prompts/             # 來自 R2-3
│       ├── rag/                     # 對應 C1-2
│       ├── n8n/                     # 對應 C1-3
│       └── agent/llm.py             # OpenAI 相容 chat 包裝
├── frontend/
│   ├── app.py                       # 對應 C1-6
│   └── requirements.txt
└── tests/
    ├── unit/
    └── e2e/
```

### 2. 環境變數

於 repo 根 `.env.example`；Phase 1-A 產生，開發者複製為 `.env`。

| 變數 | 預設 | 用途 |
|---|---|---|
| `N8N_URL` | `http://localhost:5678` | backend 從 host 連 n8n 時使用 |
| `N8N_API_KEY` | _(空)_ | n8n UI Settings → n8n API 產生後填入；header `X-N8N-API-KEY` |
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | OpenAI 相容推論端點（vllm / OpenAI / LiteLLM）；容器內用 `http://host.docker.internal:8000/v1` |
| `OPENAI_API_KEY` | `EMPTY` | Bearer token；vllm 不驗證，OpenAI 需填真實金鑰 |
| `LLM_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | 生成 LLM；需對應伺服器實際 served model id |
| `EMBED_MODEL` | `BAAI/bge-m3` | Embedding；同樣需對應伺服器 served model id |
| `CHROMA_PATH` | `./data/chroma` | ChromaDB persist dir |
| `LOG_LEVEL` | `INFO` | backend log level |
| `BACKEND_URL` | `http://localhost:8000` | 供 Streamlit 呼叫 |

若未直接以 Docker 跑 backend（MVP 推薦裸跑 Python 以便 debug），`OPENAI_BASE_URL` 可設為 `http://localhost:8000/v1`（對應本機 vllm）。

### 3. 本機 bootstrap

前置檢查：

```bash
# 1) Docker
docker --version

# 2) 推論端點健檢（確認 chat + embedding 模型都已服務）
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" | jq '.data[].id'

# 3) Python 3.11
python3.11 --version
```

安裝 backend 依賴（uv 推薦）：

```bash
cd backend
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .
```

啟動 n8n：

```bash
docker compose up -d n8n
# 首次：打開 http://localhost:5678 建立 owner 帳號
# Settings → n8n API → Create API Key → 複製到 .env 的 N8N_API_KEY
```

產生節點資料 + 向量庫：

```bash
python scripts/xlsx_to_catalog.py          # 產生 data/nodes/catalog_discovery.json
python scripts/bootstrap_rag.py             # ingest discovery + detailed 到 Chroma
```

### 4. Run 指令

| 目標 | 指令 | 備註 |
|---|---|---|
| n8n | `docker compose up -d n8n` | compose 只管 n8n |
| Backend（dev） | `cd backend && uvicorn app.main:app --reload --port 8000` | 從 host 直接跑 |
| Frontend | `cd frontend && streamlit run app.py` | 預設 :8501 |
| CLI 單次跑 Agent | `cd backend && python -m app.agent.graph "<prompt>"` | Phase 2-B 驗收用 |
| 重建 RAG | `python scripts/bootstrap_rag.py --force` | 節點 JSON 更動後 |
| Unit 測試 | `cd backend && pytest tests/unit -q` | |
| E2E 測試 | `cd backend && pytest tests/e2e -q` | 需 n8n + OpenAI 相容端點可達 |

### 5. docker-compose 最小內容（Phase 1-A 參考）

```yaml
services:
  n8n:
    image: n8nio/n8n:1.123.31
    ports: ["5678:5678"]
    volumes: [".n8n_data:/home/node/.n8n"]
    environment:
      - N8N_HOST=localhost
      - N8N_PORT=5678
      - N8N_PROTOCOL=http
      - GENERIC_TIMEZONE=Asia/Taipei
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Backend 與 frontend MVP 不入 compose（便於熱重載、IDE debug）。

### 6. 測試策略

| 層級 | 目標 | 工具 | 位置 |
|---|---|---|---|
| Unit | Pydantic 模型、validator 規則、n8n client 欄位過濾、prompt 渲染 | pytest | `tests/unit/` |
| Component | Retriever 回傳排序、assembler 輸出結構 | pytest + 固定 fixture | `tests/unit/` |
| E2E Smoke | Plan §Verification 三情境各跑 3 次 | pytest + 真實 OpenAI 相容端點 + n8n | `tests/e2e/` |

MVP 不做負載測試、不做 LLM 輸出 regression 評估（記錄到 D0-1 §非功能目標）。

### 7. Logging

- backend 用 `structlog` 或 `logging` + JSON formatter 皆可；key 欄位：`event`, `stage`, `retry_count`, `latency_ms`。
- 每個 LangGraph 節點進出都要留一條 log；validator errors 以 `event=validation_failed` 整份附上。

## Errors

- OpenAI 相容端點不可達 → backend `/health` 回 `{"openai": false}`；`/chat` 直接 503。
- n8n 不可達 → `/health` 回 `{"n8n": "down"}`；deployer 階段拋 `DeployError`（見 C1-3）。
- Chroma 目錄權限錯 → ingest script 直接 raise，不 swallow。
- `.env` 缺 `N8N_API_KEY` → backend 啟動時 fail-fast（`config.py` 用 `pydantic-settings` 必填驗證）。

## Acceptance Criteria

- [ ] `.env.example` 欄位與本表一致。
- [ ] `docker compose up -d n8n` 後 n8n UI :5678 可達。
- [ ] `python scripts/bootstrap_rag.py` 完成後 `data/chroma/` 有兩個 collection（見 C1-2）。
- [ ] `uvicorn app.main:app` 啟動後 `GET /health` 三項皆 ok。
- [ ] `streamlit run frontend/app.py` 可送訊息並看見後端回覆。
- [ ] `pytest tests/unit -q` 全綠。
