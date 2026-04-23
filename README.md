# n8n Workflow Builder Agent（繁中版）

以自然語言描述你想要的自動化流程，系統會透過 LLM Agent 產生合法的 n8n workflow JSON，經確定性規則驗證後部署到本機 n8n，並回傳可直接編輯的 URL。

> 英文版請見 [`README.md`](README.md)；完整設計規格索引見 [`docs/README.md`](docs/README.md)。

## 系統架構一覽

| 層 | 技術 | 角色 |
| --- | --- | --- |
| 前端 | Streamlit（`:8501`） | 對話介面、呼叫後端 `/chat`、顯示產出的 workflow 與錯誤 |
| 後端 | FastAPI + LangGraph（`:8000`） | Plan → Build → Assemble → Validate → Deploy 五階段 Agent |
| LLM | OpenAI 相容端點（vllm / OpenAI / LiteLLM） | 規劃與節點生成（JSON schema 結構化輸出） |
| Embedding | OpenAI 相容端點(可與 LLM 共用或獨立) | 將節點目錄向量化 |
| 向量庫 | Chroma（`.chroma/`） | `catalog_discovery`（529 節點）、`catalog_detailed`（詳細參數） |
| 目標系統 | n8n `1.123.x`（Docker `:5678`） | 接收部署的 workflow JSON |

## 前置需求

- Docker Desktop（建議 28.x 以上）
- Python 3.11+
- 一或兩個 OpenAI 相容推論端點。最簡單的情況是單一端點同時服務 chat 與 embedding(例如本機 vllm);也支援**將 chat 與 embedding 拆到不同端點 / 不同 provider**(R-CONF-01 / R-CONF-02),見下方〈拆分 LLM 與 Embedding 端點〉。可選的端點:
  - vllm(`vllm serve --served-model-name ...`)——本機推薦
  - OpenAI(`https://api.openai.com/v1`)
  - Ollama / TEI(embedding-only 常用)
  - LiteLLM / OpenRouter / 其他 OpenAI 相容閘道

檢查：

```bash
docker --version
python3.11 --version
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" | jq '.data[].id'
```

## 快速開始

1. 複製環境變數檔：

   ```bash
   cp .env.example .env
   ```

2. 啟動 n8n：

   ```bash
   docker compose up -d
   ```

3. 開啟 <http://localhost:5678>，建立 owner 帳號後到 **Settings → n8n API → Create an API key**，把金鑰貼到 `.env` 的 `N8N_API_KEY`。

4. 將後端指向你的推論伺服器。編輯 `.env`：

   ```bash
   OPENAI_BASE_URL=http://localhost:8000/v1   # 例如本機 vllm
   OPENAI_API_KEY=EMPTY                       # vllm 只要非空字串；OpenAI 則填真實金鑰
   LLM_MODEL=Qwen/Qwen2.5-7B-Instruct         # 必須對應 server 實際服務的 model id
   EMBED_MODEL=BAAI/bge-m3                    # 必須對應 server 實際服務的 model id
   # 選填:embedding 走獨立端點時才設定,否則 fallback 到 OPENAI_BASE_URL
   # EMBED_BASE_URL=http://localhost:11434/v1
   # EMBED_API_KEY=
   ```

   確認伺服器有同時服務上述兩個模型:

   ```bash
   curl -s http://localhost:8000/v1/models | jq '.data[].id'
   # 若有設 EMBED_BASE_URL,另外驗證 embedding 端點:
   # curl -s "$EMBED_BASE_URL/models" -H "Authorization: Bearer $EMBED_API_KEY" | jq '.data[].id'
   ```

5. 匯入節點目錄到 Chroma（首次執行）：

   ```bash
   python scripts/bootstrap_rag.py
   ```

6. 啟動後端（FastAPI，`:8000`）：

   ```bash
   OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY=EMPTY \
   python -m uvicorn app.main:app \
       --app-dir backend --host 0.0.0.0 --port 8000 --reload
   ```

