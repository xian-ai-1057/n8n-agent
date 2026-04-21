# n8n Workflow Builder Agent（繁中版）

以自然語言描述你想要的自動化流程，系統會透過 LLM Agent 產生合法的 n8n workflow JSON，經確定性規則驗證後部署到本機 n8n，並回傳可直接編輯的 URL。

> 英文版請見 [`README.md`](README.md)；完整設計規格索引見 [`docs/README.md`](docs/README.md)。

## 系統架構一覽

| 層 | 技術 | 角色 |
| --- | --- | --- |
| 前端 | Streamlit（`:8501`） | 對話介面、呼叫後端 `/chat`、顯示產出的 workflow 與錯誤 |
| 後端 | FastAPI + LangGraph（`:8000`） | Plan → Build → Assemble → Validate → Deploy 五階段 Agent |
| LLM | Ollama `qwen3.5:9b` | 規劃與節點生成（JSON schema 結構化輸出） |
| Embedding | Ollama `embeddinggemma` | 將節點目錄向量化 |
| 向量庫 | Chroma（`.chroma/`） | `catalog_discovery`（529 節點）、`catalog_detailed`（詳細參數） |
| 目標系統 | n8n `1.123.x`（Docker `:5678`） | 接收部署的 workflow JSON |

## 前置需求

- Docker Desktop（建議 28.x 以上）
- Python 3.11+
- 本機 Ollama 並已下載：
  - `qwen3.5:9b`
  - `embeddinggemma:latest`

檢查：

```bash
docker --version
python3.11 --version
ollama list
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

4. 確認 Ollama 模型已就緒：

   ```bash
   ollama list | grep -E 'qwen3\.5:9b|embeddinggemma'
   ```

5. 匯入節點目錄到 Chroma（首次執行）：

   ```bash
   python scripts/bootstrap_rag.py
   ```

6. 啟動後端（FastAPI，`:8000`）：

   ```bash
   OLLAMA_BASE_URL=http://localhost:11434 \
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
├── README.md / README.zh-TW.md     # 本檔
├── docs/
│   ├── L0-system/                  # 系統層規格（架構、資料模型、DevOps）
│   ├── L1-components/              # 元件層規格（Agent Graph / RAG / Validator / API / UI …）
│   ├── L2-reference/               # 參考資料（n8n schema、catalog schema、prompts）
│   ├── function_flow.md            # 函式呼叫流程（本次新增）
│   └── data_flow.md                # 資料流程（本次新增）
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
│   ├── api/routes.py               # `/health`、`/chat`
│   ├── agent/                      # LangGraph 節點與 graph 組裝
│   ├── models/                     # Pydantic SSOT（AgentState / Workflow / …）
│   ├── rag/                        # Chroma store、retriever、embedder
│   └── n8n/                        # n8n REST client
├── frontend/app.py                 # Streamlit UI
└── tests/                          # unit + integration
```

## 常見環境變數

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `N8N_URL` | `http://localhost:5678` | n8n 位置 |
| `N8N_API_KEY` | — | n8n API 金鑰（沒填則 `/chat` 走 dry-run） |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 位置 |
| `LLM_MODEL` | `qwen3.5:9b` | 生成模型 |
| `EMBED_MODEL` | `embeddinggemma` | Embedding 模型 |
| `CHROMA_PATH` | `.chroma` | Chroma 持久化目錄 |
| `LLM_TIMEOUT_SECONDS` | `180` | 單次 LLM 呼叫牆鐘逾時 |

## 延伸閱讀

- 整體架構：[`docs/L0-system/D0-1_Architecture.md`](docs/L0-system/D0-1_Architecture.md)
- 資料模型：[`docs/L0-system/D0-2_Data_Model.md`](docs/L0-system/D0-2_Data_Model.md)
- Agent Graph 契約：[`docs/L1-components/C1-1_Agent_Graph.md`](docs/L1-components/C1-1_Agent_Graph.md)
- 驗證規則：[`docs/L1-components/C1-4_Validator.md`](docs/L1-components/C1-4_Validator.md)
- 函式流程：[`docs/function_flow.md`](docs/function_flow.md)
- 資料流程：[`docs/data_flow.md`](docs/data_flow.md)
