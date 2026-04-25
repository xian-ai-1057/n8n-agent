# C1-6：UI（Streamlit）

> **版本**: v2.0.2 ｜ **狀態**: Draft ｜ **前置**: C1-5 v2.0.3, C1-1 v2.0.4, C1-4 v1.1, C1-7, C1-9 v1.0.1

## Purpose

規範 Streamlit 前端畫面、訊息格式、錯誤顯示。v2.0 相較 v1.0 的變化：

1. 消費 **SSE streaming**（C1-5 v2.0 §2），每個 pipeline 階段即時更新 UI 狀態；使用者不再看到 180 秒黑箱。
2. 支援 **HITL plan review**：planner 後使用者可以編輯 plan 再按「確認並生成」。
3. 獨立顯示 **Validator 錯誤** 與 **Critic concerns**，並帶 `rule_class` 顏色標記與 `suggested_fix`。
4. 側邊欄呈現 planner 撿回來的 **templates**（讓使用者看到 RAG 貢獻、建立信任）。
5. Backend 若回 v1 JSON，UI 自動降級回 v1 行為。

## Inputs

- 使用者輸入（`st.chat_input`）
- Backend：`POST /chat`（SSE 或 JSON）、`POST /chat/{session_id}/confirm-plan`
- 環境變數：`BACKEND_URL`、`N8N_URL`、`HITL_DEFAULT`（bool; 預設 true）

## Outputs

- HTTP 呼叫 backend
- 即時顯示：階段進度、plan 審核表、workflow 連結、workflow JSON、validator / critic 錯誤區塊

## Contracts

### 1. UI 狀態機

由 SSE 事件驅動：

| UI state | 進入事件 | UI 呈現 |
|---|---|---|
| `idle` | （初始 / 上一輪 done） | chat input 可用 |
| `planning` | `stage_started{stage="planner"}` | `st.status("規劃中…")` |
| `plan_review` | `awaiting_plan_approval` | 可編輯 plan 表 + 「確認並生成」/「取消」按鈕（SSE 連線保持） |
| `building` | `stage_started{stage="build_step_loop"}` | per-step checklist，依 `step_built` 勾選 |
| `connecting` | `stage_started{stage="connections_linker"}` | |
| `validating` | `stage_started{stage="validator"}` | |
| `critiquing` | `stage_started{stage="critic"}` | |
| `retry` | `retry` | chip：「因 V-PARAM-002 重跑 builder (1/2)」 |
| `deploying` | `stage_started{stage="deployer"}` | |
| `done_ok` | `done{ok:true}` | workflow URL + JSON + elapsed；回 `idle` |
| `done_fail` | `done{ok:false}` / `error` | error_message + validator 與 critic 錯誤區塊 |

實作建議（非強制）：每個 state 對應一個 `st.empty()` placeholder；SSE 迴圈 inside `with st.chat_message("assistant"):` 中讀取事件即時更新。

### 2. 版面（v2）

```
┌──────────────────────────────────────────────────────────┐
│  Sidebar                                                 │
│    ├─ Title: n8n Workflow Builder                        │
│    ├─ Backend URL input                                  │
│    ├─ n8n UI URL display                                 │
│    ├─ Toggle: HITL 模式 (預設依 HITL_DEFAULT)            │
│    ├─ Toggle: 串流顯示 (預設 on — 等同 Accept SSE)       │
│    ├─ "檢查後端健康 /health" button                     │
│    ├─ "清空對話歷史" button                             │
│    ├─ "下載最後一次失敗的 workflow_json" button         │
│    └─ expander: 參考範例 (templates from plan_ready)    │
├──────────────────────────────────────────────────────────┤
│  Main                                                    │
│    ├─ [error banner]                                     │
│    ├─ Chat history                                       │
│    │    user:      "每小時抓 ... 存到 sheet"             │
│    │    assistant:                                       │
│    │      ├─ 階段進度列（planning→build→validate→…）    │
│    │      ├─ [若 plan_review] plan 編輯表 + 按鈕         │
│    │      ├─ [Open in n8n] 按鈕                          │
│    │      ├─ expander: Workflow JSON                     │
│    │      ├─ Validator 錯誤（有時）                     │
│    │      └─ Critic 結果（有時）                        │
│    └─ st.chat_input("描述你想要的 workflow")             │
└──────────────────────────────────────────────────────────┘
```

### 3. SSE client

Streamlit 不原生支援 SSE，但可用 `httpx.stream`：

