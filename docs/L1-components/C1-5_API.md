# C1-5：HTTP API（FastAPI）

> **版本**: v2.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-2 v1.1, C1-1 v2.0, C1-3, C1-8

## Purpose

定義 backend 對外 HTTP 合約：
- `/chat`（對話 → workflow 部署），支援 **SSE streaming** 與 **HITL plan confirm** 兩個新模式，並保留 v1 一次性 JSON 模式作為相容路徑。
- `/chat/{session_id}/confirm-plan`（HITL 用；使用者確認或編輯 plan 後才續跑 build）。
- `/health`（upstream 健康檢查，v2 新增 templates collection 子檢查）。

所有請求在進 LangGraph 前，必須通過 C1-8 定義的 security gate（長度上限、prompt-injection 容器化、secret masking、rate limit）。

## Inputs

- `ChatRequest`（D0-2 v1.1 §8）
- `ConfirmPlanRequest`（新，§4）
- 環境變數（D0-3 v1.1 §2）：`HITL_ENABLED`、rate-limit 相關、`REDACT_TRACE` 等

## Outputs

- 一次性 JSON 模式：`ChatResponse`（v1 相容）
- SSE 模式：`text/event-stream`，含 `stage_started` / `plan_ready` / `awaiting_plan_approval` / `step_built` / `connections_built` / `validation` / `critic` / `retry` / `deployed` / `done` / `error` 事件（§3）
- HITL 模式（JSON）：`awaiting_plan_approval` 階段立即回 `{session_id, plan, status}`；後續以 `confirm-plan` 完成

## Contracts

### 1. POST `/chat`

**Request body**

```json
{
  "message": "每小時抓 https://api.github.com/zen 存到 Google Sheet",
  "hitl": true,           // optional; default: env HITL_ENABLED（預設 1）
  "session_id": "...",    // optional; 由 client 指定時 server 驗格式後沿用，否則 server 產 uuid4
  "deploy": true          // optional; default: true 若 N8N_API_KEY 已設定
}
```

Pydantic：

```python
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    hitl: bool | None = None
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    deploy: bool | None = None
```

長度 > 2000 → 400（C1-8 §1）。

**模式判別（優先序）**

| 模式 | 觸發 | 回應 Content-Type |
|---|---|---|
| `stream` | request header `Accept: text/event-stream` | `text/event-stream` |
| `hitl-json` | body `hitl=true` 且 Accept 非 SSE | `application/json`（立即回暫停狀態） |
| `one-shot`（legacy） | 其他 | `application/json`（同 v1 行為） |

### 2. SSE 事件 taxonomy

每筆事件：

```
event: <type>
data: <JSON>

```

雙換行分隔。Client 以 `event: done` 或 `event: error` 作為終止信號。

| event | 觸發時機 | data 欄位 |
|---|---|---|
| `stage_started` | 任何節點 enter | `{stage, session_id, retry_count, current_step_idx?}` |
| `plan_ready` | planner 完成 | `{plan: StepPlan[], templates: {id,name,description}[], discovery_hits_count}` |
| `awaiting_plan_approval` | HITL 暫停（graph interrupt_before） | `{session_id, plan}`；SSE 連線保持開啟 |
| `step_built` | `build_step_loop` 每步完成 | `{step_id, node: {name,type,typeVersion}}` |
| `connections_built` | `connections_linker` 完成 | `{connections_count}` |
| `validation` | validator 完成 | `{ok, errors_count, errors: ValidationIssue[]}` |
| `critic` | critic 完成 | `{pass, concerns_count, concerns: CriticConcern[]}` |
| `retry` | `route_by_error_class` 決定重試 | `{from, to, reason, retry_count}` |
| `deployed` | n8n POST 成功 | `{workflow_id, workflow_url}` |
| `done` | 終止成功 | `{ok: true, elapsed_ms, final: ChatResponse}` |
| `error` | 終止失敗 | `{ok: false, error_message, errors?}` |

Keep-alive：server 每 20s 發 SSE 註解行 `: ping\n\n`（非事件，client 忽略），避免 proxy / 瀏覽器 idle timeout。

`errors` 與 `concerns` 在 SSE payload 中只放**摘要**（rule_id / rule_class / message / node_name / suggested_fix），不回傳完整 workflow JSON；最終 `done.final` 才含完整 `workflow_json`。

### 3. HITL-JSON 模式

當 `hitl=true` 且非 SSE：

**初次回應（暫停）**

