# C1-5：HTTP API（FastAPI）

> **版本**: v2.0.3 ｜ **狀態**: Draft ｜ **前置**: D0-2 v1.1, C1-1 v2.0, C1-3, C1-8

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

## Traceability entries (current implementation)

> 以下條目為「目前 v1 實作」的硬性合約。v2 的 SSE / HITL 尚未落地,這組 ID 用於追蹤現有 `/chat` + `/health` 行為與其必要修補。

### A-RESP-01: `ChatResponse` 必須包含 `plan` 欄位

**Statement**: 當 planner node 產生 plan 後,`/chat` 的 JSON 回應必須在 `plan` 欄位序列化回傳 `list[StepPlan]`,以供前端渲染「執行摘要」區塊 (C1-6 §9 `assistant.plan`)。

**Affected files**:
- `backend/app/models/api.py` (擴充 `ChatResponse`)
- `backend/app/api/routes.py` (於 `_state_to_response` 填入 `state.plan`)
- `backend/tests/test_api.py` 或新檔 `test_routes.py` (新增回應 schema 測試)

**Schema delta**:
```python
class ChatResponse(BaseModel):
    ok: bool
    workflow_url: str | None = None
    workflow_id: str | None = None
    workflow_json: dict[str, Any] | None = None
    retry_count: int = 0
    errors: list[ValidationIssue] = Field(default_factory=list)
    plan: list[StepPlan] = Field(default_factory=list)   # NEW — A-RESP-01
    error_message: str | None = None
```

**`_state_to_response` 行為**:
- `state.plan` 為 `list[StepPlan]`;直接 assign 到 `ChatResponse.plan`。
- 若 `state.plan` 為空 list,回傳空 list(非 `None`),維持欄位恆存。
- 欄位必存於成功與失敗兩種情境(失敗時前端仍可用 plan 協助除錯)。

**Examples**:
- Pass: `ChatResponse(ok=True, plan=[StepPlan(step_id="step_1", ...)], ...)` → JSON `"plan": [{...}]`
- Pass (empty): planner 失敗 → `plan=[]`(不可 `null`)
- Fail: `state_to_response` 漏填 → `msg.get("plan")` 在 `frontend/app.py` L126 永遠為 `None` → 「執行摘要」不展開 plan 區塊。

**Test scenarios**:
1. 成功跑完 pipeline → response 含非空 `plan` list,每項有 `step_id`、`description`、`intent`、`candidate_node_types`、`reason`。
2. planner 產生 0 步 → response 的 `plan == []`(且 key 存在)。
3. v1 client(不認 `plan` 欄位)→ Pydantic 不應強制 reject;目前 v1 client 只讀已知欄位,新增欄位向前相容。

**Security note**: N/A(plan 內容已經 sanitize,不含 secrets)。

---

### A-MSG-01: `ChatRequest.message` max_length 提升至 8000

**Statement**: React 前端 (`frontend/web/src/conservative-app.jsx`) 會在 multi-turn 場景下把歷輪使用者訊息串接成 `effectivePrompt`。為避免 5+ turn 的合理對話被 422 reject,`ChatRequest.message` 的上限由 2000 調整為 8000。

**Affected files**:
- `backend/app/models/api.py` (修改 `Field(max_length=...)`)
- `backend/tests/test_api.py`(邊界測試)

**Schema delta**:
```python
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)   # A-MSG-01
```

**C1-8 同步**: 本條目**取代** C1-5 §1 與 §8.1 中「長度 > 2000 → 400」的既有描述;security gate 的 `message` 長度上限同步提升到 8000。`edited_plan[*].description` 上限 200 不變。

**Examples**:
- Pass: 7999 字的 message → 200 / 202 / 422(視 pipeline 結果,但非長度拒絕)
- Pass: 空字串 → 422(由 `min_length=1`)
- Fail: 8001 字 → 422 (Pydantic 原生 validation)

**Test scenarios**:
1. message 長度 2001(原上限邊界)→ 不再被 reject。
2. message 長度 8000 → 通過 Pydantic。
3. message 長度 8001 → 422。
4. message 長度 0 → 422。

**Security note**: 長度上限提高會增加 LLM token 消耗,但不影響 prompt-injection 防線(C1-8 sanitizer 不依賴長度)。若 token 成本成為瓶頸,由 rate limit 處理。

---

### A-WEB-01: 內嵌 React web 前端與 CORS 政策

**Statement**: 後端在啟動時,若專案根目錄下存在 `frontend/web/` 目錄,必須以 FastAPI `StaticFiles` 掛載在 `/app` 路徑,使 React 前端透過**同源** `http://localhost:8000/app/` 存取 `/chat` 與 `/health`,繞過 CORS。Streamlit 前端 (`http://localhost:8501`) 的 CORS 設定保留不變。