```python
with httpx.Client(timeout=None) as c:
    with c.stream(
        "POST",
        f"{backend_url}/chat",
        headers={"Accept": "text/event-stream"},
        json={"message": prompt, "hitl": hitl_enabled},
    ) as r:
        for event, data in _iter_sse(r):    # helper parses event/data lines
            _handle_event(event, data)      # updates st.empty placeholders
```

註解行（`: ping`）**必須略過**，不觸發任何 state 更新。連線中斷（`httpx.RemoteProtocolError`、`ReadError`、瀏覽器關 tab）→ state → `idle`、保留已累積的 UI 內容、顯示黃色 banner「連線中斷，可重試」。

若 sidebar 的「串流顯示」關閉，以 `Accept: application/json` 直接走 v1/legacy one-shot 分支（見 §8）。

### 4. Plan review 表（HITL）

使用 `st.data_editor` 呈現 plan：

| 欄位 | 可編輯 | 規則 |
|---|---|---|
| `step_id` | ❌ | 保留 planner 分配 |
| `intent` | ✅ | 下拉 `StepIntent` enum |
| `description` | ✅ | textarea，≤ 200 字（對應 StepPlan.max_length） |
| `candidate_node_types` | ✅（multi-select） | 選項集合：當前 `discovery_hits` 的 type 清單 + 已有候選 |
| `reason` | ❌ | 唯讀 |

上方顯示從 `plan_ready` event 拿到的 templates：

```
建議參考範例（來自 RAG）：
 1. github-to-sheet · 每小時抓 GitHub API 並寫入 Sheet
 2. slack-alert · 收到 webhook 觸發 Slack 通知
 3. ...
```

Client-side 驗證（禁用「確認並生成」按鈕的條件）：
- 步驟數 < 1
- 不恰好含 1 個 `intent="trigger"`
- 任一步 `candidate_node_types` 為空
- 任一步 `description` 為空

按鈕：
- **確認並生成** → `POST /chat/{session_id}/confirm-plan` body `{"approved": true, "edited_plan": <from_data_editor>}`；之後回到 SSE 事件迴圈。
- **取消** → `POST .../confirm-plan` body `{"approved": false}`；state → `done_fail`，顯示「已取消」。

### 5. Validator 錯誤顯示

每筆 `ValidationIssue` 渲染：

```
[V-PARAM-002 · parameter] [HTTP Request · nodes[1].parameters.url]
  node 'HTTP Request' param 'url' is not a valid URL: ''
  建議：填入完整 URL，如 https://api.github.com/zen
```

規則：
- Badge 顏色：`security`=紅、`catalog`=橙、`parameter`=黃、`structural`=藍、`topology`=灰。
- `suggested_fix` 若存在則在 message 下方斜體顯示。
- Severity `warning` 的以淡色顯示、不阻擋視覺焦點。

### 6. Critic 結果顯示

獨立標題「Critic（語意檢查）」下列出 `CriticConcern[]`：

```
[block · implausible_schedule] [Schedule Trigger · parameters.rule.interval]
  排程規則為空陣列，workflow 永遠不會被觸發。
  建議：新增 {field:"hours", hoursInterval:1}
```

若 `critic.pass=True, concerns=[]`：顯示綠色小字「Critic pass」。

### 7. 進度列

建議一個水平 pill 條：
```
[✓ plan] [→ build 2/4] [ ] validate [ ] critic [ ] deploy
```
依 `stage_started`、`step_built` 事件更新；retry 發生時在前綴加 `⟳(1)` badge。

### 8. Backwards compat（降級）

若：
- Sidebar 「串流顯示」關閉，或
- Backend 回應 `Content-Type: application/json`，

則走 v1 行為：單一 POST、等待回應、渲染 `ChatResponse`。此情境不出現 `plan_review` 流程。

### 9. Session state

```python
st.session_state.history: list[dict]
# assistant 項目 meta:
#   {"workflow_url","workflow_id","workflow_json",
#    "retry_count","errors","critic","plan","templates",
#    "session_id","mode"}   # mode ∈ {"stream","hitl-json","one-shot"}

st.session_state.active_session_id: str | None
st.session_state.ui_state: str                    # §1 的 state 名
st.session_state.last_failed_workflow_json: dict | None
```

「清空對話歷史」重置 history 與 active_session_id（僅前端）。

### 10. Sidebar 設定對應

