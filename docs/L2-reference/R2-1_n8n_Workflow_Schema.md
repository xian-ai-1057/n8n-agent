# R2-1：n8n Workflow JSON Schema Reference

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **目標 n8n 版本**: `1.123.31` / Public API v1.1.0

**Upstream source**：
- OpenAPI spec：`https://github.com/n8n-io/n8n/blob/master/packages/cli/src/public-api/v1/handlers/workflows/spec/`
- Workflow create schema：同目錄下 `schemas/workflow.yml`、`workflowRequest.yml`

遇欄位爭議，以上游源碼為準。

## Purpose

提供 Builder / Client / Validator 一致的 n8n workflow JSON 正確形狀參考，包含 MVP 最常踩雷的三件事：
1. 建立時必須省略的 read-only 欄位。
2. `connections` 用 source node **name** key（而非 id）。
3. 外層 `main` 陣列是 source output index，內層是 fan-out。

## Inputs

- n8n 1.123.31 public API。
- `N8N_API_KEY`（header `X-N8N-API-KEY`）。

## Outputs

- 兩個可直接 POST 的 workflow JSON 範例（minimal、branching）。
- 欄位清單（必填 / 選填 / read-only）。

## Contracts

### 1. POST `/api/v1/workflows` — request body

**Top-level 必填**：`name`, `nodes`, `connections`, `settings`。
`additionalProperties: false` — 多餘欄位會 400。

**Read-only；建立時必須省略**（帶入即 400 或被忽略）：
- `id`
- `active`
- `createdAt`
- `updatedAt`
- `isArchived`
- `versionId`
- `triggerCount`
- `shared`
- `activeVersion`

**Auth**：
```
X-N8N-API-KEY: <key>
Content-Type: application/json
```

API key 於 n8n UI `Settings → n8n API` 產生。

### 2. Node

必填：
- `id`：uuid v4 字串（n8n 前端也會接受純字串，但建議用 uuid）。
- `name`：workflow 內唯一；connections map 用它做 key。
- `type`：如 `n8n-nodes-base.httpRequest`、`n8n-nodes-base.scheduleTrigger`。
- `typeVersion`：數字（int 或 float）。必須是該 node 實際存在的版本。
- `position`：`[x, y]` 兩個數字（n8n UI 畫布座標）。
- `parameters`：物件，每個 node 不同（見 R2-2 詳細 schema）。

選填：
- `credentials`：物件（憑證綁定）；MVP 一律不設。
- `disabled`：bool。
- `onError`：字串 enum（替代已廢棄的 `continueOnFail`）：`"stopWorkflow"`（預設）、`"continueRegularOutput"`、`"continueErrorOutput"`。
- `executeOnce`：bool。
- `retryOnFail`：bool。
- `notes`：string。
- `notesInFlow`：bool。

**已廢棄（禁用）**：`continueOnFail`（已由 `onError` 取代；validator 出 V-NODE-008 警告）。

### 3. Connections（關鍵）

結構：

```json
{
  "<Source Node NAME>": {
    "main": [
      [
        { "node": "<Target Node NAME>", "type": "main", "index": 0 }
      ]
    ]
  }
}
```

**外層陣列**（`main` 下）— 對應 **source output index**。大多數節點只有一個 output（index 0），但 `If`、`Switch` 有多 output。
**內層陣列** — 對應 fan-out：同一個 output 可連多個 target。

AI 節點有其他 connection type：`ai_languageModel`、`ai_tool`、`ai_memory`、`ai_outputParser` 等；key 平行於 `main`。

**易錯點**：
- 外層 key 必須用 node `name`，**不是** `id`。
- 內層的 `"node"` 也是 target node `name`。
- source output index 不存在連線時可用空 list 占位，例如 `"main": [ [], [ {...} ] ]` 表示 output[0] 無連線、output[1] 有。

### 4. Settings

最小可用：
```json
{ "executionOrder": "v1" }
```

其他常見欄位：`saveManualExecutions`, `callerPolicy`, `errorWorkflow`。MVP 不強設。

### 5. 完整最小範例：Manual Trigger → Set

```json
{
  "name": "Hello n8n",
  "nodes": [
    {
      "id": "5b7c9c4e-6e2a-4a9d-8a1a-0b5b6a4a0a01",
      "name": "When clicking 'Execute Workflow'",
      "type": "n8n-nodes-base.manualTrigger",
      "typeVersion": 1,
      "position": [240, 300],
      "parameters": {}
    },
    {
      "id": "5b7c9c4e-6e2a-4a9d-8a1a-0b5b6a4a0a02",
      "name": "Set",
      "type": "n8n-nodes-base.set",
      "typeVersion": 3.4,
      "position": [460, 300],
      "parameters": {
        "assignments": {
          "assignments": [
            {
              "id": "a1",
              "name": "greeting",
              "value": "hello",
              "type": "string"
            }
          ]
        },
        "options": {}
      }
    }
  ],
  "connections": {
    "When clicking 'Execute Workflow'": {
      "main": [
        [
          { "node": "Set", "type": "main", "index": 0 }
        ]
      ]
    }
  },
  "settings": { "executionOrder": "v1" }
}
```