7. 啟動前端（Streamlit，`:8501`）：

   ```bash
   pip install -r frontend/requirements.txt
   streamlit run frontend/app.py --server.port 8501
   ```

8. 開啟 <http://localhost:8501>，輸入描述，例如：

   > 每小時抓 https://api.github.com/zen 存到 Google Sheet

## 冒煙測試

```bash
curl -s http://localhost:8000/health | jq
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"每小時抓 GitHub API 存到 Google Sheet"}' \
  --max-time 200 | jq .ok
```

`/chat` 會同步執行整個 LangGraph pipeline，單次預算 **180 秒**。若前面有 reverse proxy，`proxy_read_timeout` 請設到 200 秒以上。

## 專案結構

```
n8n_agent/
├── docker-compose.yml              # n8n 服務
├── .env.example                    # 環境變數樣板
├── CLAUDE.md                       # Claude Code 主 session orchestration 規則
├── README.md / README.zh-TW.md     # 本檔
├── .claude/
│   ├── agents/                     # 6 個 subagent 定義（spec-guardian、backend-engineer 等）
│   ├── scripts/
│   │   └── git_sync.sh             # 自動同步腳本
│   └── skills/
│       ├── spec-driven-workflow/   # Spec-driven 工作流程契約
│       └── traceability-audit/     # 三方一致性稽核（含 audit.py）
├── docs/
│   ├── L0-system/                  # 系統層規格（架構、資料模型、DevOps、MVP 結果、評估）
│   ├── L1-components/              # 元件層規格（C1-1 ~ C1-8，含 _TEMPLATE.md）
│   ├── L2-reference/               # 參考資料（n8n schema、catalog schema、prompts、templates）
│   ├── research/                   # 研究報告（bottleneck analysis、spec gap、n8n insights）
│   ├── agent_teams_master_reference.md  # Subagent 設計總覽
│   ├── function_flow.md            # 函式呼叫流程
│   └── data_flow.md                # 資料流程
├── data/nodes/
│   ├── catalog_discovery.json      # 529 個節點的索引
│   └── definitions/                # 約 30 個節點的完整參數定義
├── scripts/
│   ├── xlsx_to_catalog.py          # 由 xlsx 產生 catalog_discovery.json
│   ├── bootstrap_rag.py            # 將 catalog 匯入 Chroma
│   ├── validate_catalogs.py        # 資料完整性檢查
│   └── deploy_smoke.py             # n8n 部署冒煙測試
├── backend/app/
│   ├── main.py                     # FastAPI 進入點
│   ├── config.py                   # pydantic-settings
│   ├── api/routes.py               # `/health`、`/chat`、`/plan`
│   ├── agent/                      # LangGraph 節點與 graph 組裝（含 OpenAI 相容 chat 包裝）
│   ├── models/                     # Pydantic SSOT（AgentState / Workflow / …）
│   ├── rag/                        # Chroma store、retriever、OpenAI 相容 embedder
│   └── n8n/                        # n8n REST client
├── backend/tests/
│   ├── unit/                       # 單元測試（validator、assembler、RAG、timeout 等）
│   └── integration/                # 整合測試（API contract、n8n live、RAG live）
├── frontend/app.py                 # Streamlit UI
└── tests/eval/                     # Eval harness（prompts.yaml）
```

