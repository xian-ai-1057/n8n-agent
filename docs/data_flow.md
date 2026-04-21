# 資料流程（Data Flow）

本文件描述資料在系統中的「來源 → 轉換 → 去向」，以及跨元件傳遞時採用的型別與儲存位置。

> 相關規格：[D0-2 Data Model](L0-system/D0-2_Data_Model.md)、[C1-2 RAG](L1-components/C1-2_RAG.md)、[R2-1 n8n Workflow Schema](L2-reference/R2-1_n8n_Workflow_Schema.md)。

## 0. 參與者與位置

| 角色 | 位置 | 狀態 |
| --- | --- | --- |
| 使用者 | Streamlit（`:8501`） | 在 `st.session_state.messages` 維持單一 session |
| Backend | FastAPI（`:8000`） | stateless；每次請求重新組 `AgentState` |
| Chroma | `.chroma/`（本機檔案） | 持久化；兩個 collection |
| Ollama | host `:11434` | 提供生成與 embedding |
| n8n | Docker（`:5678`） | 以自身 SQLite 保存 workflow |

## 1. 索引資料（離線，一次性）

這條流程只在節點目錄更新時執行，用 `scripts/bootstrap_rag.py`。

```
n8n_official_nodes_reference.xlsx
        │  (scripts/xlsx_to_catalog.py)
        ▼
data/nodes/catalog_discovery.json   ← 529 個節點摘要
data/nodes/definitions/*.json       ← ~30 個節點的完整參數
        │  (scripts/bootstrap_rag.py)
        ▼
┌──────────────────────────────────────────────┐
│ Chroma  (.chroma/)                            │
│ ├── collection: catalog_discovery (embedded)  │
│ │     doc = description / keywords            │
│ │     meta = {type, display_name, category}   │
│ └── collection: catalog_detailed (exact-lookup)│
│       meta = {type, raw: NodeDefinition JSON} │
└──────────────────────────────────────────────┘
```

Embedding 由 `OllamaEmbedder` 透過 `embeddinggemma` 模型產生，寫入時一併保存。

## 2. 線上請求流（Online）

### 2.1 請求進入

```
使用者輸入："每小時抓 GitHub API 存到 Google Sheet"
        ▼
ChatRequest { message: str }           ← models/api.py
```

### 2.2 Plan 階段

```
ChatRequest.message ──▶ Retriever.search_discovery(k=8)
                           │
                           ├─ embedder: text → vector (768d)
                           └─ Chroma: cosine → 8 × NodeCatalogEntry
                           
候選節點 + user_message ──▶ Planner Prompt ──▶ Ollama(qwen3.5:9b)
                                                 ▼
                                     PlannerOutput { steps: StepPlan[] }
                                     StepPlan { intent, description,
                                                candidate_types[] }
```

寫回 `AgentState.plan` 與 `AgentState.discovery_hits`。

### 2.3 Build 階段

```
每個 StepPlan.candidate_types[0] ──▶ Retriever.get_detail(type)
                                            ▼
                                NodeDefinition { parameters, credentials… }

plan + definitions + user_message ──▶ Builder Prompt ──▶ Ollama
                                                           ▼
                                          BuilderOutput {
                                            nodes: BuiltNode[],
                                            connections: Connection[]
                                          }
```

若是重試（來自 `fix_build`），prompt 會額外帶入上一輪的 `ValidationReport.errors`。

### 2.4 Assemble 階段（純程式、無 LLM）

```
BuiltNode[] + Connection[] + user_message
        ▼
assemble_workflow()
  - 指派 UUID
  - 計算 position.x / position.y（分支 ± 200）
  - 推斷 workflow name
  - settings.executionOrder = "v1"
        ▼
WorkflowDraft {
  name, nodes[], connections{}, settings
}
```

### 2.5 Validate 階段（純程式、無 LLM）

```
WorkflowDraft ──▶ WorkflowValidator.validate()
                     ├─ 讀 catalog_discovery.json（type 是否存在）
                     └─ 套用 19 條規則
                     ▼
             ValidationReport { ok, errors[], warnings[] }
```