### 6. 分支範例：Webhook → If → {Slack, Gmail}

```json
{
  "name": "Urgent router",
  "nodes": [
    {
      "id": "n1", "name": "Webhook",
      "type": "n8n-nodes-base.webhook", "typeVersion": 2,
      "position": [240, 300],
      "parameters": { "httpMethod": "POST", "path": "urgent", "responseMode": "onReceived" }
    },
    {
      "id": "n2", "name": "If",
      "type": "n8n-nodes-base.if", "typeVersion": 2.2,
      "position": [460, 300],
      "parameters": {
        "conditions": {
          "options": { "caseSensitive": true, "typeValidation": "strict" },
          "conditions": [
            {
              "leftValue": "={{ $json.body.type }}",
              "rightValue": "urgent",
              "operator": { "type": "string", "operation": "equals" }
            }
          ],
          "combinator": "and"
        }
      }
    },
    {
      "id": "n3", "name": "Slack",
      "type": "n8n-nodes-base.slack", "typeVersion": 2.2,
      "position": [680, 200],
      "parameters": { "resource": "message", "operation": "post", "text": "urgent!" }
    },
    {
      "id": "n4", "name": "Gmail",
      "type": "n8n-nodes-base.gmail", "typeVersion": 2.1,
      "position": [680, 400],
      "parameters": { "resource": "message", "operation": "send", "subject": "FYI" }
    }
  ],
  "connections": {
    "Webhook": {
      "main": [
        [ { "node": "If", "type": "main", "index": 0 } ]
      ]
    },
    "If": {
      "main": [
        [ { "node": "Slack", "type": "main", "index": 0 } ],
        [ { "node": "Gmail", "type": "main", "index": 0 } ]
      ]
    }
  },
  "settings": { "executionOrder": "v1" }
}
```

注意 `If` 有兩個 output：index 0 = true 分支、index 1 = false 分支，對應外層陣列兩個元素。

### 7. 常見 Node type / typeVersion（參考；正式值由 R2-2 detailed JSON 決定）

| type | typeVersion | 備註 |
|---|---|---|
| `n8n-nodes-base.manualTrigger` | 1 | 手動觸發 |
| `n8n-nodes-base.scheduleTrigger` | 1.2 | 排程 |
| `n8n-nodes-base.webhook` | 2 | HTTP 觸發 |
| `n8n-nodes-base.httpRequest` | 4.2 | HTTP 呼叫（版本隨 1.123 再確認） |
| `n8n-nodes-base.if` | 2.2 | 條件分支 |
| `n8n-nodes-base.switch` | 3.2 | 多路分支 |
| `n8n-nodes-base.set` | 3.4 | 設定欄位 |
| `n8n-nodes-base.code` | 2 | JS/Python |
| `n8n-nodes-base.slack` | 2.2 | Slack |
| `n8n-nodes-base.gmail` | 2.1 | Gmail |
| `n8n-nodes-base.googleSheets` | 4.5 | Google Sheets |

上表為起步參考；Phase 1-B 在寫 detailed JSON 時請以 n8n 1.123.31 實際值校正（打開 UI 新增節點可看到當下 version）。

### 8. Response（建立成功）

n8n 回 201 + 完整 workflow（含 read-only 欄位）：

```json
{
  "id": "zB8q...",
  "name": "Hello n8n",
  "active": false,
  "nodes": [...],
  "connections": {...},
  "settings": {...},
  "createdAt": "...",
  "updatedAt": "..."
}
```

Client 取 `id` 後拼：
```
workflow_url = f"{N8N_URL}/workflow/{id}"
```

## Errors

| 上游 status | 常見原因 |
|---|---|
| 400 | top-level 多了 read-only 欄位；`parameters` 不合節點 schema；`typeVersion` 不存在 |
| 401 | `X-N8N-API-KEY` 缺或錯 |
| 404 | GET / activate 不存在的 id |
| 5xx | n8n 內部錯誤（罕見） |

## Acceptance Criteria

- [ ] §5 minimal JSON 可被 `POST /api/v1/workflows` 201 接受（Phase 1-C unit test）。
- [ ] §6 branching JSON 同上、且 n8n UI 畫布顯示兩分支。
- [ ] §1 read-only 清單與 plan §n8n Schema 段落一致。
- [ ] C1-3、C1-4、R2-2 對欄位命名與本文字一致。