## 常見環境變數

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `N8N_URL` | `http://localhost:5678` | n8n 位置 |
| `N8N_API_KEY` | — | n8n API 金鑰（沒填則 `/chat` 走 dry-run） |
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | Chat LLM 端點(vllm / OpenAI / LiteLLM);embedding 預設也走這裡 |
| `OPENAI_API_KEY` | `EMPTY` | LLM 端點的 Bearer token;vllm 不驗證,OpenAI 需填真實金鑰 |
| `EMBED_BASE_URL` | _(空 → fallback `OPENAI_BASE_URL`)_ | **選填** — embedding 獨立端點(R-CONF-01),例如 chat 在 vllm、embedding 在 Ollama |
| `EMBED_API_KEY` | _(空 → fallback `OPENAI_API_KEY`)_ | **選填** — embedding 獨立 API key(R-CONF-02),跨 provider 時使用 |
| `LLM_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | 生成模型 id(需對應 LLM 伺服器實際 served model) |
| `EMBED_MODEL` | `BAAI/bge-m3` | Embedding 模型 id(需對應 embedding 伺服器實際 served model) |
| `CHROMA_PATH` | `.chroma` | Chroma 持久化目錄 |
| `LLM_TIMEOUT_SECONDS` | `180` | 單次 LLM 呼叫牆鐘逾時 |

## 拆分 LLM 與 Embedding 端點

預設情況 LLM 與 embedding 共用 `OPENAI_BASE_URL` / `OPENAI_API_KEY`(v1.1 行為)。若你的部署拓撲把兩個模型放在不同伺服器甚至不同 provider,可分別設定:

- `EMBED_BASE_URL`:embedding 專用端點。未設或空字串時 fallback 到 `OPENAI_BASE_URL`(R-CONF-01)
- `EMBED_API_KEY`:embedding 專用 API key。未設或空字串時 fallback 到 `OPENAI_API_KEY`(R-CONF-02)

Chat LLM 始終只看 `OPENAI_*`,不會被 `EMBED_*` 影響。

### 常見拓撲

| 情境 | `OPENAI_BASE_URL` | `OPENAI_API_KEY` | `EMBED_BASE_URL` | `EMBED_API_KEY` |
| --- | --- | --- | --- | --- |
| 本機 vllm 同時服務 chat + embed | `http://localhost:8000/v1` | `EMPTY` | _(空)_ | _(空)_ |
| Chat 在 vllm、embedding 在本機 Ollama | `http://localhost:8000/v1` | `EMPTY` | `http://localhost:11434/v1` | _(空)_ |
| Chat 在 vllm、embedding 走 OpenAI 雲端 | `http://localhost:8000/v1` | `EMPTY` | `https://api.openai.com/v1` | `sk-...` |
| Chat / embedding 各自不同雲端 provider | `https://api.openai.com/v1` | `sk-openai-...` | `https://api.voyageai.com/v1` | `pa-voyage-...` |

### 驗證

啟動後端後看 log:

```
backend up: ... openai=http://localhost:8000/v1 ... embed_url=http://localhost:11434/v1 (split) embed_key=fallback ...
```

- `(shared)` = 兩端點相同;`(split)` = embedding 走獨立端點
- `embed_key=set` = `EMBED_API_KEY` 已設定;`fallback` = 使用 `OPENAI_API_KEY`

`GET /health` 在 split 拓撲下會分別探測兩個端點,任一失敗整體 `ok=False`,error 訊息會標明是 `llm endpoint` 還是 `embed endpoint` 有問題。

> 注意:若切換到**不同模型**的 embedding 端點,需要重跑 `python scripts/bootstrap_rag.py --reset` 重建 Chroma collection(向量維度 / 語意空間不一致)。同模型、不同伺服器則不必重建。

## 延伸閱讀

- 整體架構:[`docs/L0-system/D0-1_Architecture.md`](docs/L0-system/D0-1_Architecture.md)
- 資料模型:[`docs/L0-system/D0-2_Data_Model.md`](docs/L0-system/D0-2_Data_Model.md)
- Agent Graph 契約:[`docs/L1-components/C1-1_Agent_Graph.md`](docs/L1-components/C1-1_Agent_Graph.md)
- RAG / Embedding 端點拆分(R-CONF-01 / R-CONF-02):[`docs/L1-components/C1-2_RAG.md`](docs/L1-components/C1-2_RAG.md) §10–11
- 驗證規則:[`docs/L1-components/C1-4_Validator.md`](docs/L1-components/C1-4_Validator.md)
- 函式流程:[`docs/function_flow.md`](docs/function_flow.md)
- 資料流程:[`docs/data_flow.md`](docs/data_flow.md)
