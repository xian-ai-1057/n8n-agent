# 資料流程（Data Flow，v2 pipeline）

本文件描述資料在系統中的「來源 → 轉換 → 去向」，以及跨元件傳遞時採用的型別與儲存位置。v2 主要變動：第三個 RAG collection（workflow_templates）、HITL 暫停 / resume 的 session 狀態、per-step 生成、critic 階段、新 AgentState 欄位。

> 相關規格：[D0-2 Data Model v1.1](L0-system/D0-2_Data_Model.md)、[C1-2 RAG v1.1](L1-components/C1-2_RAG.md)、[C1-5 API v2.0](L1-components/C1-5_API.md)、[C1-7 Critic](L1-components/C1-7_Critic.md)、[R2-1 n8n Workflow Schema](L2-reference/R2-1_n8n_Workflow_Schema.md)、[R2-4 Workflow Templates](L2-reference/R2-4_Workflow_Templates.md)。

## 0. 參與者與位置

| 角色 | 位置 | 狀態 |
| --- | --- | --- |
| 使用者 | Streamlit（`:8501`） | `st.session_state.history` + `active_session_id`；單 tab session |
| Backend | FastAPI（`:8000`） | stateless 請求層 + in-process `MemorySaver`（HITL session，30min TTL） |
| Chroma | `.chroma/`（本機檔案） | 持久化；**三** collection：`catalog_discovery` / `catalog_detailed` / `workflow_templates` |
| OpenAI 相容端點 | `$OPENAI_BASE_URL` | 生成（planner / builder / fix / critic / reranker 視 `*_MODEL` 而定）、embedding |
| n8n | Docker（`:5678`） | SQLite 保存 workflow |

## 1. 索引資料（離線，一次性）

`scripts/bootstrap_rag.py` 執行：

```
n8n_official_nodes_reference.xlsx
        │  (scripts/xlsx_to_catalog.py)
        ▼
data/nodes/catalog_discovery.json   ← 529 個節點摘要
data/nodes/definitions/*.json       ← ~30 個節點的完整參數（含手工 schema_hint）
data/templates/*.json + *.meta.yaml ← 策展過的 workflow 範例（R2-4）
        │
        │  (backend/app/rag/ingest_discovery.py)
        │    + has_detail flag：若 definitions/<slug>.json 存在則 True
        │  (backend/app/rag/ingest_detailed.py)
        │    + schema_hint 欄位驗 allowlist
        │  (backend/app/rag/ingest_templates.py)
        │    + 先走 WorkflowValidator V-TOP/V-NODE/V-CONN 核心檢查
        ▼
┌─────────────────────────────────────────────────────────┐
│ Chroma  (.chroma/)                                       │
│ ├── catalog_discovery                                    │
│ │     doc = 依 EMBED_PROMPT_PROFILE 包裝                 │
│ │     meta = {type, display_name, category, has_detail}  │
│ ├── catalog_detailed                                     │
│ │     meta = {type, raw: NodeDefinition JSON(含 hint)}   │
│ └── workflow_templates                                   │
│       meta = {template_id, name, node_types, raw JSON}   │
└─────────────────────────────────────────────────────────┘
```

`EMBED_PROMPT_PROFILE`（D0-3 v1.1）決定 embedding 時的 wrapper；預設 `auto` 依 `EMBED_MODEL` 推斷。

## 2. 線上請求流

### 2.1 請求進入

```
使用者輸入："每小時抓 GitHub API 存到 Google Sheet"
        ▼
sidebar 決定 mode：
  - 串流 on  → Accept: text/event-stream
  - HITL on  → body.hitl = true
        ▼
ChatRequest { message, hitl?, session_id?, deploy? }   (models/api.py)
        ▼
api/routes.py::chat
  1. sanitize_user_message → 長度 + injection 容器化 + secret mask (C1-8)
  2. rate-limit check
  3. mode = negotiate(Accept, body.hitl)
  4. session_id = body.session_id or uuid4()
```

