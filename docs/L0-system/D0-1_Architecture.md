# D0-1：Architecture

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: 無 ｜ **SSOT**: plan `snazzy-otter`

## Purpose

定義 MVP 的系統總覽：元件切分、資料流、技術決策、範圍邊界與非功能目標。本 spec 是其他所有 spec 的起點；後續 D0-2/D0-3/C1-*/R2-* 皆依本文切分出的元件展開。

## Inputs

- 使用者自然語言需求（經 Streamlit）
- 現有資產：`n8n_official_nodes_reference.xlsx`（529 節點）、`99_Archive/n8n_Agent/` 可移植之程式碼與節點 JSON。
- 外部服務：OpenAI 相容推論端點（預設 vllm 本機 `http://localhost:8000/v1`；亦可指向 OpenAI / LiteLLM 等）、docker 上 n8n `1.123.31`。

## Outputs

- 單一 FastAPI backend + 單一 Streamlit frontend 的執行拓撲。
- 一份凍結的技術決策表。
- MVP 範圍邊界（in/out）與非功能目標。

## Contracts

### 1. 系統總覽（ASCII）

```
┌──────────────┐     HTTP JSON      ┌─────────────────────────────────────┐     REST     ┌────────────┐
│  Streamlit   │ ─────────────────▶ │  FastAPI  /chat  /health            │ ───────────▶ │  n8n REST  │
│  :8501       │ ◀─────────────── │    │                                  │ ◀─────────── │  :5678     │
└──────────────┘                    │    ▼                                │              └────────────┘
                                    │  LangGraph                          │
                                    │  plan → build → assemble →          │
                                    │  validate → (retry|deploy)          │
                                    └──┬──────────────────────────────┬───┘
                                       │                              │
                              ┌────────▼──────────┐        ┌──────────▼──────────┐
                              │ ChromaDB (local)  │        │ OpenAI-compat API   │
                              │  ├─ catalog_disco │        │  $LLM_MODEL         │
                              │  └─ catalog_detai │        │  $EMBED_MODEL       │
                              └───────────────────┘        └─────────────────────┘
```

### 2. 元件清單

| ID | 元件 | 技術 | Spec |
|---|---|---|---|
| FE | Streamlit UI | Streamlit | C1-6 |
| API | FastAPI backend | FastAPI + uvicorn | C1-5 |
| GRAPH | LangGraph state machine | LangGraph 1.1.x | C1-1 |
| RAG | 雙索引檢索 | ChromaDB + OpenAI 相容 embeddings | C1-2 |
| N8N | n8n REST client | httpx | C1-3 |
| VAL | Deterministic validator | pure Python | C1-4 |
| LLM | OpenAI 相容 adapter | langchain-openai | C1-1 |
| DATA | 節點資料 | xlsx + JSON | R2-2 |

### 3. 單輪對話資料流

```
1. User 於 Streamlit chat 送出 prompt
2. FE → POST /chat {message}
3. API 建立 AgentState，呼叫 LangGraph.invoke
   3.1 planner:    RAG.search_discovery(prompt, k=8) → StepPlan[]
   3.2 builder:    對每個 step 呼叫 RAG.get_detail(type) → BuiltNode[] + Connection[]
   3.3 assembler:  pure code → WorkflowDraft
   3.4 validator:  ValidationReport（pure code）
       - errors 為空 → 進 deployer
       - errors 非空 且 retry_count < 2 → retry_count+=1，回 builder
       - errors 非空 且 retry_count >= 2 → 中止、回傳錯誤
   3.5 deployer:   n8n POST /api/v1/workflows → workflow_url
4. API → FE 回 ChatResponse {workflow_url, workflow_json, retry_count, errors}
5. FE 顯示連結與可展開的 JSON
```

### 4. 技術決策（凍結，引自 plan）

| 項目 | 選擇 | 說明 |
|---|---|---|
| n8n | `n8nio/n8n:1.123.31` | docker-compose 只起 n8n；`.n8n_data` 卷保 API key |
| 推論端點 | OpenAI 相容（預設本機 vllm） | `OPENAI_BASE_URL=http://localhost:8000/v1`（容器內可用 `http://host.docker.internal:8000/v1`） |
| 生成 LLM | `$LLM_MODEL` | 預設 `Qwen/Qwen2.5-7B-Instruct`；需與推論伺服器 `--served-model-name` 對齊 |
| Embedding | `$EMBED_MODEL` | 預設 `BAAI/bge-m3`；同樣需與伺服器對齊 |
| Backend | Python 3.11 + FastAPI + LangGraph 1.1.x | `langchain-openai` 0.3.x |
| 結構化輸出 | `ChatOpenAI(...).with_structured_output(Model, method="json_schema")` | 走 OpenAI Structured Outputs / vllm guided decoding；不使用 `function_calling`（vllm 部分模型不支援） |
| Vector store | ChromaDB（persistent local） | 沿用 archive |
| 前端 | Streamlit | 呼叫 backend `/chat` |

### 5. MVP 範圍邊界

**In**：
- 單輪「描述 → 完整 workflow → 部署 → 回連結」。
- 離散 retry：validator 失敗時把 `ValidationReport.errors` 回饋 builder，最多 2 次。
- 官方節點（n8n-nodes-base.*）單節點支援。
- 無憑證配置（credentials 欄位一律空）。

**Out**：
- 憑證管理 UI。
- 實際執行 workflow 並回灌結果。
- 多使用者 / session 儲存 / PostgreSQL / Redis。
- per-edit tool-calling（官方 AI Workflow Builder 作法）。
- 多輪精修 workflow、差分編輯。
- AI 節點類（`@n8n/n8n-nodes-langchain.*`）之詳細 schema 支援（discovery 可出，但 detailed 不強求）。

### 6. 非功能目標

| 指標 | 目標 |
|---|---|
| 單輪 end-to-end 延遲 | p50 ≤ 45 秒（含 2 次 LLM 呼叫） |
| 單輪最壞情形（retry×2） | ≤ 120 秒 |
| 部署模式 | 本地單一使用者、無外網依賴（除 n8n credentials 的真實服務） |
| 觀察性 | stdout 結構化 log，能追 `plan → retrieved → workflow_json → deploy_id` |
| 資源占用 | backend 容器 ≤ 1.5GB RAM（不含推論伺服器本身的記憶體） |

## Errors

本層僅定義總體錯誤分類；具體 code 由各子 spec（特別是 C1-3、C1-5）說明。

| 類別 | 描述 | 暴露於 `/chat` |
|---|---|---|
| `PlanningError` | LLM 無法產出 valid StepPlan（schema fail） | 是（HTTP 500） |
| `BuildingError` | Builder 連續 2 次產出 validator 拒絕 | 是（HTTP 422，附 errors） |
| `DeployError` | n8n POST 失敗 | 是（HTTP 502，附上游訊息） |
| `UpstreamUnavailable` | OpenAI 相容推論端點 / n8n / Chroma 其中之一不可達 | 是（HTTP 503） |

## Acceptance Criteria

- [ ] D0-2、D0-3、C1-1～C1-6、R2-1～R2-3 全部以本 spec 為頂層依據，無互相矛盾。
- [ ] 技術決策表與 plan 文字完全一致。
- [ ] MVP 範圍邊界與 plan §MVP 範圍邊界段落一致。
- [ ] 元件清單每一項皆對應一份 L1 spec。
- [ ] ASCII 圖可被 Phase 1 agent 一眼看懂並對應到實作目錄結構（見 D0-3）。
