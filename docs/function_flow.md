# 函式流程（Function Flow，v2 pipeline）

本文件追蹤一次 `/chat` 請求從前端輸入到 n8n 部署完成，每個階段呼叫的具體函式與檔案位置。v2 相較 v1 的主要差異：per-step builder 迴圈、HITL plan confirm、critic、rule_class 分流、SSE。

> 相關規格：[C1-1 Agent Graph v2.0](L1-components/C1-1_Agent_Graph.md)、[C1-5 API v2.0](L1-components/C1-5_API.md)、[C1-7 Critic](L1-components/C1-7_Critic.md)。

## 全景時序

```
┌────────────┐  POST /chat (SSE / JSON)  ┌───────────────┐              ┌──────────────┐
│ Streamlit  │──────────────────────────▶│  FastAPI       │──invoke────▶│ LangGraph   │
│ frontend   │◀───── SSE events ─────────│  app.main:app  │◀──events────│  agent       │
└─────┬──────┘       JSON (one-shot)     └──────┬─────────┘              └──────┬───────┘
      │                                         │                                │
      │   /chat/{sid}/confirm-plan              │   ┌────────────────────────────┘
      └────────────────────────────────────────▶│   ▼
                                                │ OpenAI-compat endpoint / Chroma（3 collections）
                                                │
                                                ▼ deploy
                                         ┌──────────────┐
                                         │  n8n REST    │
                                         └──────────────┘

LangGraph 內部 (C1-1 v2.0)：

START ─▶ planner ─▶ await_plan_approval ─▶ build_step_loop ⟳ ─▶ connections_linker
                         │ (HITL 暫停)           ▲
                         │                       │ fix_build（同節點家族）
                         ▼                       │
                      give_up                    │
                                                 │
build_step_loop ─▶ connections_linker ─▶ assembler ─▶ validator
                                                           │
                                                 route_by_error_class
                                                           │
                                 ┌────────┬────────┬───────┴──────────┐
                                 ▼        ▼        ▼                  ▼
                              critic  fix_build  replan           give_up
                                 │        │    (planner)
                              (pass)  (回 build_step_loop)
                                 ▼
                               deploy ─▶ END
```

## 1. 前端入口

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [frontend/app.py](../frontend/app.py) | `_submit_message()` | 依 sidebar toggle 與 `HITL_DEFAULT` 組 body；若串流開啟以 `Accept: text/event-stream` 呼叫 `httpx.Client.stream("POST", /chat)`；否則走 JSON |
| frontend/app.py | `_iter_sse(response)` | 解析 `event:` / `data:` 行、忽略 `: ping` 註解；yield (event_type, json_payload) |
| frontend/app.py | `_handle_event(event, data)` | 依事件更新對應 `st.empty()` placeholder；`awaiting_plan_approval` 切到 `plan_review` 狀態 |
| frontend/app.py | `_render_plan_review(plan, templates)` | 以 `st.data_editor` 顯示可編輯 StepPlan 表 + 按鈕 |
| frontend/app.py | `_submit_plan_confirmation(session_id, approved, edited_plan)` | `POST /chat/{sid}/confirm-plan`；若原走 SSE 則 endpoint 回 202，事件續流 |
| frontend/app.py | `_render_assistant(msg)` | workflow URL / JSON / validator 錯誤（含 rule_class 色碼 + suggested_fix） / critic concerns |

## 2. FastAPI Handler

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [backend/app/main.py](../backend/app/main.py) | `create_app()` | 建立 FastAPI app、掛 CORS、注入 security middleware、register router |
| [backend/app/api/routes.py](../backend/app/api/routes.py) | `chat(req, request)` | (1) sanitize + secret mask (C1-8) → (2) rate-limit → (3) 模式判別（Accept / body.hitl） → (4) 分派給三條路徑之一 |
| backend/app/api/streaming.py | `sse_generator(graph, state, session_id)` | `async for ev in graph.astream_events(...)` 轉為 SSE 事件；每 20s 發 `: ping`；client 中斷時記 `stream_aborted` |
| backend/app/api/routes.py | `confirm_plan(session_id, req)` | 從 session store 抓 state、驗 stage、merge edited_plan、`graph.invoke({"__resume__": ...})`；SSE 模式回 202、hitl-json 模式同步跑到 done |
| backend/app/api/session.py | `SessionStore` | `MemorySaver` wrapper；30min TTL；1000 concurrent 上限；背景 task reap |
| backend/app/api/routes.py | `health()` | 探測 openai / n8n / Chroma 三 collection；任一空或 down → `ok=false` |

