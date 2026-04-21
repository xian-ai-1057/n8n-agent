# C1-3：n8n Client

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, R2-1

## Purpose

包裝 n8n Public API（v1.1.0，對應 image `1.123.31`）為 Python client。職責：把 `WorkflowDraft` 轉成符合 R2-1 的 JSON、呼叫 REST 端點、回傳 `WorkflowDeployResult`、把 HTTP 錯誤映射為 typed exceptions。

Phase 1-C 直接參考 archive `src/integrations/n8n_adapter.py`，但按以下合約重寫：
- 依 R2-1 §Read-only fields 去除欄位。
- 預設補 `settings={"executionOrder": "v1"}`。
- 使用 `onError` 取代 `continueOnFail`。

## Inputs

- `N8N_URL`、`N8N_API_KEY` 環境變數。
- `WorkflowDraft`（D0-2 §5）。

## Outputs

- `WorkflowDeployResult(id, url)`。
- 列表 / 取得 workflow 的 dict 或 Pydantic wrapper。

## Contracts

### 1. Base config

```python
BASE = f"{settings.n8n_url}/api/v1"
HEADERS = {"X-N8N-API-KEY": settings.n8n_api_key, "accept": "application/json"}
TIMEOUT = 20.0  # seconds
```

HTTP 客戶端：`httpx.Client(base_url=BASE, headers=HEADERS, timeout=TIMEOUT)`。

### 2. 端點

| 方法 | Path | 用途 | MVP 必用 |
|---|---|---|---|
| POST | `/workflows` | 建立 workflow | ✅ |
| GET | `/workflows/{id}` | 取單筆 | ✅ |
| GET | `/workflows` | 列表（含分頁） | ⬜ debug |
| POST | `/workflows/{id}/activate` | 啟用 | ⬜ 未在 MVP 啟用（使用者於 UI 自行決定） |
| POST | `/workflows/{id}/deactivate` | 停用 | ⬜ |
| DELETE | `/workflows/{id}` | 刪除 | ⬜ |

官方 OpenAPI 來源：`https://github.com/n8n-io/n8n/blob/master/packages/cli/src/public-api/v1/handlers/workflows/spec/`。遇欄位疑義以該源碼為準。

### 3. Draft → n8n JSON（序列化規則）

依 R2-1：

**Top-level 必出**：`name`、`nodes`、`connections`、`settings`。
**Top-level 禁出（read-only；後端會 400）**：`id`、`active`、`createdAt`、`updatedAt`、`isArchived`、`versionId`、`triggerCount`、`shared`、`activeVersion`。

```python
def draft_to_payload(draft: WorkflowDraft) -> dict:
    settings = dict(draft.settings or {})
    settings.setdefault("executionOrder", "v1")
    return {
        "name": draft.name,
        "nodes": [node_to_dict(n) for n in draft.nodes],
        "connections": connections_to_map(draft.connections),
        "settings": settings,
    }
```

`node_to_dict`：
- 必出：`id`, `name`, `type`, `typeVersion`, `position`, `parameters`。
- 選出（有值才出）：`credentials`, `disabled`, `onError`, `executeOnce`, `retryOnFail`, `notes`, `notesInFlow`。
- 禁出：`continueOnFail`（若 BuiltNode 誤帶，client 直接 drop 並 log WARN）。

`connections_to_map`（R2-1 §Connections）：

```python
def connections_to_map(conns: list[Connection]) -> dict:
    out: dict[str, dict[str, list[list[dict]]]] = {}
    # group by source_name -> type -> source_output_index
    for c in conns:
        src = out.setdefault(c.source_name, {}).setdefault(c.type.value, [])
        while len(src) <= c.source_output_index:
            src.append([])
        src[c.source_output_index].append({
            "node": c.target_name,
            "type": c.type.value,
            "index": c.target_input_index,
        })
    return out
```

### 4. 函式簽章

```python
from pydantic import BaseModel

class WorkflowDeployResult(BaseModel):
    id: str
    url: str  # "{N8N_URL}/workflow/{id}"


class N8nClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0): ...

    def create_workflow(self, draft: WorkflowDraft) -> WorkflowDeployResult: ...
    def get_workflow(self, workflow_id: str) -> dict: ...
    def list_workflows(self, limit: int = 50) -> list[dict]: ...
    def activate(self, workflow_id: str) -> None: ...
    def delete(self, workflow_id: str) -> None: ...
    def health(self) -> bool:
        """GET /workflows?limit=1 — 200 視為 ok。"""
```

### 5. 例外階層

```python
class N8nError(Exception): ...
class N8nAuthError(N8nError):   ...  # 401
class N8nNotFoundError(N8nError): ...  # 404
class N8nBadRequestError(N8nError):  # 400；包含上游 body.message
    def __init__(self, message: str, *, payload: dict | None = None): ...
class N8nServerError(N8nError):  ...  # 5xx
class N8nUnavailable(N8nError):  ...  # connection refused / timeout
```

HTTP status → 例外映射：

| status | 映射 |
|---|---|
| 200/201 | ok |
| 400 | `N8nBadRequestError`（附 response body） |
| 401/403 | `N8nAuthError` |
| 404 | `N8nNotFoundError` |
| 5xx | `N8nServerError` |
| 連線錯誤 / timeout | `N8nUnavailable` |

### 6. 重試策略

- 連線層錯誤（`N8nUnavailable`）：不自動 retry（MVP 簡化）。由 caller（deployer）記 error 並回 API。
- 400：不 retry（payload 錯；交給 graph retry 流程重生成）。

## Errors

依 §5 階層；最終在 deployer 轉成 `ChatResponse.error_message`（C1-5）。

## Acceptance Criteria

- [ ] 手寫一個 Manual Trigger → Set 最小 draft，`create_workflow` 後 n8n UI 能看見。
- [ ] 故意帶入 `id`/`active`/`createdAt` 欄位的 draft，POST 前 client 自動 strip，上游不回 400。
- [ ] 漏 `settings` 時 client 自動補 `{"executionOrder": "v1"}`。
- [ ] 帶入 `continueOnFail=True` 的 BuiltNode 時，client drop 並 log WARN、改寫 `onError="continueRegularOutput"` 視 Phase 1-B 判斷（若 archive 有現成 mapping 則沿用；否則一律 drop）。
- [ ] 401 → `N8nAuthError`、404 → `N8nNotFoundError`、400 → `N8nBadRequestError.payload` 含上游訊息。
- [ ] `health()` 在 API key 正確時回 True。
