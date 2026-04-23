# n8n Workflow Builder Agent（繁中版）

以自然語言描述你想要的自動化流程，系統會透過 LLM Agent 產生合法的 n8n workflow JSON，經確定性規則驗證後部署到本機 n8n，並回傳可直接編輯的 URL。

> 英文版請見 [`README.md`](README.md)；完整設計規格索引見 [`docs/README.md`](docs/README.md)。

## 系統架構一覽

| 層 | 技術 | 角色 |
| --- | --- | --- |
| 前端 | Streamlit（`:8501`） | 對話介面、呼叫後端 `/chat`、顯示產出的 workflow 與錯誤 |
| 後端 | FastAPI + LangGraph（`:8000`） | Plan → Build → Assemble → Validate → Deploy 五階段 Agent |
| LLM | OpenAI 相容端點（vllm / OpenAI / LiteLLM） | 規劃與節點生成（JSON schema 結構化輸出） |
| Embedding | OpenAI 相容端點 | 將節點目錄向量化 |
| 向量庫 | Chroma（`.chroma/`） | `catalog_discovery`（529 節點）、`catalog_detailed`（詳細參數） |
| 目標系統 | n8n `1.123.x`（Docker `:5678`） | 接收部署的 workflow JSON |

## 前置需求

- Docker Desktop（建議 28.x 以上）
- Python 3.11+
- 一個 OpenAI 相容推論端點，須同時服務一個 chat 模型和一個 embedding 模型。下列皆可：
  - vllm（`vllm serve --served-model-name ...`）——本機推薦
  - OpenAI（`https://api.openai.com/v1`）
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
   ```

   確認伺服器有同時服務上述兩個模型：

   ```bash
   curl -s http://localhost:8000/v1/models | jq '.data[].id'
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
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | OpenAI 相容推論端點（vllm / OpenAI / LiteLLM） |
| `OPENAI_API_KEY` | `EMPTY` | Bearer token；vllm 不驗證，OpenAI 需填真實金鑰 |
| `LLM_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | 生成模型 id（需對應伺服器實際 served model） |
| `EMBED_MODEL` | `BAAI/bge-m3` | Embedding 模型 id（需對應伺服器實際 served model） |
| `CHROMA_PATH` | `.chroma` | Chroma 持久化目錄 |
| `LLM_TIMEOUT_SECONDS` | `180` | 單次 LLM 呼叫牆鐘逾時 |

## 延伸閱讀

- 整體架構：[`docs/L0-system/D0-1_Architecture.md`](docs/L0-system/D0-1_Architecture.md)
- 資料模型：[`docs/L0-system/D0-2_Data_Model.md`](docs/L0-system/D0-2_Data_Model.md)
- Agent Graph 契約：[`docs/L1-components/C1-1_Agent_Graph.md`](docs/L1-components/C1-1_Agent_Graph.md)
- 驗證規則：[`docs/L1-components/C1-4_Validator.md`](docs/L1-components/C1-4_Validator.md)
- 函式流程：[`docs/function_flow.md`](docs/function_flow.md)
- 資料流程：[`docs/data_flow.md`](docs/data_flow.md)