```json
{
  "ok": false,
  "status": "awaiting_plan_approval",
  "session_id": "sess_abc123",
  "plan": [ {StepPlan...}, ... ],
  "templates": [ {id,name,description}, ... ],
  "workflow_json": null,
  "errors": []
}
```

HTTP status **202**（Accepted；尚未完成）。客戶端後續須呼叫 `POST /chat/{session_id}/confirm-plan`（§4）。

### 4. POST `/chat/{session_id}/confirm-plan`

**Request**

```json
{
  "approved": true,
  "edited_plan": [ ... StepPlan ... ]
}
```

Pydantic：

```python
class ConfirmPlanRequest(BaseModel):
    approved: bool
    edited_plan: list[StepPlan] | None = None
```

`edited_plan` 只在 `approved=true` 時生效；若帶入，覆蓋 state 的 plan。

**行為分支**

| 情境 | 回應 |
|---|---|
| session_id 不存在（過期 / 未曾建立） | 404 `{error:"session_not_found"}` |
| session 存在但 graph 不在 `await_plan_approval` | 409 `{error:"not_awaiting_plan_approval", current_stage: "..."}` |
| approved=false | 續跑至 `give_up`，回 `ChatResponse { ok:false, error_message:"plan_rejected" }` (HTTP 200) |
| approved=true，edited_plan schema 不合 | 422 + 詳細欄位錯誤 |
| approved=true 且 `edited_plan` 缺 trigger / 0 步 | 400 |
| approved=true，成功續跑 | 若初次呼叫是 SSE：本 endpoint 回 202，事件續流到原 SSE 連線；若 HITL-JSON：本 endpoint 同步跑到 `done` 並回最終 `ChatResponse` |

### 5. One-shot（legacy v1 相容）

當 `hitl=false` 且非 SSE：行為與 v1 C1-5 一致。Response schema 維持 v1：

```python
class ChatResponse(BaseModel):
    ok: bool
    workflow_url: str | None
    workflow_id: str | None
    workflow_json: dict | None
    retry_count: int
    errors: list[ValidationIssue]
    critic_concerns: list[CriticConcern] = []   # NEW v2；v1 client 會 extra="ignore"
    error_message: str | None = None
```

HTTP status 映射：

| 狀況 | status | body |
|---|---|---|
| 成功部署 | 200 | `ok=true` |
| Validator / Critic 連續失敗 | 422 | `ok=false` + errors / critic_concerns |
| Plan rejected（HITL approved=false） | 200 | `ok=false, error_message="plan_rejected"` |
| 請求 schema 錯 | 422 | FastAPI 預設 |
| session 不存在 | 404 | |
| n8n 400 / 409 | 502 | `error_message` 含上游訊息 |
| n8n auth | 502 | |
| upstream（OpenAI / n8n / Chroma）不可達 | 503 | |
| Rate limit | 429 | `{error:"rate_limited"}`，header `Retry-After: <s>` |
| V-SEC-001 命中 | 422 | `error_message="workflow contains blocked node type: {type}"` |
| Session 上限 1000 | 503 | `{error:"session_limit"}` |
| 未預期 | 500 | `error_message="internal error"` |

### 6. Session 儲存

- MVP：in-process `MemorySaver`（LangGraph checkpointer）；以 `session_id` 作 thread id。
- TTL：30 分鐘（最後一次更新時間起算）。每 60s 背景 task 掃過期 session。
- 容量上限：1000 個並存 session；超出回 503。
- 水平擴充（多 worker）明確 **out-of-MVP**；未來接 Redis checkpointer。

### 7. GET `/health`（v2 擴充）

**Response**

```json
{
  "ok": true,
  "openai": true,
  "n8n": true,
  "chroma": true,
  "collections": {
    "discovery": 529,
    "detailed": 30,
    "templates": 22
  },
  "checks": {
    "openai":    {"ok": true, "latency_ms": 42},
    "n8n":       {"ok": true, "latency_ms": 88},
    "chroma":    {"ok": true, "detail": "discovery=529,detailed=30,templates=22"}
  }
}
```

規則：
- `collections.discovery > 0 AND collections.templates > 0 AND collections.detailed > 0` → `chroma=true`。
- 任一為 0 → `chroma=false`，top-level `ok=false`。
- HTTP status 永遠 200；交由監控端 parse。
- Backwards compat：v1 client 看到的 `chroma: bool` 欄位仍存在且語意一致。

### 8. Security gate（C1-8 整合）

所有 `/chat*` endpoint 必經：