**Affected files**:
- `backend/app/main.py`(新增 `StaticFiles` 掛載,條件式)
- `backend/app/config.py`(沿用既有 `_PROJECT_ROOT`,不需改 Settings)
- `backend/tests/test_main.py` 或 `test_static.py`(新增測試)

**Mount 規則**:
1. 計算 web 目錄:`web_dir = _PROJECT_ROOT / "frontend" / "web"`。
   - `_PROJECT_ROOT` 已由 `backend/app/config.py` 匯出,等同 `Path(__file__).resolve().parents[2]`(從 `backend/app/main.py` 算仍為專案根)。
2. 僅在 `web_dir.exists() and web_dir.is_dir()` 時才 mount;否則 log warning 並略過。
   - 原因:`StaticFiles(directory=...)` 在目錄缺席時會在 app 啟動期拋錯;對於 CI / minimal deploy 情境需要 graceful degrade。
3. Mount 語法:`app.mount("/app", StaticFiles(directory=str(web_dir), html=True), name="web")`。
   - `html=True` 讓 `GET /app/` 自動回傳 `index.html`(SPA 路由支援)。
4. **順序**:必須在 `app.include_router(router)` 之後 mount,確保 `/` 與 `/health` 等 API routes 不被 static 覆蓋(StaticFiles 以 `/app` 前綴掛載,實務上不衝突,但維持 router-first 順序避免未來誤配)。

**CORS 規則(不變)**:
- `allow_origins=["http://localhost:8501"]` 保留為 Streamlit 專用。
- 不新增 browser origin(如 `http://localhost:8000` 為同源,本就不需 CORS)。
- React 前端若在開發期用 Vite dev server 額外 port 啟動(out-of-scope),需自行 proxy 到 8000,或之後補條目擴增 allow_origins。

**Examples**:
- Pass: `curl http://localhost:8000/app/` → 200, `text/html`, 內容為 `index.html`。
- Pass: `curl http://localhost:8000/app/src/conservative-app.jsx` → 200, js 內容。
- Pass: `curl http://localhost:8000/health` 仍回 JSON(未被 static 覆蓋)。
- Fail (graceful): `frontend/web/` 不存在 → app 啟動成功,log 出現 "static frontend not mounted: <path> missing"。

**Test scenarios**:
1. `frontend/web/index.html` 存在 → `TestClient.get("/app/")` 回 200 且 content-type 含 `text/html`。
2. `frontend/web/` 缺席(monkeypatch `_PROJECT_ROOT` 指向 tmpdir 或 rename 目錄)→ app 啟動不拋例外,`/chat` 仍可用。
3. `/chat` 路由未被 `/app` mount 影響:POST `/chat` 正常。
4. CORS preflight `OPTIONS /chat` from `Origin: http://localhost:8501` → 通過;from `Origin: http://evil.test` → 無 CORS header(瀏覽器 reject,後端無損)。

**Security note**:
- **C1-8 相關**: StaticFiles 路徑必須限定於 `frontend/web/`,不得指向 repo 其他目錄(避免洩漏 `backend/` 原始碼)。`html=True` 不會啟用目錄列表,僅 fallback 到 `index.html`。
- 若未來加入使用者上傳功能,不得共用此 mount。
- CORS allow_origins 維持白名單;不用 `*`、不開啟 `allow_credentials`。

---

---

### C1-5:HITL-SHIP-01: `POST /chat/{session_id}/confirm-plan` endpoint 落地

**Statement**: C1-5 v2.0.0 §4 既有 `POST /chat/{session_id}/confirm-plan` 規格(request schema、四個 status 行為分支)為硬性合約,本條目把它從 spec-only 推到 impl。endpoint 在 `routes.py` 落地,行為遵循 §4 全部規則:404 on session 不存在、409 on stage mismatch、422 on edited_plan schema 錯、200 on 完成。同時 C1-9 chat layer 的 `confirm_plan_tool` 透過 in-process callable(C1-9:CHAT-API-02)而非 HTTP 重複進入此 endpoint;但 endpoint 本身仍須對外 expose 給 SSE / 外部 client。

實作上需注意:
1. 從 SessionStore(C1-9:CHAT-SESS-01)取對應 chat session,順便確認 `awaiting_plan_approval=True`(409 on false)。
2. 呼叫 `resume_graph_with_confirmation(session_id, approved, edited_plan)`(C1-1:HITL-SHIP-01 提供)。
3. 把 resume 後的最終 AgentState 用 `_state_to_response` 序列化(沿用既有 helper),fill `session_id` / `assistant_text="...plan confirmed/rejected..."` / `status` 三個 chat-layer 欄位(C1-9:CHAT-API-01)。
4. session 過期(graph checkpointer 已 GC)→ 404,session 仍在 store 內但 graph 找不到 thread → 也視為 404(對外行為一致)。