## 3. LangGraph Pipeline

進入點：[`backend/app/agent/graph.py`](../backend/app/agent/graph.py) 的 `build_graph(retriever, deploy_enabled, hitl_enabled)` → `compile(checkpointer=MemorySaver(), interrupt_before=["build_step_loop"] if hitl_enabled else [])`。

### 3.1 planner

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/planner.py](../backend/app/agent/planner.py) | `plan_step(state, retriever)` | 1) `QUERY_REWRITE_ENABLED=1` 時 `rewrite_query(user_message)` 產多 query；2) 對每個 query 跑 `retriever.search_discovery(k=8)`（內部 rerank） → merge；3) `retriever.filter_by_coverage(hits)` 把 `has_detail=True` 前移；4) `retriever.search_templates_by_query(user_message, k=3)` 取相似 workflow；5) 組 planner prompt（R2-3 v1.1，內含 templates 摘要）；6) `ChatOpenAI(PLANNER_MODEL, temperature=PLANNER_TEMPERATURE).with_structured_output(PlannerOutput).invoke(prompt)` |
| [rag/retriever.py](../backend/app/rag/retriever.py) | `search_discovery` / `search_templates_by_query` / `filter_by_coverage` | C1-2 v1.1 §3 |
| [rag/embedder.py](../backend/app/rag/embedder.py) | `embed(text)` | 依 `EMBED_PROMPT_PROFILE`（env / auto 推斷）挑選 query wrapper |

Replan 路徑（`fix_target="planner"`）：prompt 增一個「避免再使用 {bad_types}」區塊。

### 3.2 await_plan_approval

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/graph.py](../backend/app/agent/graph.py) | `_await_plan_approval(state, hitl_enabled)` | `hitl_enabled=False` → `plan_approved=True` 直接通過；`True` → graph `interrupt_before` 在這節點後暫停，控制流回 `chat` handler；待 `confirm-plan` resume |

### 3.3 build_step_loop（self-loop）

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/builder.py](../backend/app/agent/builder.py) | `build_step_one(state, retriever)` | 1) 取 `step = state.plan[state.current_step_idx]`；2) `retriever.get_detail(step.candidate_node_types[0])` → 無則走 4-tier fallback（C1-2 v1.1 §5）；3) `retriever.search_templates_by_types([chosen_type], k=2)` 取範例 parameters；4) 組 prompt（首跑 `builder_step.md` / fix `fix_step.md`）；5) LLM 產 `BuilderStepOutput.node`；6) append 到 `built_nodes`；`current_step_idx += 1` |
| agent/graph.py | `_build_loop_cond(state)` | `state.current_step_idx < len(state.plan)` ? 回自己 : 進 `connections_linker` |

Fix 模式：`fix_target="builder"` 且 `validation.errors` 只指向單一 `node_name` 時，`current_step_idx` 回到該步 index；多步牽連則 `current_step_idx=0` 從頭重跑但保留未受影響的 built_nodes。

### 3.4 connections_linker

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/connections_linker.py](../backend/app/agent/connections_linker.py) | `connections_step(state)` | 若 `should_skip_llm_linker(plan)`（無 condition intent）→ 純 Python 串 linear；否則獨立一次 LLM call（`ConnectionsOutput`） |

### 3.5 assembler

不變（C1-1 v2.0 §2.5）：UUID、position layout、derive name、`settings.executionOrder="v1"`。

### 3.6 validator

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/validator.py](../backend/app/agent/validator.py) | `WorkflowValidator.validate(draft, node_definitions, blocklist, warnlist)` | 跑 V-TOP / V-NODE / V-CONN / V-TRIG / **V-PARAM / V-SEC**；每 issue 帶 `rule_class` + `suggested_fix` |

