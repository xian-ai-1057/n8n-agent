# R2-2：Node Catalog Schema

> **版本**: v1.1.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, C1-2

## Purpose

定義 `data/nodes/catalog_discovery.json` 與 `data/nodes/definitions/<slug>.json` 的 JSON 結構。Phase 1-B 依此產出檔案、Phase 2-A RAG ingest 依此讀入。

## Inputs

- `n8n_official_nodes_reference.xlsx`（5 sheets / 529 rows）— 來源 for discovery。
- archive `99_Archive/n8n_Agent/data/nodes/definitions/*.json`（20 筆）— 沿用 20 個；新增 10 個核心節點補足 30。

## Outputs

- `catalog_discovery.json`：單一 JSON 檔，array of objects。
- `definitions/<slug>.json`：每節點一檔。檔名 slug 規則 = `type` 中 `.` 之後的部分（例：`httpRequest.json`）。

## Contracts

### 1. `catalog_discovery.json`（array items）

```json
[
  {
    "type": "n8n-nodes-base.httpRequest",
    "display_name": "HTTP Request",
    "category": "Core Nodes",
    "description": "Makes an HTTP request and returns the response data.",
    "default_type_version": 4.2
  },
  {
    "type": "n8n-nodes-base.slack",
    "display_name": "Slack",
    "category": "Communication",
    "description": "Send messages and manage channels in Slack.",
    "default_type_version": 2.2
  }
]
```

**欄位**

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `type` | string | ✅ | 完整 n8n node type（如 `n8n-nodes-base.slack`）；item 唯一 key |
| `display_name` | string | ✅ | UI 顯示名 |
| `category` | string | ✅ | xlsx 類別欄位；用於 embedding 文本 |
| `description` | string | ✅ | 中文或英文描述，用於 embedding |
| `default_type_version` | number \| null | ⬜ | 若已知最新版本則填；否則 null |
| `has_detail` | boolean | ⬜ | 預設 `false`；當 `data/nodes/definitions/{slug}.json` 存在時為 `true`。**不寫入 xlsx 來源**，由 ingest 階段補齊（見 §6 Ingest）。 |

對應 Pydantic：`NodeCatalogEntry`（D0-2 §4）。

### 2. `definitions/<slug>.json`

以 HTTP Request 為**完整範例**：

```json
{
  "type": "n8n-nodes-base.httpRequest",
  "display_name": "HTTP Request",
  "description": "Makes an HTTP request and returns the response data.",
  "category": "Core Nodes",
  "type_version": 4.2,
  "parameters": [
    {
      "name": "method",
      "display_name": "Method",
      "type": "options",
      "required": true,
      "default": "GET",
      "description": "HTTP method",
      "options": [
        {"name": "GET", "value": "GET"},
        {"name": "POST", "value": "POST"},
        {"name": "PUT", "value": "PUT"},
        {"name": "DELETE", "value": "DELETE"},
        {"name": "PATCH", "value": "PATCH"}
      ]
    },
    {
      "name": "url",
      "display_name": "URL",
      "type": "string",
      "required": true,
      "default": "",
      "description": "The URL to make the request to"
    },
    {
      "name": "authentication",
      "display_name": "Authentication",
      "type": "options",
      "required": false,
      "default": "none",
      "description": "How to authenticate",
      "options": [
        {"name": "None", "value": "none"},
        {"name": "Generic Credential Type", "value": "genericCredentialType"},
        {"name": "Predefined Credential Type", "value": "predefinedCredentialType"}
      ]
    },
    {
      "name": "sendQuery",
      "display_name": "Send Query Parameters",
      "type": "boolean",
      "required": false,
      "default": false
    },
    {
      "name": "queryParameters",
      "display_name": "Query Parameters",
      "type": "fixedCollection",
      "required": false,
      "default": {}
    },
    {
      "name": "sendBody",
      "display_name": "Send Body",
      "type": "boolean",
      "required": false,
      "default": false
    },
    {
      "name": "bodyContentType",
      "display_name": "Body Content Type",
      "type": "options",
      "required": false,
      "default": "json",
      "options": [
        {"name": "JSON", "value": "json"},
        {"name": "Form URL Encoded", "value": "form-urlencoded"},
        {"name": "Raw", "value": "raw"}
      ]
    },
    {
      "name": "jsonBody",
      "display_name": "JSON Body",
      "type": "json",
      "required": false,
      "default": ""
    }
  ],
  "credentials": [],
  "inputs": ["main"],
  "outputs": ["main"]
}
```

**欄位**

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `type` | string | ✅ | 同 discovery `type` |
| `display_name` | string | ✅ | |
| `description` | string | ✅ | 用於 embedding 文本 |
| `category` | string | ✅ | |
| `type_version` | number | ✅ | 此檔對應的 version；Builder 會原樣寫入 BuiltNode |
| `parameters` | array\<NodeParameter> | ✅（可空） | 見 §3 |
| `credentials` | array\<string> | ⬜ | 憑證類型名；MVP 可空 |
| `inputs` | array\<string> | ⬜ | 預設 `["main"]` |
| `outputs` | array\<string> | ⬜ | 預設 `["main"]`；If/Switch 多路則填多項 |

對應 Pydantic：`NodeDefinition`（D0-2 §4）。