| Sidebar 欄位 | 來源 | 說明 |
|---|---|---|
| Backend URL | `BACKEND_URL` 預設 | 使用者可覆寫 |
| n8n UI URL | `N8N_URL` | 純展示 |
| HITL 模式 toggle | `HITL_DEFAULT`（預設 true） | 送出時帶入 request body `hitl` |
| 串流顯示 toggle | true（預設） | 決定 Accept header |
| 檢查後端健康 | — | 呼叫 `/health` 並彩色顯示 openai/n8n/chroma 三格 + collections 計數 |
| 下載最後失敗 JSON | — | 從 `last_failed_workflow_json` 產 `.json` download |
| 參考範例 expander | 由 `plan_ready` event 填充 | 點開看 template 簡介；bug 回報時附上 |

## Errors

| 情境 | UI |
|---|---|
| SSE 連線中斷 / 瀏覽器關 tab | 黃 banner「連線中斷，可重試」；state → `idle`；保留已顯示內容 |
| 400 `message_too_long` | 紅 banner「輸入過長，請精簡（≤ 2000 字）」 |
| 404 `session_not_found`（confirm 時） | 紅 banner「session 已過期，請重新送出需求」；state → `idle` |
| 409 `not_awaiting_plan_approval` | toast「plan 已處理過，請重新送出」；state → `idle` |
| 429 rate limit | 橙 banner，顯示倒數 Retry-After 秒；送出按鈕 disable |
| 422 V-SEC blocked | 紅 banner + 指出 blocked type；建議改用其他節點 |
| 503 upstream unavailable | 紅 banner；`/health` 連結 |
| backend 完全不可達 | 紅 banner；保留歷史 |
| SSE JSON 解析失敗 | skip 當筆 event + 記 warning；不中斷整體流 |
| timeout > 200s | 紅 banner「生成超時，請縮短描述再試」 |

## Acceptance Criteria

- [ ] 串流開啟情境下，使用者看到五段進度（planning / build N/M / validating / critiquing / deploying）即時更新，無「卡住 180s」體驗。
- [ ] HITL 模式下，plan 表可編輯 description 與 candidate_node_types；「確認並生成」後 edited_plan 正確送達 backend。
- [ ] 「取消」後 UI 顯示「已取消」並回 `idle`。
- [ ] Validator error 與 Critic concern 視覺可區分（顏色 / 區塊）；`rule_class` badge 顏色正確。
- [ ] `suggested_fix` 若存在，位於 message 下方斜體顯示。
- [ ] Sidebar 「檢查後端健康」顯示 `discovery / detailed / templates` 三欄計數。
- [ ] Backend 回 v1 JSON（非 SSE）時 UI 正確降級、不顯示 plan review。
- [ ] 連續送出兩次需求，第一次的 SSE 連線中止不污染第二次的 UI 狀態。
- [ ] 瀏覽器關 tab 後 backend log 有 `stream_aborted`、UI 無崩潰（下次 reload 由 `history` 恢復）。
- [ ] `HITL 模式` toggle 關閉時直接走 one-shot / 串流而不出現 plan review。

## Traceability entries (current implementation)

### U-PLAN-01: Streamlit 顯示 assistant.plan

**Statement**: `frontend/app.py` 的 `_render_assistant` 在接收到 backend JSON 回應時,必須將 `response.plan` 存入 `st.session_state.messages[i]["plan"]`,並在「執行摘要」expander 中展開顯示每一步 `step_id` + `description`(對應 C1-6 §9 `assistant.plan` 欄位規格)。

**Affected files**:
- `frontend/app.py`(現已有 `msg.get("plan")` 讀取邏輯,L134–137;但 backend 從未填入 → 需在接收端從 response 取 `plan` 並寫入 message dict)
- `frontend/tests/` 或 smoke test(若有)

**行為規則**:
1. 當 POST `/chat` 回應含 `plan` key(A-RESP-01 保證),將 `plan` 原樣存入 message dict:
   `assistant_msg["plan"] = response_json.get("plan", [])`
2. `_render_assistant` 只要 `plan` 非空即展開 expander(現有分支已寫好,只需確認資料來源正確)。
3. 若 `plan` 欄位不存在(極舊 backend)→ 當作空 list,不 crash。

**Examples**:
- Pass: backend 回 `{"plan": [{"step_id":"step_1","description":"抓 API",...}]}` → UI expander 顯示 `- step_1 抓 API`。
- Pass: backend 回 `{"plan": []}` → expander 只顯示 retries / elapsed,不顯示 plan 區塊。
- Fail (pre-fix): backend 不回 `plan` 欄位 → `msg.get("plan")` 為 None,永遠走 falsy 分支。

**Test scenarios**:
1. mock backend response 含非空 plan → message dict 有 `plan` key,render 輸出含 step_id。
2. mock backend response `plan=[]` → render 不顯示 plan 區塊。
3. mock backend response 無 plan key(legacy)→ 不拋 KeyError。

