# C1-4：Validator

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, R2-1

## Purpose

Deterministic pure-Python validator。對 `WorkflowDraft`（或同等 dict）跑完整規則集，產 `ValidationReport`。失敗會驅動 C1-1 的 builder retry。Phase 1-C 移植 archive `src/agents/validator.py` 並對齊下表規則。

## Inputs

- `WorkflowDraft`（D0-2 §5）
- Node type registry：由 `NodeCatalogEntry` 彙整出的 `set[str]`（discovery 529 筆 type；Phase 1-B 生成）

## Outputs

- `ValidationReport(ok, errors, warnings)`（D0-2 §6）

## Contracts

### 1. 規則表

Severity：`E` = error（阻擋 deploy）、`W` = warning（不阻擋）。
`rule_id` 命名：`V-<範疇>-<序號>`。

| rule_id | sev | 說明 | 訊息模板 |
|---|---|---|---|
| V-TOP-001 | E | `name` 非空字串 | `"workflow name is required"` |
| V-TOP-002 | E | `nodes` 非空 list | `"workflow must contain at least one node"` |
| V-TOP-003 | E | `settings` 為 dict，且含 `executionOrder` | `"settings.executionOrder is required (use 'v1')"` |
| V-TOP-004 | W | `settings.executionOrder` ∈ {"v0","v1"} | `"unknown executionOrder: {value}"` |
| V-NODE-001 | E | 每個 node 必填：`id`, `name`, `type`, `typeVersion`, `position`, `parameters` | `"node[{idx}] missing required field: {field}"` |
| V-NODE-002 | E | `id` 為非空字串（uuid v4 建議但不強制） | `"node[{idx}].id must be a non-empty string"` |
| V-NODE-003 | E | node `name` 全 workflow 唯一 | `"duplicate node name: {name}"` |
| V-NODE-004 | E | `type` 存在於 registry | `"unknown node type: {type}"` |
| V-NODE-005 | E | `typeVersion` 為數字（int or float） | `"node '{name}' typeVersion must be a number, got {type_of_value}"` |
| V-NODE-006 | E | `position` 為 2 元素 list，皆數字 | `"node '{name}' position must be [x, y] numbers"` |
| V-NODE-007 | E | `parameters` 為 dict | `"node '{name}' parameters must be an object"` |
| V-NODE-008 | W | 不得出現已廢棄 `continueOnFail` | `"node '{name}' uses deprecated 'continueOnFail'; use 'onError' instead"` |
| V-NODE-009 | E | `id` 跨 node 唯一 | `"duplicate node id: {id}"` |
| V-CONN-001 | E | Connections key 為 source node 的 **name**（不是 id） | `"connection key '{key}' is not a known node name"` |
| V-CONN-002 | E | 每個連接 target `node` 為既存 node name | `"connection {source}->{target} targets unknown node"` |
| V-CONN-003 | E | `type` 屬於已知集合（`main`/`ai_*`） | `"connection {source}->{target} has invalid type '{type}'"` |
| V-CONN-004 | W | 節點無任何輸入連接（非 trigger） | `"node '{name}' has no incoming connection"` |
| V-CONN-005 | W | 節點無任何輸出連接（非末端） | `"node '{name}' has no outgoing connection"` |
| V-TRIG-001 | E | 至少一個 trigger node（type 以 `Trigger` 結尾或屬 manual/webhook/schedule 清單） | `"workflow must contain at least one trigger node"` |
| V-TRIG-002 | W | 多個 trigger 時提醒 | `"workflow has {n} trigger nodes"` |

Trigger 辨識（V-TRIG-001）：符合任一即視為 trigger：
- `type` 結尾為 `Trigger`（如 `scheduleTrigger`、`manualTrigger`）。
- `type` 在硬編 allowlist：`n8n-nodes-base.webhook`, `n8n-nodes-base.formTrigger`, `n8n-nodes-base.emailReadImap`。

### 2. 簽章

```python
# backend/app/agent/validator.py
def validate_workflow(
    draft: WorkflowDraft,
    *,
    known_types: set[str],
) -> ValidationReport: ...
```

純函式、無 I/O、無 LLM。

### 3. 實作提示

- 按 top → node → connection → trigger 依序跑；前階段 error 後仍繼續跑（盡量把錯誤一次全回，讓 Builder retry 有完整資訊）。
- `path` 欄位（ValidationIssue）範例：`nodes[2].position`、`connections['HTTP Request']`。
- 遇 `node.parameters` 為 `None` 而非 `{}` → V-NODE-007 error（切勿靜默轉換）。

### 4. 與 Retry 的耦合

Validator output 會被 C1-1 builder retry 使用：每個 `ValidationIssue.message` 必須足以讓 LLM 理解怎麼改。模板盡量帶上 `node name` / field name 的具體線索。

## Errors

Validator 本身不丟出 Python 例外；所有問題化作 `ValidationIssue`。唯一會 raise 的場景：
- `draft is None` → `TypeError`（caller bug）。
- `known_types is None` → `TypeError`。

## Acceptance Criteria

- [ ] 上表 19 條規則全部在 unit test 各有一個 fixture 觸發（positive + negative）。
- [ ] 「Manual Trigger → Set」最小有效 workflow → `ok=True`, errors=[]。
- [ ] 移除 trigger node → `V-TRIG-001` error。
- [ ] 讓 connections key 用 node id 而非 name → `V-CONN-001` error。
- [ ] `parameters=None` 觸發 V-NODE-007；`continueOnFail=True` 觸發 V-NODE-008 warn（不擋部署）。
- [ ] 所有 errors 的 `message` 都能讓 builder retry prompt 模板（R2-3 §3）內嵌後 LLM 能理解。
