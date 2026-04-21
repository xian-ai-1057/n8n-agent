# 函式流程（Function Flow）

本文件追蹤一次 `/chat` 請求從前端輸入到 n8n 部署完成，每個階段呼叫的具體函式與檔案位置。

> 相關規格：[C1-1 Agent Graph](L1-components/C1-1_Agent_Graph.md)、[C1-5 API](L1-components/C1-5_API.md)。

## 全景時序

```
┌────────────┐  POST /chat  ┌───────────────┐            ┌──────────┐   ┌──────────────┐
│ Streamlit  │────────────▶│  FastAPI       │──invoke──▶│ LangGraph│──▶│ OpenAI-API   │
│ frontend   │◀──────────  │  app.main:app  │◀──state──│  agent   │◀──│ (vllm) / Chroma│
└────────────┘   JSON       └───────┬───────┘            └────┬─────┘   └──────────────┘
                                    │                          │
                                    │                          ▼
                                    │                    ┌──────────┐
                                    └──deploy─────────▶ │  n8n REST │
                                                         └──────────┘
```

## 1. 前端入口

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [frontend/app.py](../frontend/app.py) | `_submit_message()` | 讀取 `st.chat_input`，以 `httpx.Client` POST `{backend_url}/chat`（timeout 200s），結果寫入 `st.session_state.messages` |
| [frontend/app.py](../frontend/app.py) | `_render_assistant()` | 顯示 workflow URL、JSON、validator 錯誤、plan 摘要 |

## 2. FastAPI Handler

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [backend/app/main.py](../backend/app/main.py) | `create_app()` | 建立 FastAPI app、註冊 CORS、掛 `api.routes.router` |
| [backend/app/api/routes.py](../backend/app/api/routes.py) | `chat(req: ChatRequest)` | 於背景 thread 執行 `run_cli(req.message, deploy=True)`，180s 牆鐘逾時；回傳 `ChatResponse` |
| [backend/app/api/routes.py](../backend/app/api/routes.py) | `health()` | 逐一探測 OpenAI 相容端點 / n8n / Chroma，回傳 `{ok, openai, n8n, chroma}` |

## 3. LangGraph Pipeline

進入點：[`backend/app/agent/graph.py`](../backend/app/agent/graph.py) 的 `run_cli()` → `compiled.invoke(state)`。

Graph 結構：

```
START
  └─▶ plan_step ─▶ build_nodes ─▶ assemble_step ─▶ validate_step
                                                         │
                           ┌─────────────────────────────┤
                           ▼                             ▼
                    retry_count<2?                    ok == True
                     ▼        ▼                          ▼
                 fix_build  give_up ─▶ END           deploy_step ─▶ END
                     │
                     └──▶ assemble_step（回圈）
```

### 3.1 `plan_step`

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/planner.py](../backend/app/agent/planner.py) | `plan_step(state)` | 1) 呼叫 `Retriever.search_discovery(user_message, k=8)` 取得 `NodeCatalogEntry[]`；2) 補上 `if/switch` 等核心控制節點；3) 組 planner prompt；4) `invoke_with_timeout(llm, prompt)` 取得 `PlannerOutput`；5) 回傳 `{plan, discovery_hits, messages}` |
| [rag/retriever.py](../backend/app/rag/retriever.py) | `search_discovery(query, k)` | 呼叫 `OpenAIEmbedder.embed`→`ChromaStore.query("catalog_discovery", ...)` |
| [rag/embedder.py](../backend/app/rag/embedder.py) | `embed(text)` | 透過 `langchain_openai.OpenAIEmbeddings` 呼叫 `$OPENAI_BASE_URL/embeddings`（`$EMBED_MODEL`） |
| [agent/llm.py](../backend/app/agent/llm.py) | `get_llm(schema)` | 建 `ChatOpenAI`，以 `method="json_schema"` 做結構化輸出 |
| [agent/llm.py](../backend/app/agent/llm.py) | `invoke_with_timeout(llm, prompt, seconds)` | daemon thread + `Event`，超時即拋 `LLMTimeoutError`（避開推論伺服器長時間鎖死） |