### 2.2 Plan 階段

```
ChatRequest.message
   │
   │  (QUERY_REWRITE_ENABLED=1) rewrite_query → ["原句", "rewrite_1", "rewrite_2"]
   ▼
for q in queries:
   embedder.embed(q)           ← EMBED_PROMPT_PROFILE wrapper
   store.query(catalog_discovery, k=8*RERANKER_CANDIDATES_MULTIPLIER)
   │
merge by type（max-score 勝）
   │
rerank(hits, original_query, top_k=8)   ← RERANKER_MODEL 空則 identity
   │
filter_by_coverage(hits)   ← has_detail=True 前移
   │
同時 retriever.search_templates_by_query(original_query, k=3) → list[WorkflowTemplate]
   │
候選節點 + templates + user_message ─▶ planner prompt (R2-3 §1 v1.1)
                                        ▼
                          ChatOpenAI($PLANNER_MODEL, $PLANNER_TEMPERATURE)
                                        ▼
                       PlannerOutput { steps: StepPlan[] }
```

寫回 `AgentState.plan`、`discovery_hits`、`templates`。SSE 發 `plan_ready` 事件。

### 2.3 HITL 確認（僅在 hitl_enabled=True 時）

```
graph interrupt_before=["build_step_loop"]
   │
Backend：把 state 以 session_id 存入 MemorySaver（30min TTL）
SSE：發 awaiting_plan_approval 事件，連線保持
hitl-json：API 回 202 + plan + session_id
   │
使用者在 UI data_editor 改 StepPlan → 按確認
   ▼
POST /chat/{session_id}/confirm-plan { approved, edited_plan }
   │
Backend：
  - 驗 edited_plan schema
  - merge 回 state（覆蓋 plan，plan_approved=True）
  - graph.resume() 從 build_step_loop 起跑
```

`approved=false` → state 直接跳 give_up，`error="plan_rejected"`，SSE 發 error 後關連線。

### 2.4 Build 階段（per-step 迴圈）

```
for i in range(len(plan)):
    step = plan[i]
    # 4-tier 降級 (C1-2 v1.1 §5)
    defn = retriever.get_detail(step.candidate_node_types[0])
         or retriever.search_detail(step.description, k=3)[近似類別]
         or retriever.search_templates_by_types([type], k=1)[0].param 範例
         or None (empty shell)
    few_shot = retriever.search_templates_by_types([type], k=2)

    prompt = builder_step.md 或 fix_step.md（retry）
             ← 輸入 step、defn、few_shot、(retry 時) validation.errors + critic.concerns
    ChatOpenAI($BUILDER_MODEL / $FIX_MODEL).with_structured_output(BuilderStepOutput)
         → node: BuiltNode
    state.built_nodes.append(node)
    state.current_step_idx += 1
    SSE：step_built 事件
```

Fix 模式時，若 errors 僅指向單一 node_name，`current_step_idx` 直接回到該 index 只重跑該步；否則從頭跑但保留無關的 built_nodes。

### 2.5 Connections 階段

```
plan, built_nodes
   │
should_skip_llm_linker(plan)? （無 condition intent）
   ├─ 是 → 純 Python 串 linear → Connection[]
   └─ 否 → connections.md prompt + ChatOpenAI → ConnectionsOutput
   │
state.connections 更新
SSE：connections_built 事件
```

### 2.6 Assemble 階段（純程式、無 LLM）

不變（C1-1 v2.0 §2.5）：UUID、position layout、derive name、`settings.executionOrder="v1"`。

### 2.7 Validate 階段（純程式、無 LLM）