**Rationale**: HITL endpoint 在 v2.0.0 已寫好但沒 ship,且為 chat layer confirm_plan tool 的 fallback 入口(若未來改成跨 worker 部署,tool 必須走 HTTP 而非 in-process callable)。先把 endpoint 立起來、tool 走 in-process 是漸進路徑。

**Affected files**:
- `backend/app/api/routes.py`(新增 endpoint handler;新增 in-process `_do_confirm_plan(session_id, request) -> dict` 給 chat tool 注入)
- `backend/app/api/__init__.py`(可能要 export 新 helper)
- `backend/app/models/api.py`(`ConfirmPlanRequest` 已在 §4 spec,落地 Pydantic class)
- `backend/tests/test_routes_confirm_plan.py`(新增)

**Function signature**:
```python
# C1-5:HITL-SHIP-01
class ConfirmPlanRequest(BaseModel):
    approved: bool
    edited_plan: list[StepPlan] | None = None

@router.post("/chat/{session_id}/confirm-plan")
async def confirm_plan(session_id: str, body: ConfirmPlanRequest, request: Request) -> JSONResponse:
    """Resume HITL graph after plan review. Returns 200/404/409/422 per C1-5 §4."""

def _do_confirm_plan(session_id: str, body: ConfirmPlanRequest) -> dict:
    """Shared sync logic for endpoint + in-process chat tool callable (C1-9:CHAT-API-02)."""
```

**Examples**:
- ✅ approved=True, valid session → 200, ChatResponse with status="deployed", workflow_url filled
- ✅ approved=False → 200, status="rejected", error_message="plan_rejected"
- ❌ session 不存在 → 404 `{error:"session_not_found"}`
- ❌ session 存在但 graph 不在 await_plan_approval → 409 `{error:"not_awaiting_plan_approval", current_stage}`
- ❌ approved=True 但 edited_plan 缺 trigger / 0 步 → 400 `{error:"invalid_edited_plan"}`
- ❌ edited_plan schema 錯 → 422(FastAPI 自動)

**Test scenarios**:
- `test_confirm_plan_endpoint_404_on_unknown_session`
- `test_confirm_plan_endpoint_409_on_stage_mismatch`(mock graph 在 build 階段)
- `test_confirm_plan_endpoint_200_approved_no_edits`
- `test_confirm_plan_endpoint_200_approved_with_edited_plan`
- `test_confirm_plan_endpoint_200_rejected_sets_error_plan_rejected`
- `test_confirm_plan_endpoint_400_on_empty_edited_plan`
- `test_confirm_plan_endpoint_422_on_malformed_edited_plan`
- `test_do_confirm_plan_callable_returns_dict_for_chat_tool_use`

**Security note**: session_id 從 URL path 取,須過 C1-9:CHAT-SEC-01 pattern validation;不合格 → 404(避免揭露 internal id 結構)。Rate limit 30 req/min/IP(沿用 §8.4)。

**v1 reconcile note** (2026-04-25): 409 stage-mismatch 行為(spec §4 Errors)在 v1 acceptance 落地為:graph 層(C1-1:HITL-SHIP-01)未主動 raise `SessionStageMismatch`,endpoint 透過 `except Exception` fallback 收住(回 500,而非 spec 要求的 409)。同 session_id 重複 confirm 的場景目前由 LangGraph internal error 抑制,實務 friction 低(chat layer 內 single-flight)。**Follow-up**:加 graph 層 stage probe 並補對應 endpoint 分支,把 500 → 409。test `test_confirm_plan_endpoint_409_on_stage_mismatch` 已存在但目前驗證的是 fallback 路徑,需在 follow-up PR 中改驗 409。本注記不阻擋 v1 ship。

---

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版：同步 /chat + /health |
| v2.0.0 | 2026-04-21 | SSE streaming、HITL `confirm-plan` 端點、session storage 契約；整合 C1-8 security gate；/health 新增 templates 檢查；ChatResponse 加 critic_concerns 欄位（向前相容） |
| v2.0.1 | 2026-04-22 | 新增 traceability 條目：A-RESP-01（ChatResponse.plan）、A-MSG-01（message max_length=8000，取代 §1/§8.1 中 2000 的上限）、A-WEB-01（StaticFiles 掛載 `/app`）|
| v2.0.2 | 2026-04-25 | 新增 HITL shipping 條目:HITL-SHIP-01(`POST /chat/{sid}/confirm-plan` endpoint 落地;in-process `_do_confirm_plan` callable 供 C1-9 chat tool 注入)。對應 C1-9 chat layer 依賴 |
| v2.0.3 | 2026-04-25 | reconcile review:HITL-SHIP-01 補「v1 reconcile note」明列 409 stage-mismatch 由 endpoint `except Exception` fallback(回 500)收住、graph 層 stage probe 為 follow-up;test `test_confirm_plan_endpoint_409_on_stage_mismatch` 暫驗 fallback 路徑 |
