# D0-3：Dev / Ops

> **版本**: v1.1.0 ｜ **狀態**: Draft ｜ **前置**: D0-1, D0-2

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
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | OpenAI 相容推論端點（vllm / OpenAI / LiteLLM）；容器內用 `http://host.docker.internal:8000/v1`。預設同時供 chat 與 embeddings 使用（若未設 `EMBED_BASE_URL`） |
| `EMBED_BASE_URL` | _(空 → fallback to `OPENAI_BASE_URL`)_ | OpenAI 相容 embeddings 端點。設值時只影響 embedding 呼叫（C1-2 RAG），chat LLM 仍走 `OPENAI_BASE_URL`。用途：chat 與 embedding 掛在不同伺服器（例如 LLM 走 vllm、embedding 走 Ollama / TEI）。**API key 沿用 `OPENAI_API_KEY`**（本期不分離；若未來兩端需要不同 key 再新增 `EMBED_API_KEY`，屬後續 spec）。參見 R-CONF-01（C1-2 §10）。 |
| `OPENAI_API_KEY` | `EMPTY` | Bearer token；vllm 不驗證，OpenAI 需填真實金鑰。同時用於 chat 與 embedding 端點 |
| `LLM_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | 生成 LLM；需對應伺服器實際 served model id |
| `EMBED_MODEL` | `BAAI/bge-m3` | Embedding；需對應 `EMBED_BASE_URL`（或 fallback 的 `OPENAI_BASE_URL`）實際 served model id |
| `CHROMA_PATH` | `./data/chroma` | ChromaDB persist dir |
| `LOG_LEVEL` | `INFO` | backend log level |
| `BACKEND_URL` | `http://localhost:8000` | 供 Streamlit 呼叫 |

若未直接以 Docker 跑 backend（MVP 推薦裸跑 Python 以便 debug），`OPENAI_BASE_URL` 可設為 `http://localhost:8000/v1`（對應本機 vllm）。

#### 2.1 模型配置（分階段）

為讓 Planner / Builder / Fix / Critic 等不同推論階段能獨立掛載不同模型與溫度（常見場景：fix 階段用較強的 model、critic 用小而精準的 model），提供下列進階環境變數。**全部為選填**；未設值則沿用頂層 `LLM_MODEL`，行為與 v1.0 相同。

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `PLANNER_MODEL` | `$LLM_MODEL` | Planner 階段用的 chat model；不設則沿用 LLM_MODEL |
| `BUILDER_MODEL` | `$LLM_MODEL` | Builder（首跑）階段用的 chat model |
| `FIX_MODEL` | `$LLM_MODEL` | Fix retry 階段；建議用比 builder 強的 model（fix 是更難的任務） |
| `CRITIC_MODEL` | `$LLM_MODEL` | C1-7 critic LLM；建議用小但精準的 model |
| `RERANKER_MODEL` | `` (空＝停用 reranker) | Cross-encoder 或 LLM-as-reranker model id；空則 RAG 走純 cosine |
| `PLANNER_TEMPERATURE` | `0.2` | |
| `BUILDER_TEMPERATURE` | `0.2` | |
| `FIX_TEMPERATURE` | `0.0` | Fix 需要 deterministic，預設降到 0 |
| `CRITIC_TEMPERATURE` | `0.0` | |
| `EMBED_PROMPT_PROFILE` | `auto` | 嵌入 prompt profile：`auto` / `embeddinggemma` / `bge` / `openai` / `none`。`auto` 則依 `EMBED_MODEL` id 推斷 |

> **向下相容**：若只設 `LLM_MODEL`，所有 stage 沿用舊行為；無破壞性變更。既有 `.env` 無需修改即可升級至 v1.1。

`.env` 範例（分階段模型；全部註解掉代表沿用 `LLM_MODEL`）：

```
# 進階：分階段模型（預設全部沿用 LLM_MODEL）
# PLANNER_MODEL=Qwen/Qwen2.5-7B-Instruct
# BUILDER_MODEL=Qwen/Qwen2.5-14B-Instruct
# FIX_MODEL=Qwen/Qwen2.5-32B-Instruct
# CRITIC_MODEL=Qwen/Qwen2.5-7B-Instruct
# RERANKER_MODEL=BAAI/bge-reranker-v2-m3
# FIX_TEMPERATURE=0.0
# EMBED_PROMPT_PROFILE=auto
```

#### 2.2 Runtime 調校參數（v1.2）