### 3.2 `build_nodes`

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/builder.py](../backend/app/agent/builder.py) | `build_nodes(state)` | 1) 對每個 `StepPlan` 取第一順位 candidate，以 `Retriever.get_detail(type)` 抓 `NodeDefinition`；2) 組 builder（或 fix）prompt；3) LLM 產 `BuilderOutput`；4) 回傳 `{built_nodes, connections, candidates, messages}` |
| [rag/retriever.py](../backend/app/rag/retriever.py) | `get_detail(type)` | Chroma where-filter 精確查 `catalog_detailed` |

若為重試，`_make_fix_build_node()` 注入 `ValidationReport` 到 prompt，以「修正版」模板再跑一次 `build_nodes`。

### 3.3 `assemble_step`

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/assembler.py](../backend/app/agent/assembler.py) | `assemble_workflow(built_nodes, connections, user_message)` | 純 Python：指派 UUID、計算座標（`x = -100 + 220*i`，分支於 `y ± 200`）、決定 workflow name、填入 `settings.executionOrder = "v1"`，產出 `WorkflowDraft` |

### 3.4 `validate_step`

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/validator.py](../backend/app/agent/validator.py) | `WorkflowValidator.validate(draft)` | 依 `catalog_discovery.json` 逐條套用 19 條規則（V-TOP-*、V-NODE-*、V-CONN-*、V-TRIG-*），回傳 `ValidationReport{ok, errors, warnings}` |
| [agent/graph.py](../backend/app/agent/graph.py) | `_after_validate(state)` | 條件路由：`ok` → `deploy`；否則若 `retry_count<2` → `fix_build`；否則 → `give_up` |

### 3.5 `deploy_step`

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [agent/deployer.py](../backend/app/agent/deployer.py) | `deploy_step(state)` | 檢查 `N8N_API_KEY`，無則 dry-run；呼叫 `N8nClient.create_workflow(draft)` |
| [n8n/client.py](../backend/app/n8n/client.py) | `create_workflow(draft)` | 去除唯讀欄位、預設 `settings`、遷移 `continueOnFail` → `onError`，POST `{N8N_URL}/api/v1/workflows`；回傳 `WorkflowDeployResult{id, url}` |

## 4. 回應組裝

| 檔案 | 函式 | 動作 |
| --- | --- | --- |
| [api/routes.py](../backend/app/api/routes.py) | `_state_to_response(state)` | 把 `AgentState` 的 `draft / validation / workflow_id / workflow_url / retry_count / messages` 投影成 `ChatResponse` |

## 5. 重試與失敗路徑

- 驗證失敗 → `fix_build` 最多兩次；fix prompt 會在 `agent/prompts/` 下，明確帶入每一條 `ValidationIssue`。
- 仍失敗 → `give_up` 節點把 `error="validator failed after 2 retries"` 寫入 state 後終止。
- LLM 逾時 → `LLMTimeoutError` 向上冒泡，handler 回 500 / `{ok: false, error: ...}`。
- n8n 錯誤 → `N8nAuthError / N8nBadRequestError` 對應 `api/routes.py` 的狀態碼映射。

## 6. 重要函式速查

| 想找什麼 | 去哪裡 |
| --- | --- |
| Graph 如何組裝 | `agent/graph.py::build_graph` |
| AgentState 欄位 | `models/agent_state.py::AgentState` |
| LLM 結構化輸出 schema | `models/planning.py`、`models/workflow.py` |
| 19 條驗證規則 | `agent/validator.py` + `docs/L1-components/C1-4_Validator.md` |
| Prompt 文字 | `backend/app/agent/prompts/*.md` |
| Chroma collection 命名 | `rag/store.py::ChromaStore` |
| n8n 欄位相容處理 | `n8n/client.py::_sanitize_payload` |