1. 長度上限（`message` ≤ 2000、`edited_plan[*].description` ≤ 200）。
2. `sanitize_user_message`：injection pattern 命中則包裝為 `<user_request>...</user_request>` 傳入 LLM；不 reject。
3. Secret-like 遮罩（bearer / AWS key / Slack token / 前綴 key|token|secret 的 ≥32 字串）。
4. Rate limit：`/chat` 10 req/min/IP；`/chat/*/confirm-plan` 30 req/min/IP；超出回 429。
5. 命中 V-SEC-001 node type → 在 validator 階段 give_up；422 回應。
6. CORS：允許 `http://localhost:8501`（Streamlit 同機）。
7. `REDACT_TRACE=1` 時，`messages` diagnostic 陣列在回應中被清空（僅 server-side 保留）。

### 9. 路由模組結構

```
backend/app/
├── main.py                # FastAPI app + router register
├── api/
│   ├── routes.py          # /health + /chat 入口（dispatch 三種模式）
│   ├── streaming.py       # SSE generator（yield bytes）
│   └── session.py         # session store + TTL reaper
├── agent/
│   └── graph.py           # 提供 build_graph / resume helpers
├── request_context.py     # rid / session_id contextvar
└── config.py
```

### 10. 處理 pipeline（/chat）

```
1. sanitize body (C1-8)
2. rate-limit check (C1-8)
3. session_id = body.session_id or uuid4
4. mode = negotiate(Accept, body.hitl)
5. graph = get_graph(hitl_enabled=(mode != "one-shot"))
6. if mode == "stream":
       return StreamingResponse(sse_generator(graph, state, session_id))
   elif mode == "hitl-json":
       run_until_interrupt; return 202 + plan
   else:
       run_to_end; return ChatResponse
```

Timeout：一次性 pipeline 預算 **180s** wall clock（LangGraph 內每 LLM call 另有自己的 timeout；C1-1）。`uvicorn --timeout-keep-alive 200`。

## Errors

| 例外 | status | body |
|---|---|---|
| `RequestValidationError`（body 解析） | 422 | FastAPI 預設 |
| `SessionNotFound` | 404 | `{error:"session_not_found"}` |
| `SessionStageMismatch` | 409 | `{error:"not_awaiting_plan_approval", current_stage}` |
| `SessionLimitExceeded` | 503 | `{error:"session_limit"}` |
| `RateLimitExceeded` | 429 | `{error:"rate_limited"}`, header `Retry-After` |
| `MessageTooLong` | 400 | `{error:"message_too_long"}` |
| `SecurityBlocked`（V-SEC-001） | 422 | `error_message="workflow contains blocked node type: {type}"` |
| `N8nAuthError` | 502 | `error_message="n8n auth failed"` |
| `N8nBadRequestError` | 502 | `error_message=f"n8n rejected payload: {e.detail}"` |
| `N8nUnavailable` | 503 | |
| `EmbedderUnavailable` | 503 | |
| `RagNotInitialized` | 503 | `error_message="run bootstrap_rag first"` |
| Client 中途關 SSE 連線 | — | log `stream_aborted`；背景 task 仍完成並將最終 state 存至 session（供後續 polling / debug） |
| 其他 | 500 | `error_message="internal error"`（不露 traceback） |

## Acceptance Criteria

- [ ] `curl -N -H "Accept: text/event-stream" -X POST .../chat -d '{"message":"..."}'` 回傳連續 SSE 事件並以 `event: done` 結束。
- [ ] HITL 模式下，SSE 在 `awaiting_plan_approval` 暫停（連線保持）；呼叫 `confirm-plan` 後事件繼續流出直到 `done`。
- [ ] One-shot 模式回應與 v1 行為一致（v1 client 可無縫升級）。
- [ ] `session_id` 超過 30min 後呼叫 `confirm-plan` 回 404；未過期但 graph 不在暫停點回 409。
- [ ] `approved=false` 的 confirm 使終結事件 `error_message="plan_rejected"`。
- [ ] Rate limit：同 IP 連送 11 次 `/chat` 的第 11 次回 429 且含 `Retry-After`。
- [ ] `/health` 含 `collections.templates`；templates 為空時 `chroma=false` 且 `ok=false`。
- [ ] Client 提早關 SSE 連線不使 backend crash；log 記 `stream_aborted`。
- [ ] V-SEC-001 命中時回 422 且 `error_message` 明確指出 blocked type。
- [ ] 同時 1001 個並存 session 時第 1001 個回 503。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版：同步 /chat + /health |
| v2.0.0 | 2026-04-21 | SSE streaming、HITL `confirm-plan` 端點、session storage 契約；整合 C1-8 security gate；/health 新增 templates 檢查；ChatResponse 加 critic_concerns 欄位（向前相容） |