**Security note**: N/A(plan 由 backend sanitize 過)。

---

### U-WEB-01: React web 前端由 backend 同源供應

**Statement**: `frontend/web/index.html` + `src/` 為獨立 React 前端;部署時由 backend 以 `/app` 路徑(C1-5:A-WEB-01)掛載,而非由獨立 web server 伺服。這消除瀏覽器端的 CORS 問題。

**Affected files**:
- `frontend/web/index.html`(不需修改;資源相對路徑即可解析為 `/app/...`)
- `frontend/web/src/conservative-app.jsx`(fetch 目標維持 `/chat`、`/health` 相對路徑,不寫絕對 origin)

**行為規則**:
1. `fetch("/chat", ...)` 與 `fetch("/health")` 必須使用**相對路徑**,令瀏覽器解析為 `http://localhost:8000/chat`(同源)。
2. 絕對路徑(如 `http://localhost:8000/chat`)不應寫死,避免 port 變動時破裂;若需配置化,使用 `<meta name="backend-base">` 或 runtime injected config。
3. 若 `frontend/web/` 要獨立用 Vite dev server 啟動(非本條目 scope),需 proxy `/chat` 與 `/health` 到 8000,或擴充 C1-5:A-WEB-01 的 allow_origins。

**Examples**:
- Pass: 瀏覽器打開 `http://localhost:8000/app/` → HTML 載入,其內 `fetch("/chat")` 解析為 `http://localhost:8000/chat`,同源,無 CORS preflight 需求。
- Fail: hardcode `fetch("http://localhost:8000/chat")` 且從 `file://` 或其他 origin 開啟 → CORS block。

**Test scenarios**:
1. `grep -n "fetch(" frontend/web/src/conservative-app.jsx` → 所有 fetch 目標為相對路徑。
2. E2E(手動 / Playwright,out-of-MVP):打開 `http://localhost:8000/app/`,送一次 `/chat` 請求成功。

**Security note**: 同 A-WEB-01(C1-8)。同源策略使 React 前端不依賴 CORS 白名單,降低配置錯誤面。

---

### CHAT-UI-01: Streamlit Session ID 維護(對應 C1-9 chat-first)

**Statement**: `frontend/app.py` 使用 `st.session_state.session_id` 追蹤目前 chat session id。規則:

1. **初始值**: `None`(尚未與 backend 建立 session)。
2. **回填**: 每次 `POST /chat` 收到 `ChatResponse` 後,把 `response.session_id` 寫入 `st.session_state.session_id`。
3. **送出**: 每次 `POST /chat` 在 request body 帶入當前 `session_id`(若有);第一次為 `None`,backend 會配發新 id。
4. **清空**: 「清空對話歷史」按鈕同步 reset `session_id = None`(等同開新 session)。
5. **Debug expander**: sidebar 提供「Debug: Session ID」expander(預設 collapsed),顯示 `st.session_state.session_id`,方便 QA / 客服除錯。

**Affected files**:
- `frontend/app.py`(初始化、回填、送出、清空、debug expander)

**Examples**:
- ✅ 第一次 prompt → 不帶 session_id;backend 配發 `abc12345...`;UI 把 id 存起來
- ✅ 第二次 prompt → 帶上次 id;backend 沿用同 session
- ✅ 點「清空對話歷史」→ history 清空 + session_id 重設 None;下次 prompt 等同新 session

**Test scenarios**(frontend 目前無自動化測試;以人工 / smoke 為主):
- 連續送 2 prompt 觀察 backend log 同一 session_id
- 「清空」後送新 prompt 觀察 backend log 為新 session_id
- Debug expander 顯示 session_id 正確

**Security note** — session_id pattern 由 backend C1-9:CHAT-SEC-01 守住;frontend 不另驗。

---

### CHAT-UI-02: 新 ChatResponse shape 渲染

**Statement**: `frontend/app.py` 依 `ChatResponse`(C1-9:CHAT-API-01)的擴充欄位渲染 assistant message:

1. **`assistant_text`** 為主文(取代 v1 的 workflow URL 直接置頂);無 `assistant_text` 時 fallback 至既有 `_render_assistant` 邏輯。
2. **`status` 路由顯示**:
   - `"chat"` / `"awaiting_plan_approval"` → 純文字
   - `"completed"` / `"deployed"` → 顯示 `workflow_url` 連結(若有)+ `Open in n8n` 按鈕 + `workflow_json` expander
   - `"error"` / `"rejected"` → `st.error(error_message)`(對話可繼續,不 abort)