```
draft, node_definitions (from state.candidates), blocklist, warnlist
   ▼
WorkflowValidator.validate()
   ├─ V-TOP-*         (structural)
   ├─ V-NODE-001..009 (structural / catalog)
   ├─ V-CONN-*        (structural / topology)
   ├─ V-TRIG-*        (structural)
   ├─ V-PARAM-001..009 (parameter) ← 需 schema_hint
   └─ V-SEC-001..002   (security)
   ▼
ValidationReport {
  ok,
  errors: [ ValidationIssue(rule_id, rule_class, severity, message, node_name, path, suggested_fix) ],
  warnings: [...]
}
```

SSE 發 `validation` 事件（含 errors 摘要）。

### 2.8 路由（route_by_error_class）

```
validation.ok?
   │
   ├─ True ─────────▶ critic
   │
   ├─ class 含 security ─▶ give_up（不計 retry）
   │
   ├─ class 含 catalog   且 retry<2 ─▶ replan (回 planner，retry+1)
   │                                     └─▶（若 HITL 模式再經 await_plan_approval）
   │
   ├─ structural/parameter/topology 且 retry<2 ─▶ fix_build (build_step_loop) retry+1
   │
   └─ retry 用盡 ─▶ give_up
```

SSE 發 `retry` 事件含 reason。

### 2.9 Critic 階段（LLM）

```
draft + user_message + plan
   │
   ▼
critic prompt (agent/prompts/critic.md)
   │
ChatOpenAI($CRITIC_MODEL, temperature=0).with_structured_output(CriticReport)
   │    (逾時 / 崩潰 → fail-open: pass=True + messages warning)
   ▼
CriticReport { pass, concerns: [CriticConcern(rule, severity, message, suggested_fix, ...)] }
   │
state.critic 更新
   │
   ├─ pass=True                         ─▶ deploy
   ├─ pass=False  retry<2               ─▶ fix_build（concerns 進 prompt）
   └─ retry 用盡                         ─▶ give_up
```

SSE 發 `critic` 事件。

### 2.10 Deploy 階段

```
WorkflowDraft ──▶ N8nClient.create_workflow()
                    ├─ 去除唯讀欄位
                    ├─ settings 預設補 executionOrder
                    └─ 遷移 continueOnFail → onError
                    ▼
     POST {N8N_URL}/api/v1/workflows   (header: X-N8N-API-KEY)
                    ▼
             WorkflowDeployResult { id, url }
```

SSE 發 `deployed` 事件。n8n SQLite 永久保存 workflow。

### 2.11 回應組裝

SSE 模式：

```
done 事件 { ok, elapsed_ms, final: ChatResponse }
  ChatResponse = {
    ok, workflow_id, workflow_url, workflow_json,
    retry_count, errors: [ValidationIssue], critic_concerns: [CriticConcern],
    error_message
  }
```

one-shot / hitl-json 模式：直接回 `ChatResponse`（與 v1 shape 相容，新增 `critic_concerns`）。

前端把結果塞進 `st.session_state.history` 並渲染（C1-6 v2.0 §5 §6）。

## 3. AgentState（單一事實來源，v1.1）

`models/agent_state.py::AgentState` 在 graph 期間持續被各節點合併更新。

| 欄位 | 寫入者 | 內容 |
| --- | --- | --- |
| `user_message` | handler | 原始輸入（已 sanitize） |
| `session_id` | handler | uuid4 / body.session_id |
| `plan_approved` | await_plan_approval | HITL confirm 結果 |
| `discovery_hits` | planner | rerank + coverage-biased 結果 |
| `templates` | planner | `list[WorkflowTemplate]`（前三相似 workflow） |
| `plan` | planner / confirm-plan | `list[StepPlan]`（可能被 edited_plan 覆蓋） |
| `current_step_idx` | build_step_loop | per-step 進度 |
| `candidates` | build_step_loop | 每步使用的 `NodeDefinition` |
| `built_nodes` / `connections` | builder / linker | LLM / 純程式產出 |
| `draft` | assembler | `WorkflowDraft` |
| `validation` | validator | `ValidationReport`（含 rule_class + suggested_fix） |
| `fix_target` | route_by_error_class | `"planner"` / `"builder"` / `None` |
| `retry_count` | route_by_error_class | 0..2（planner + builder 共用） |
| `critic` | critic | `CriticReport` |
| `workflow_id` / `workflow_url` | deployer | n8n 回傳 |
| `messages` | 各節點 | 內部日誌（role 新增 `critic` / `router` / `hitl`） |
| `error` | give_up | 終止原因 |

