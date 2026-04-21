# C1-6：UI（Streamlit）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: C1-5

## Purpose

規範 Streamlit 前端畫面、訊息格式、錯誤顯示。Phase 3 實作 `frontend/app.py`。MVP 只做最小可用對話介面：輸入 → 後端 → 顯示 workflow URL + JSON。

## Inputs

- 使用者輸入（chat box）
- Backend：`POST {BACKEND_URL}/chat`
- 環境變數：`BACKEND_URL`、`N8N_URL`（只為顯示）

## Outputs

- HTTP POST to `/chat`
- 畫面上的：對話歷史、workflow 連結按鈕、展開 JSON、錯誤 banner

## Contracts

### 1. 版面（由上而下）

```
┌────────────────────────────────────────────────┐
│  Sidebar                                       │
│    ├─ Title: n8n Workflow Builder              │
│    ├─ Backend URL input  (default from env)    │
│    ├─ n8n URL display                          │
│    └─ "Clear history" button                   │
├────────────────────────────────────────────────┤
│  Main                                          │
│    ├─ [error banner if last request failed]    │
│    ├─ Chat history (st.chat_message loop)      │
│    │     user:        "每小時抓 ... 存到 sheet" │
│    │     assistant:                            │
│    │       ├─ status text (retry_count etc.)   │
│    │       ├─ [Open in n8n] link button        │
│    │       └─ expander: Workflow JSON          │
│    └─ st.chat_input("描述你想要的 workflow")   │
└────────────────────────────────────────────────┘
```

### 2. Session state

```python
st.session_state.history: list[dict]  # 每筆 {"role", "content", "meta"?}
# meta for assistant:
#   {"workflow_url", "workflow_id", "workflow_json", "retry_count", "errors"}
```

Clear history 重置 `history = []`。

### 3. 訊息格式（前後端）

前端呼叫：

```python
payload = {"message": user_text}
r = httpx.post(f"{backend_url}/chat", json=payload, timeout=180)
data = r.json()  # 對齊 ChatResponse（D0-2 §8）
```

錯誤顯示規則：
- `r.status_code >= 500` → 紅色 banner：`"後端錯誤：{error_message}"`。
- `r.status_code == 422` 且 `ok=false` 且 `errors` 非空 → 黃色 banner：`"Workflow 無法通過驗證（重試 {retry_count} 次）"`，下方以 expander 列 `errors[i].message`；仍把 `workflow_json` 放 expander 供使用者除錯。
- `ok=true` → 綠色成功訊息 + 連結按鈕。

### 4. 連結按鈕

```python
st.link_button("在 n8n 開啟 →", url=workflow_url, type="primary")
```

連結即 `ChatResponse.workflow_url`（已由後端拼好）。

### 5. Workflow JSON 展示

```python
with st.expander("Workflow JSON", expanded=False):
    st.code(json.dumps(workflow_json, ensure_ascii=False, indent=2), language="json")
```

### 6. 等待態

`st.chat_input` 送出後，用 `st.status("生成 workflow 中…", expanded=True)` 顯示階段 placeholder：`plan → build → validate → deploy`。MVP 不做 SSE，只在取得回應後一次 update（可之後升級）。

### 7. 環境變數

| Sidebar 欄位 | 來源 |
|---|---|
| Backend URL | `BACKEND_URL` 預設；使用者可在 sidebar 覆寫 |
| n8n URL | `N8N_URL`；純展示，方便使用者去 UI 補 credentials |

## Errors

| 前端情境 | 行為 |
|---|---|
| Backend 不可達（ConnectionError） | 紅色 banner："無法連到 backend {url}"；保留歷史 |
| timeout 180s | 紅色 banner："生成超時，請再試" |
| JSON 解析失敗 | banner 列出 raw text（前 500 字） |

## Acceptance Criteria

- [ ] 空白輸入不送出（`st.chat_input` 天然 empty-guard）。
- [ ] 成功情境顯示 `[Open in n8n]` 按鈕，點擊直接打開 n8n workflow 頁。
- [ ] Validator 失敗情境黃色 banner + errors 列表 + JSON 可展開。
- [ ] Backend 停機時前端顯示紅色 banner 不崩。
- [ ] sidebar 「Clear history」清空歷史。
- [ ] 介面語言：zh-Hant（按鈕/banner 文案）。