3. **`tool_calls`**:非空時於 expander(預設 collapsed)顯示 `[{name, args_summary, result_status}, ...]`(觀察用)。
4. **HTTP 錯誤碼處理**:
   - **504**: 保留 `session_id`(可重試),顯示「逾時 (504)」訊息
   - **404**: 清除 `session_id`(session 已過期),提示重啟對話
   - **400**: `st.error(error_message)`(message_too_long 等)
   - **422 / 500**: 一般錯誤 banner

**Affected files**:
- `frontend/app.py`(`_render_assistant`、`_send_to_backend` 內 status code 路徑)

**Examples**:
- ✅ status="chat" + assistant_text="Hi!" → 純文字泡泡
- ✅ status="deployed" + assistant_text="已部署" + workflow_url 填值 → 文字 + URL + JSON expander
- ✅ status="error" + error_message="..." → `st.error(...)`,session_id 保留
- ✅ HTTP 504 → 「逾時」訊息,session_id 不清

**Test scenarios**(frontend smoke):
- mock backend 回 200 / status=deployed → workflow_url 顯示
- mock backend 回 504 → session_id 仍在
- mock backend 回 404 → session_id 變 None

**Security note** — assistant_text 已過 backend C1-8 redaction;frontend 直接 render(無 raw HTML 風險,Streamlit 預設 escape)。

---

### CHAT-UI-03: Plan 確認 UI

**Statement**: 當 backend 回 `status="awaiting_plan_approval"` 時,在最新 assistant message 後渲染 bordered plan card:

1. **Card 內容**: 列出每 step 的 `step_id`、`description`、`candidate_node_types`(逗號分隔)。
2. **三個按鈕**:
   - **確認執行** → 自動送 message `"確認執行"`(等同使用者打字確認)
   - **我要修改** → 顯示 `st.text_area`,使用者輸入修改建議,送出時當一般 chat message 送
   - **取消** → 自動送 message `"我不想建立這個 workflow"`
3. **State machine**: `st.session_state.awaiting_plan_approval` (bool)、`st.session_state.pending_plan` (list)、`st.session_state.pending_plan_action` (str | None)、`st.session_state.show_edit_input` (bool) 四個 flag 協作。
4. **Plan card 持續顯示直到 backend status 不再為 `awaiting_plan_approval`**(deployed / rejected / 新 chat 都會清掉)。

**Affected files**:
- `frontend/app.py`(plan card render + button handler + pending action loop)

**Examples**:
- ✅ status=awaiting_plan_approval → card 顯示;按「確認執行」→ 下一輪 backend 跑 confirm_plan(approved=true)
- ✅ 按「我要修改」→ 顯示 textarea,使用者輸入「step 2 改用 Slack」送出 → backend chat layer 由 LLM 翻譯成 confirm_plan(approved=True, edits=[...])
- ✅ 按「取消」→ 下一輪 backend 跑 confirm_plan(approved=false),status 變 rejected,card 消失

**Test scenarios**(smoke):
- mock 連續兩 turn 達 awaiting_plan_approval → card 顯示;按「確認」→ 第二次 POST body 含 message="確認執行"
- 按「取消」→ 卡片消失,next assistant 顯示 rejected 訊息

**Security note** — plan 內容已過 backend sanitize;按鈕送出的 hardcoded message 不接 user input(避免 injection);「我要修改」的 textarea 內容被當一般 chat message,backend 在 dispatcher 入口 sanitize。

---

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版：同步 /chat、workflow 連結、錯誤 banner |
| v2.0.0 | 2026-04-21 | 接入 SSE、HITL plan review 表、critic 結果獨立顯示、rule_class 顏色標記、suggested_fix 提示、templates sidebar；backend 為 v1 JSON 時自動降級 |
| v2.0.1 | 2026-04-22 | 新增 traceability 條目：U-PLAN-01（Streamlit 顯示 assistant.plan，搭配 C1-5:A-RESP-01）、U-WEB-01（React 前端由 backend 同源供應，搭配 C1-5:A-WEB-01）|
| v2.0.2 | 2026-04-25 | 為 C1-9 chat-first pipeline 補 frontend traceability:CHAT-UI-01(`session_id` 維護 + Debug expander)、CHAT-UI-02(新 ChatResponse shape 渲染 + status 路由 + HTTP error code 處理)、CHAT-UI-03(Plan 確認 card + 三個按鈕 + 修改 textarea)。對應 frontend/app.py 已落地的暫時 ID 正式化 |