## 4. 資料邊界與持久性

| 資料 | 位置 | 生命週期 |
| --- | --- | --- |
| 使用者對話歷史 | 前端 `st.session_state` | 瀏覽器 session（不持久化） |
| `AgentState` 中間值 | 後端 Python 物件 | 單次請求；HITL 模式下透過 MemorySaver 存活至 confirm 或 30min TTL |
| HITL session state | MemorySaver（in-process） | 30min TTL；1000 並存上限；backend 重啟即失效 |
| 節點目錄 | `data/nodes/*.json` + `.chroma/` | 版本控管；手動重建（bootstrap_rag） |
| 範例 workflow | `data/templates/*.json` + `.chroma/` | 版本控管；手動重建 |
| 產生的 workflow | n8n SQLite | 永久，直到使用者在 n8n UI 刪除 |
| LLM / embedding 權重 | 推論伺服器 | 由伺服器端管理 |

## 5. 外部呼叫與機密

| 呼叫 | 方向 | 認證 | 備註 |
| --- | --- | --- | --- |
| Frontend → Backend | HTTP JSON / SSE | 無（本機） | 200s timeout；rate limit 10/min/IP |
| Backend → OpenAI 相容端點 | HTTP | `Authorization: Bearer $OPENAI_API_KEY` | `/chat/completions`、`/embeddings`、`/rerank`（若啟用） |
| Backend → Chroma | 本地 client | 無 | `PersistentClient(path=.chroma)` |
| Backend → n8n | HTTP JSON | `X-N8N-API-KEY`（從 `.env`） | 無 key 則走 dry-run |

進 LLM 前的安全處理（C1-8）：
- `sanitize_user_message`：超長 400；injection pattern 命中時以 `<user_request>...</user_request>` 包裝。
- Secret pattern 遮罩：bearer / AWS / Slack token / 前綴 key|token|secret 的長字串 → `[REDACTED]`。
- `REDACT_TRACE=1` 時 `messages` 欄在回應中清空（server 端保留）。

若 `OPENAI_BASE_URL` 指向雲端，prompt 與 wrapped user_message 仍會離開本機。

## 6. 失敗時的資料狀態

| 失敗點 | `AgentState` | 回應 |
| --- | --- | --- |
| Plan LLM 逾時 | 只有 `user_message` + `session_id` | 500 + `error`；SSE 發 error |
| HITL 使用者 reject | plan 有值、`plan_approved=False` | `error_message="plan_rejected"`；`workflow_json=None` |
| HITL session 過期 | — | 404 `session_not_found` |
| Build per-step 產出不合 schema | `built_nodes` 部分空缺 | validator 抓到 → route → fix_build 或 replan |
| Validator V-SEC 命中 | `validation.errors` 含 rule_class="security" | 422 + `error_message`；不 retry；保留 `workflow_json` 供 debug |
| Validator retry 用盡 | `validation.errors` 有值 | 422；仍回 `workflow_json` + `errors` + （若有）`critic_concerns` |
| Critic retry 用盡 | `critic.pass=False` | 422；回 `workflow_json` + `critic_concerns` |
| Deploy 401 | `workflow_id=None` | 502；`error_message="n8n auth failed"` |
| SSE client 中途關連線 | graph 仍繼續跑到終局並存 session | server log `stream_aborted`；下次 GET 可透過 session_id 查到最終 state（未來擴充；MVP 僅 log） |

此設計讓失敗回應仍能保留中間產物（plan、draft、validation、critic），方便使用者在前端看出問題並調整描述或 plan。