### 2.6 路由

```
validation.ok? ── 是 ─▶ Deploy
               └─ 否 ─▶ retry_count<2? ── 是 ─▶ Fix Build（回到 2.3）
                                       └─ 否 ─▶ Give Up
```

### 2.7 Deploy 階段

```
WorkflowDraft ──▶ N8nClient.create_workflow()
                     ├─ 去除唯讀欄位（id, active, createdAt…）
                     └─ 遷移 continueOnFail → onError
                     ▼
     POST {N8N_URL}/api/v1/workflows  (header: X-N8N-API-KEY)
                     ▼
             n8n 回 { id, name, ... }
                     ▼
     WorkflowDeployResult { id, url: "{N8N_URL}/workflow/{id}" }
```

n8n 自己的 SQLite 會永久保存此 workflow。

### 2.8 回應組裝

```
AgentState ──▶ _state_to_response()
                  ▼
ChatResponse {
  ok: bool,
  workflow_id: str | null,
  workflow_url: str | null,
  workflow_json: dict | null,
  errors: ValidationIssue[],
  retry_count: int,
  messages: ChatMessage[],
  elapsed_ms: int
}
```

前端把結果塞進 `st.session_state.messages` 並渲染。

## 3. AgentState（單一事實來源）

`models/agent_state.py::AgentState` 在 graph 期間持續被各節點合併更新。重要欄位：

| 欄位 | 寫入者 | 內容 |
| --- | --- | --- |
| `user_message` | handler | 原始輸入 |
| `discovery_hits` | planner | 向量搜尋結果 |
| `plan` | planner | `StepPlan[]` |
| `candidates` | builder | 每步使用的候選 `NodeDefinition` |
| `built_nodes` / `connections` | builder | LLM 產出 |
| `draft` | assembler | `WorkflowDraft` |
| `validation` | validator | `ValidationReport` |
| `retry_count` | fix_build | 0 / 1 / 2 |
| `workflow_id` / `workflow_url` | deployer | n8n 回傳 |
| `messages` | 各節點 | 內部日誌（可出現在 `/chat` 回應） |
| `error` | give_up / catch | 終止原因 |

## 4. 資料邊界與持久性

| 資料 | 位置 | 生命週期 |
| --- | --- | --- |
| 使用者對話 | 前端 `st.session_state` | 瀏覽器 session（不持久化） |
| `AgentState` 中間值 | 後端 Python 物件 | 單次請求；結束即釋放 |
| 節點目錄 | `data/nodes/*.json` + `.chroma/` | 版本控管；手動重建 |
| 產生的 workflow | n8n SQLite | 永久，直到使用者在 n8n UI 刪除 |
| Ollama 模型 | 主機 Ollama 資料夾 | 由 `ollama` 管理 |

## 5. 外部呼叫與機密

| 呼叫 | 方向 | 認證 | 備註 |
| --- | --- | --- | --- |
| Frontend → Backend | HTTP JSON | 無（本機） | 200s timeout |
| Backend → Ollama | HTTP | 無 | `/api/chat`、`/api/embed` |
| Backend → Chroma | 本地 client | 無 | `PersistentClient(path=.chroma)` |
| Backend → n8n | HTTP JSON | `X-N8N-API-KEY`（從 `.env`） | 無 key 則走 dry-run |

所有機密僅來自 `.env`；專案不對外傳送任何資料（除呼叫本機 Ollama／n8n）。

## 6. 失敗時的資料狀態

| 失敗點 | `AgentState` | 回應 |
| --- | --- | --- |
| Plan LLM 逾時 | 只有 `user_message` | 500 + `error` |
| Build 產出 schema 不合 | `built_nodes` 可能為空 | 走 validator → fix 或 give_up |
| Validator 錯誤 | `validation.errors` 有值 | 若 retry 用盡則 `workflow_json` 仍回給使用者協助除錯 |
| Deploy 401 | `workflow_id` 為 None | `errors` 帶 n8n 原始訊息 |

此設計讓失敗回應仍能保留中間產物（plan、draft、validation），方便使用者在前端看出問題並調整描述。