### 3.7 route_by_error_class（條件邊）

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| agent/graph.py | `route_by_error_class(state)` | 見 C1-1 v2.0 §2.7；寫 `fix_target` |

決策表：

| 條件 | 下一站 | retry_count |
|---|---|---|
| ok=True | critic | 不動 |
| class 含 security | give_up | 不動 |
| class 含 catalog 且 retry<2 | replan（planner） | +1 |
| structural/parameter/topology 且 retry<2 | fix_build | +1 |
| retry 已達 MAX | give_up | — |

### 3.8 critic（C1-7）

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/critic.py](../backend/app/agent/critic.py) | `critic_step(state)` | `ChatOpenAI(CRITIC_MODEL, temperature=0).with_structured_output(CriticReport).invoke(prompt)`；逾時 / 崩潰 → fail-open（pass=True + messages warning） |

### 3.9 fix_build

與 `build_step_loop` 同一節點家族；`fix_target="builder"` 時走 fix prompt，注入 `validation.errors` 與 `critic.concerns`（含 `suggested_fix`）。

### 3.10 deployer

不變：`N8nClient.create_workflow(draft)` → 回 `{workflow_id, workflow_url}`。無 `N8N_API_KEY` 走 dry-run。

### 3.11 give_up

寫 `error` 格式：`"{cause} after {retry_count} retries; {n} validator errors, {m} critic concerns"`。cause ∈ `validator failed` / `critic failed` / `security blocked` / `plan rejected`。

## 4. 回應組裝

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| api/streaming.py | `sse_event(event_type, data)` | 將 LangGraph `astream_events` 節點 callback 轉成 SSE；`done` 送完整 `ChatResponse` |
| api/routes.py | `_state_to_response(state)` | one-shot / hitl-json 模式下最終轉 ChatResponse；v2 新增 `critic_concerns` 欄位 |

## 5. 重試與失敗路徑

- **catalog error**（V-NODE-004）→ replan；planner prompt 會告知要排除的 type。
- **structural / parameter / topology error** → fix_build；只重跑 errors 指涉的 step。
- **security error**（V-SEC-001）→ give_up（不 retry）。
- **critic block** → fix_build；concerns + suggested_fix 進 prompt。
- `MAX_RETRIES = 2`，replan + fix 共用 budget。
- LLM 逾時 → 該節點 fail；進入下一輪 validator 時會被認出（缺 node）→ route 決定是否 retry。
- n8n 錯誤（auth / 400 / 503）→ 不 retry；直接回 502/503。
- HITL 使用者未在 30 min 內 confirm → session GC；下次 `confirm-plan` 回 404。

## 6. 重要函式速查

| 想找什麼 | 去哪裡 |
| --- | --- |
| Graph 組裝與 checkpointer | `agent/graph.py::build_graph` |
| AgentState 欄位 | `models/agent_state.py::AgentState`（D0-2 v1.1） |
| SSE 事件生成 | `api/streaming.py::sse_generator` |
| Session 管理 / TTL | `api/session.py::SessionStore` |
| Plan confirm 端點 | `api/routes.py::confirm_plan` |
| LLM 結構化輸出 schema | `models/planning.py`、`models/workflow.py`、`models/critic.py` |
| Validator 規則（結構 + 語意 + security） | `agent/validator.py` + `docs/L1-components/C1-4_Validator.md` v1.1 |
| Critic prompt / 節點 | `agent/critic.py` + `agent/prompts/critic.md`（R2-3 v1.1） |
| Query rewrite | `agent/query_rewrite.py`（新） |
| Reranker | `rag/reranker.py`（新；`RERANKER_MODEL` 空則 identity） |
| Templates 檢索 | `rag/retriever.py::search_templates_by_query` / `search_templates_by_types` |
| Security gate（sanitize / rate limit） | `api/security.py`（新；C1-8） |
| n8n 欄位相容處理 | `n8n/client.py::_sanitize_payload` |
