# C1-5：HTTP API（FastAPI）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, C1-1, C1-3

## Purpose

定義 backend 對外 HTTP 合約：`/chat`（對話 → workflow 部署）、`/health`（upstream 健康檢查）。Phase 3 依此實作 `backend/app/main.py`。

## Inputs

- `ChatRequest`（D0-2 §8）
- 環境變數（D0-3 §2）

## Outputs

- `ChatResponse`（D0-2 §8）
- Health JSON

## Contracts

### 1. POST `/chat`

**Request**

```http
POST /chat
Content-Type: application/json

{"message": "每小時抓 https://api.github.com/zen 存到 Google Sheet"}
```

Pydantic：

```python
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
```

**Response（成功）**

```json
{
  "ok": true,
  "workflow_url": "http://localhost:5678/workflow/abcd1234",
  "workflow_id": "abcd1234",
  "workflow_json": { "name": "...", "nodes": [...], "connections": {...}, "settings": {...} },
  "retry_count": 0,
  "errors": []
}
```

**Response（validator 連續失敗）**

```json
{
  "ok": false,
  "workflow_url": null,
  "workflow_id": null,
  "workflow_json": { "...最後一次嘗試的 payload": "..." },
  "retry_count": 2,
  "errors": [
    {"rule_id": "V-CONN-002", "severity": "error", "message": "...", "node_name": "If", "path": "connections['If']"}
  ],
  "error_message": "validator failed after 2 retries"
}
```

**HTTP status 映射**

| 狀況 | status | body |
|---|---|---|
| 成功部署 | 200 | `ok=true` |
| 請求 schema 錯誤 | 422 | FastAPI 預設 |
| Validator 連續失敗 | 422 | `ok=false` + errors |
| n8n 400 / 409 | 502 | `ok=false`, `error_message` 含上游訊息 |
| n8n auth 失敗 | 502 | 同上 |
| OpenAI 相容端點 / n8n / Chroma 不可達 | 503 | `ok=false`, `error_message="upstream unavailable: <which>"` |
| 未預期例外 | 500 | `ok=false`, `error_message="internal error"` |

### 2. GET `/health`

**Response**

```json
{
  "ok": true,
  "openai": true,
  "n8n": true,
  "chroma": true,
  "checks": {
    "openai": {"ok": true, "latency_ms": 42},
    "n8n":    {"ok": true, "latency_ms": 88},
    "chroma": {"ok": true, "detail": "discovery=529,detailed=30"}
  }
}
```

任一 check 失敗則 top-level `ok=false`，status 仍為 200（便於監控端 probe 後自行判斷），`checks.<name>.ok=false` 並帶 `error`。

Check 實作：
- `openai`：`GET {OPENAI_BASE_URL}/models`（帶 `Authorization: Bearer $OPENAI_API_KEY`）；200 且 `$LLM_MODEL`、`$EMBED_MODEL` 皆在 `data[*].id` 中視為 up。
- `n8n`：呼叫 `N8nClient.health()`（C1-3 §4）。
- `chroma`：嘗試 `collection.count()` on 兩個 collection。

### 3. CORS / 安全

- MVP 單一使用者本機：`CORSMiddleware(allow_origins=["http://localhost:8501"], allow_methods=["*"], allow_headers=["*"])`。
- 無認證（本機）。
- 回應不要回填 `N8N_API_KEY` 等敏感欄位。

### 4. 路由模組結構

```
backend/app/
├── main.py              # FastAPI app + /health
├── routers/
│   └── chat.py          # POST /chat
├── config.py            # pydantic-settings
└── deps.py              # retriever / n8n client / graph 的 FastAPI Depends
```

### 5. 處理 pipeline（/chat）

```
1. 解析 ChatRequest
2. graph = get_graph()  # 單例
3. state = AgentState(user_message=req.message)
4. final = graph.invoke(state)  # 同步 or await（LangGraph 支援）
5. 組 ChatResponse 並決定 status
```

Phase 3 可選將 LangGraph 以 background task 跑；MVP 直接同步（Streamlit 會等待）。超時 180 秒（uvicorn `--timeout-keep-alive 180`）。

## Errors

| 例外 | status | body |
|---|---|---|
| `ValidationError`（請求入口） | 422 | FastAPI 預設 |
| `N8nAuthError` | 502 | `error_message="n8n auth failed"` |
| `N8nBadRequestError` | 502 | `error_message=f"n8n rejected payload: {e.message}"` |
| `N8nUnavailable` | 503 | |
| `EmbedderUnavailable`（OpenAI 相容 embedding 端點） | 503 | |
| `RagNotInitialized` | 503 | `error_message="run bootstrap_rag first"` |
| 其他 | 500 | 記 log、不露內部 traceback |

## Acceptance Criteria

- [ ] `POST /chat` 以 plan §Verification 情境 1 prompt 測試，回 200 且 `workflow_url` 可在瀏覽器打開。
- [ ] `GET /health` 三項皆 ok 時 top-level ok=true；故意停 n8n → `checks.n8n.ok=false` 且 top ok=false。
- [ ] Validator 連續失敗情境回 422 含 errors 陣列、`retry_count=2`。
- [ ] CORS 允許 Streamlit 同機 :8501。
- [ ] 未設 `N8N_API_KEY` → backend 啟動 fail-fast（pydantic-settings）。
