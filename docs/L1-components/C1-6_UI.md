# C1-6：UI（Streamlit）

> **版本**: v2.0.0 ｜ **狀態**: Draft ｜ **前置**: C1-5 v2.0, C1-1 v2.0, C1-4 v1.1, C1-7

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

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版：同步 /chat、workflow 連結、錯誤 banner |
| v2.0.0 | 2026-04-21 | 接入 SSE、HITL plan review 表、critic 結果獨立顯示、rule_class 顏色標記、suggested_fix 提示、templates sidebar；backend 為 v1 JSON 時自動降級 |