原本寫死在 agent / rag 模組裡的 knobs，現在可以用環境變數覆寫，不需要改 code
就能在 local / small cloud / large cloud 等部署情境之間搬動。所有變數皆**選
填**；未設值時使用 `app/config.py` 中的預設值（與 v1.1 行為一致）。

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `LLM_TEMPERATURE` | `0.2` | 所有未指定 stage 的預設 sampling 溫度 |
| `LLM_TIMEOUT_SEC` | `180` | 單次 LLM 呼叫的 HTTP timeout（秒）|
| `CHAT_REQUEST_TIMEOUT_SEC` | `180` | `/chat` 全流程 wall-clock budget（秒） |
| `AGENT_MAX_RETRIES` | `2` | Validator 失敗後 fix_build 的最大重試次數 |
| `BUILDER_PROMPT_CHAR_BUDGET` | `12000` | Builder/Fix prompt 字元上限；超過會裁掉尾端 definitions |
| `VECTOR_STORE_BACKEND` | `chroma` | 向量庫實作。`app/rag/vector_store.py` 留有 factory 擴充點 |
| `RAG_DISTANCE_METRIC` | `cosine` | 相似度度量：`cosine` / `l2` / `ip` |
| `RAG_DISCOVERY_K` | `8` | Planner discovery 檢索 top-k |
| `RAG_DETAILED_K` | `3` | Builder fallback 檢索 top-k |
| `EMBED_BATCH_SIZE` | `32` | Ingest 時呼叫 embedding 的批次大小 |

**部署 profile 範例**

- 本機開發（vllm 7B + 本地 Chroma）：全部沿用預設。
- 雲端大模型（slow & expensive）：`LLM_TIMEOUT_SEC=300`、`CHAT_REQUEST_TIMEOUT_SEC=600`、`AGENT_MAX_RETRIES=1`（減少重試以控成本）、`FIX_MODEL=gpt-4o`。
- 大 context 模型：`BUILDER_PROMPT_CHAR_BUDGET=40000`、`RAG_DETAILED_K=8`。
- 切換向量庫：實作 `VectorStore` protocol、在 `get_vector_store()` 註冊，再設 `VECTOR_STORE_BACKEND=<name>`。

### 3. 本機 bootstrap

前置檢查：

```bash
# 1) Docker
docker --version

# 2) 推論端點健檢（確認 chat + embedding 模型都已服務）
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" | jq '.data[].id'
# 若 chat 與 embedding 分別掛在不同伺服器，另外檢查 embedding 端點：
curl -s "${EMBED_BASE_URL:-$OPENAI_BASE_URL}/models" -H "Authorization: Bearer $OPENAI_API_KEY" | jq '.data[].id'

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

eval harness（D0-5）可透過上述分階段變數（`PLANNER_MODEL` / `BUILDER_MODEL` / `FIX_MODEL` / `CRITIC_MODEL` 等）做 A/B 模型比較。

### 7. Logging

- backend 用 `structlog` 或 `logging` + JSON formatter 皆可；key 欄位：`event`, `stage`, `retry_count`, `latency_ms`。
- 每個 LangGraph 節點進出都要留一條 log；validator errors 以 `event=validation_failed` 整份附上。

## Errors

- OpenAI 相容端點不可達 → backend `/health` 回 `{"openai": false}`；`/chat` 直接 503。
- Embedding 端點不可達（當 `EMBED_BASE_URL` 有設且無法連線；未設時 fallback 到 `OPENAI_BASE_URL`，同上）→ RAG ingest / retriever 啟動時 raise `EmbedderUnavailable`（C1-2 §Errors）。`/health` 目前不單獨探測 embedding 端點（若未來需要可列入後續 spec）。
- n8n 不可達 → `/health` 回 `{"n8n": "down"}`；deployer 階段拋 `DeployError`（見 C1-3）。
- Chroma 目錄權限錯 → ingest script 直接 raise，不 swallow。
- `.env` 缺 `N8N_API_KEY` → backend 啟動時 fail-fast（`config.py` 用 `pydantic-settings` 必填驗證）。

## Acceptance Criteria

- [ ] `.env.example` 欄位與本表一致。
- [ ] `EMBED_BASE_URL` 未設時，`OpenAIEmbedder` 的 `base_url` 應等於 `OPENAI_BASE_URL`（向後相容 v1.1；詳見 C1-2 §10 / R-CONF-01）。
- [ ] `docker compose up -d n8n` 後 n8n UI :5678 可達。
- [ ] `python scripts/bootstrap_rag.py` 完成後 `data/chroma/` 有兩個 collection（見 C1-2）。
- [ ] `uvicorn app.main:app` 啟動後 `GET /health` 三項皆 ok。
- [ ] `streamlit run frontend/app.py` 可送訊息並看見後端回覆。
- [ ] `pytest tests/unit -q` 全綠。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版 |
| v1.1.0 | 2026-04-21 | 新增分階段模型/溫度/embedding prompt profile 環境變數 |
| v1.2.0 | 2026-04-22 | 新增 runtime 調校參數（timeout/retries/prompt budget/RAG k/distance metric）與 VECTOR_STORE_BACKEND 擴充點 |
| v1.3.0 | 2026-04-23 | 新增 `EMBED_BASE_URL`（embedding 端點可獨立於 chat 端點；未設則 fallback 到 `OPENAI_BASE_URL`）。詳見 C1-2 §10 / R-CONF-01 |