### 3. NodeParameter 欄位

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `name` | string | ✅ | n8n UI 上的參數 key |
| `display_name` | string | ⬜ | |
| `type` | enum | ✅ | 見下 |
| `required` | boolean | ⬜ | 預設 false |
| `default` | any | ⬜ | |
| `description` | string | ⬜ | |
| `options` | array\<{name, value}> | ⬜ | 僅當 `type` ∈ {`options`, `multiOptions`} |
| `schema_hint` | string \| null | ⬜ | 預設 `null`；受控詞彙，見下表。用於告知 semantic validator 與 Builder 該參數在 runtime 的意義形狀，補足結構型別（`string`/`number`/…）無法表達的語意。 |

`type` 合法值：`string`, `number`, `boolean`, `options`, `multiOptions`, `collection`, `fixedCollection`, `json`, `color`, `dateTime`。

`schema_hint` 合法值（受控詞彙，超出 allowlist 的值 ingest 需 raise）：

| 值 | 意義 |
|---|---|
| `"url"` | HTTP(S) URL；C1-4 V-PARAM-* 規則會檢 scheme 與 host。 |
| `"cron"` | Cron 表達式。 |
| `"node_id"` | 指向另一個 node 的 id 或 name。 |
| `"expression"` | n8n expression（`={{ ... }}`），validator 可做括號配對檢查。 |
| `"credential_ref"` | 憑證引用名。 |
| `"email"` | Email 地址。 |
| `"datetime"` | ISO 8601 時間字串。 |
| `"secret"` | 機密字串；禁止明文落盤/入 log。 |
| `"resource_locator"` | n8n resourceLocator 物件（含 `mode`/`value`）。 |
| `null` | 無語意標註（預設）。 |

**範例**：`httpRequest` 節點的 `parameters.url` 應標註 `schema_hint="url"`；`scheduleTrigger` 的 cron 欄位應標 `"cron"`。

**標註策略**：manual-first。Phase 2-D 手動為現有 ~30 個 definitions 中具 runtime 意義的參數打標；自動推論不在 MVP 範圍。新增 definition 時須 by-hand 標註。

### 4. 必要 30 個 detailed 節點（MVP 目標）

從 archive 移植（20）＋新增（10），覆蓋 plan §Verification 三情境所需：

- Triggers：`manualTrigger`, `scheduleTrigger`, `webhook`, `emailReadImap`, `formTrigger`
- Flow control：`if`, `switch`, `merge`, `wait`, `code`
- Data：`set`, `httpRequest`
- Communication：`slack`, `gmail`, `telegram`
- Storage：`googleSheets`, `notion`, `airtable`
- Files：`readBinaryFile`, `writeBinaryFile`
- AI：`openAi`, `langchainAgent`, `lmChatOpenAi`
- Misc：`crypto`, `dateTime`, `splitInBatches`, `function`, `rssFeedRead`, `executeWorkflow`, `n8nTrainingCustomerDatastore`

實際 30 個名單由 Phase 1-B 最終敲定；缺口標到 `docs/L0-system/D0-3_Dev_Ops.md` 的 bootstrap 說明。

### 5. Discovery 與 Detailed 的關係

- `type` 必須一致。
- 若某 type 僅在 discovery（未有 detailed）：Builder 走降級（C1-2 §5）。
- 若某 type 僅在 detailed（discovery 未含）：ingest script 應補一筆最小 discovery entry，避免 Planner 看不到。

### 6. Ingest 責任

`scripts/bootstrap_rag.py` 在生成 / 載入 `catalog_discovery.json` 時必須：

- 掃描 `data/nodes/definitions/` 下的所有 `{slug}.json`，把檔案存在視為 detailed 可用信號。
- 對每筆 discovery entry，若其 `type` 對應的 definition 檔存在，就把該 entry 的 `has_detail` 設為 `true`；否則保持 `false`。
- 此欄位**不來自 xlsx 原始資料**，純由 ingest 階段依檔案系統狀態合成，因此每次 rebuild discovery 都需要重跑此步驟。
- `schema_hint` 僅存在於 definition 檔；ingest 需以 allowlist 驗證每個 parameter 的 `schema_hint`，不在表列的值應 raise（見 Errors）。

## Errors

| 情境 | 行為 |
|---|---|
| `type` 重複 | ingest raise |
| `parameters` 非 list | ingest raise |
| `type_version` 非 number | ingest raise |
| `options` 在非 options type 下出現 | ingest warning，欄位保留 |
| `schema_hint` 非 allowlist 值 | ingest raise |

## Acceptance Criteria

- [ ] `catalog_discovery.json` 至少 520 筆（容許 xlsx 解析捨去重複）。
- [ ] `definitions/` 含 ≥ 30 檔；每檔 schema pass（`NodeDefinition.model_validate(...)` 不 raise）。
- [ ] HTTP Request、Slack、Schedule Trigger、If、Webhook、Google Sheets 六個 detailed 必須存在（三情境 smoke test 依賴）。
- [ ] 所有 detailed 的 `type` 在 discovery 內都找得到同名 entry。
- [ ] Discovery 中 `has_detail=true` 的筆數等於 `data/nodes/definitions/` 下的檔案數。
- [ ] 至少 10 個 definition 檔對 runtime-meaningful 參數附上手動 `schema_hint` 標註。
- [ ] JSON schema 驗證對 `schema_hint` allowlist 之外的值會拒收（ingest raise）。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版（Phase 0） |
| v1.1.0 | 2026-04-21 | 新增 NodeCatalogEntry.has_detail、NodeParameter.schema_hint；支援 semantic validator 與 planner coverage-aware 選型 |
