# C1-4：Validator

> **版本**: v1.1.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, R2-1, R2-2 v1.1, C1-7, C1-8

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

#### 1a. V-PARAM-*（semantic parameter rules）

此組規則僅在以下條件**同時成立**時啟用：

- 該 node 的 `NodeDefinition`（來自 `catalog_detailed`，R2-2 §2）可被 validator 取得。
- 對應參數有 `schema_hint`（R2-2 v1.1）或 `required=true` 欄位可供判斷。

若 `validate_workflow` 呼叫端未傳入 `node_definitions`，本節所有規則整組略過（backwards compat，不 raise）。

| rule_id | sev | 說明 | 訊息模板 |
|---|---|---|---|
| V-PARAM-001 | E | 所有 `required=true` 的 parameter 在 `node.parameters` 內必須存在且非空字串 / 非空 list / 非空 dict | `"node '{name}' missing required param '{param}'"` |
| V-PARAM-002 | E | `schema_hint="url"` 的值必須是 `http://` 或 `https://` 開頭的字串，且不含明顯 placeholder（`TODO`, `<...>`, `{{...}}` 的 n8n expression 除外） | `"node '{name}' param '{param}' is not a valid URL: '{value}'"` |
| V-PARAM-003 | E | `schema_hint="cron"` 的值必須是合法 5 或 6 欄 cron（以 crontab 慣例驗證；或對應 n8n 的 rule.interval 結構非空） | `"node '{name}' param '{param}' has invalid cron: '{value}'"` |
| V-PARAM-004 | W | `schema_hint="email"` 的值需符合 RFC5322 簡化版 regex | `"node '{name}' param '{param}' is not a valid email"` |
| V-PARAM-005 | W | `schema_hint="datetime"` 必須是 ISO-8601 parseable | `"node '{name}' param '{param}' is not ISO-8601"` |
| V-PARAM-006 | W | `schema_hint="secret"` 的值在 `parameters` 裡為明文（非 credentials 引用）— 提醒要用 credential | `"node '{name}' param '{param}' looks like an inline secret; use credentials binding"` |
| V-PARAM-007 | E | `schema_hint="credential_ref"` 時，node 的 `credentials` 欄必須至少引用一個 key | `"node '{name}' needs credentials binding but 'credentials' is empty"` |
| V-PARAM-008 | E | 當 `NodeParameter.type=options` 時，填入值必須出現在 `options[*].value` 列舉中 | `"node '{name}' param '{param}' value '{value}' not in allowed options"` |
| V-PARAM-009 | E | placeholder 偵測：參數字串值命中下列 regex（case-insensitive）視為未填值，error 擋 deploy。詳見 §1a.1。 | `"node '{name}' param '{param}' appears to be a placeholder: '{value}'"` |

**Expression exemption**：n8n expression 語法 `{{ $json... }}` 或 `={{ ... }}` 開頭的字串**不視為** placeholder，也不被 V-PARAM-002/V-PARAM-009 誤判；檢測器需明確 exempt（見 C1-7 對語意 plausibility 的分工，§4）。

##### §1a.1 V-PARAM-009 實作細節（short-term workaround，C1-7 Critic 未實作前）

**Rule class**: `parameter_quality`（注意：不屬 `parameter`；保留給 schema-driven V-PARAM-001..008）。C1-1 `route_by_error_class` 把 `parameter_quality` 視為 `parameter` 處理，即走 fix_build。

**Statement**: `WorkflowValidator` 必須新增獨立方法（建議名 `_check_placeholders(data)`）掃所有 `nodes[*].parameters`（遞迴 dict / list），對每個字串 leaf value 執行下列偵測順序：

1. **Expression exemption**（先檢查）：若 value.strip() 以 `={{` 或 `{{` 開頭 → skip（視為 n8n expression）
2. **Empty string**：若 value 是空字串 `""` 且 param 名出現於 `NodeDefinition.parameters[*].required=true` 清單 → 已由 V-PARAM-001 處理，V-PARAM-009 skip；否則 skip（空字串不觸發 placeholder）
3. **Regex match**：若 value 命中下列 combined regex（`re.IGNORECASE`）→ raise ValidationIssue

**正式 regex**（權威定義，code 實作必須與此完全一致）:

```python
_PLACEHOLDER_PATTERN = re.compile(
    r"(?xi)"                                      # verbose, ignorecase
    r"(?:"
    r"  \bTODO\b                          | "     # word boundary guards
    r"  \bFIXME\b                         | "
    r"  \bREPLACE[_\s-]?ME\b              | "
    r"  \bXXX\b                           | "
    r"  <\s*fill[_\s-]?in\s*>             | "     # <fill_in>, <fill in>, <fill-in>
    r"  <\s*your[-_\s][^>]{1,40}\s*>      | "     # <your-api-key>, <your token>
    r"  \byour[-_]api[-_]key\b            | "
    r"  \byour[-_](token|secret|key)\b    | "
    r"  \bplaceholder\b                   | "
    r"  \bexample\.(?:com|org|net)\b"
    r")"
)
```

**Rationale for patterns**:
- `TODO` / `FIXME` / `XXX`：人工標記，明顯未填
- `REPLACE_ME` / `<fill_in>`：模板文件常見標示
- `<your-api-key>` / `your-api-key` / `your-token`：LLM 幻覺常產出的佔位字串（實測最高頻）
- `placeholder`（字面字）：常見於 n8n node defaults
- `example.com`：測試網域，deploy 後必然連不上

**Severity**: `ERROR`（阻擋 deploy）。升級自 spec v1.1 原本的 WARNING — 根據 bottleneck analysis §Critic-not-exists，placeholder 直達 n8n 是使用者回報 builder 失敗的最常見症狀，不擋不行。

**Path 欄位格式**: `nodes[{idx}].parameters.{param_path}`，其中 `param_path` 為 dot-notation 展開巢狀 dict、方括號展開 list index。範例：
- 平鋪: `nodes[2].parameters.url`
- 巢狀 dict: `nodes[3].parameters.bodyParameters.items[0].value`
- List of dict: `nodes[5].parameters.options.queryParameters[1].name`

**suggested_fix 欄位格式**（V-PARAM-009 **必填**）:

```
"replace placeholder '{matched_substring}' in param '{param_path}' with a concrete value (e.g. a real URL, API key from credentials, or n8n expression ={{ $json.xxx }})"
```

其中 `{matched_substring}` 為 regex 實際命中的子字串（`re.search().group(0)`），幫助 LLM 定位該改哪塊。

**Examples**:
- ❌ Fail: `url: "https://example.com/api"` → V-PARAM-009, matched=`example.com`
- ❌ Fail: `apiKey: "your-api-key"` → V-PARAM-009, matched=`your-api-key`
- ❌ Fail: `token: "<your-token>"` → V-PARAM-009, matched=`<your-token>`
- ❌ Fail: `body: {url: "TODO: add endpoint"}` → V-PARAM-009 at path `nodes[N].parameters.body.url`
- ✅ Pass: `url: "={{ $json.api_url }}"` → expression，skip
- ✅ Pass: `url: "https://api.example-real-domain.tld/v1"` → regex 不命中 `example-real-domain`（只命中 `example.com|.org|.net`）
- ✅ Pass: `description: "TODO in another context"`（如果此參數非必填且值非 URL/credential） — **設計決策**：保守起見仍觸發；使用者可改寫 description 避開。若誤報頻繁，可後續依 `NodeParameter.schema_hint` 限縮僅 URL/secret/credential 類 param 檢查。

**Activation condition**: V-PARAM-009 **不依賴** `node_definitions`（不同於 V-PARAM-001..008）。即使 caller 未傳 `node_definitions`，此規則仍會跑 — 這是 workaround 性質，最大覆蓋優先。

**Implementation 位置**: 放在 `WorkflowValidator._check_nodes` **之後**、`_check_connections` **之前**的新方法 `_check_placeholder_params(data)`。在 `validate()` 的 `issues.extend(...)` 鏈中插入對應一行。

**Test scenarios**（對應 C1-4 Acceptance）:
- 命中：`url="TODO"` / `apiKey="your-api-key"` / `token="<fill_in>"` / `host="example.com"` 各 1 例 → ERROR
- 不命中：`url="={{ $json.url }}"`、`url=""`（空字串，skip）、`description="Fix the todo list"`（word-boundary 守護：`todo` 有邊界但 `Fix the` 不是）→ **注意此例命中**，fixture 需提供真能 pass 的樣本如 `description="process the daily report"`
- 巢狀：`parameters.body.url="TODO"` → path 含正確展開
- 多命中：一個 node 有 2 個 placeholder param → 回 2 條 issue
- suggested_fix 包含 matched substring 與 param path
- rule_class == `"parameter_quality"`

**Security note**: 間接相關 — placeholder 不擋的情況下 `ssh` credential 欄位可能填成 `"root"` 或 `"<your-pass>"` 直接 deploy，但 V-SEC-001 會先擋 executeCommand/ssh 節點型。V-PARAM-009 補漏其他 credential 型節點。

#### 1b. V-SEC-*（security rules，來源 C1-8）

本節規則由 C1-8 Security 定義語意，validator 負責 deterministic 實作。黑名單／警告名單由環境變數覆寫（C1-8 §3），validator 建構時接收 `blocklist: set[str]` 與 `warnlist: set[str]` 參數；未傳入時套用預設值。

預設 block-list：`n8n-nodes-base.executeCommand`, `n8n-nodes-base.ssh`。
預設 warn-list：`n8n-nodes-base.code`。

| rule_id | sev | 說明 | 訊息模板 |
|---|---|---|---|
| V-SEC-001 | E | `type` 屬於 `NODE_BLOCKLIST`（預設含 `n8n-nodes-base.executeCommand`, `n8n-nodes-base.ssh`） | `"node '{name}' type '{type}' is in security blocklist"` |
| V-SEC-002 | W | `type` 屬於 warn-list（預設含 `n8n-nodes-base.code`） | `"node '{name}' type '{type}' runs arbitrary code; review carefully"` |

### 2. Rule class（供 graph error-class routing 使用）

C1-1 v2.0 將依據 error 來源把控制權路由回對應階段（planner / builder / give_up），而非一律回 builder。為此每條規則都需標註 `rule_class`，並透過 `ValidationIssue.rule_class` 外露給 graph 層。

| rule_class | 代表規則 | 修復責任層 |
|---|---|---|
| `structural` | V-TOP-*, V-NODE-001..003, V-NODE-005..009, V-CONN-001..003, V-TRIG-* | builder（fix_build） |
| `catalog` | V-NODE-004（unknown type） | planner（重挑候選） |
| `topology` | V-CONN-004, V-CONN-005 | builder（補連線） |
| `parameter` | V-PARAM-001..008（schema-driven） | builder（fix_build） |
| `parameter_quality` | V-PARAM-009（placeholder / 文字未填） | builder（fix_build） — router 與 `parameter` 同路徑 |
| `security` | V-SEC-001..002 | planner（換節點）或 give_up |

`ValidationIssue` 擴充欄位（對應 D0-2 後續 bump）：

```python
class ValidationIssue(BaseModel):
    rule_id: str
    rule_class: Literal[
        "structural",
        "catalog",
        "topology",
        "parameter",
        "parameter_quality",   # V-PARAM-009 (C1-4 v1.1.1)
        "security",
    ]
    severity: ValidationSeverity
    message: str
    node_name: str | None = None
    path: str | None = None
    suggested_fix: str | None = None   # NEW, optional, filled by rules that can
```

- `rule_class` 對所有 19 條舊規則 + 新 V-PARAM/V-SEC 皆非 None（見 Acceptance Criteria）。
- `suggested_fix` 選填；由具備具體修復建議的規則填入（例如 V-PARAM-001、V-PARAM-007、V-NODE-004）。
- **Data model update note**：D0-2 將 bump 以反映 `ValidationIssue.rule_class` 與 `suggested_fix` 兩欄；在其下一版發出前，此處為 authoritative source。

### 3. 簽章

```python
# backend/app/agent/validator.py
def validate_workflow(
    draft: WorkflowDraft,
    *,
    known_types: set[str],
    blocklist: set[str] | None = None,
    warnlist: set[str] | None = None,
    node_definitions: dict[str, NodeDefinition] | None = None,  # NEW: needed for V-PARAM-*
) -> ValidationReport: ...
```

純函式、無 I/O、無 LLM。

- 若 `node_definitions` 為 `None`，`V-PARAM-*` 整組**略過**（舊 caller 不需改動；backwards compat）。
- 若 `blocklist` / `warnlist` 為 `None`，套用 C1-8 §3 的預設值。顯式傳入空集合 `set()` 可關閉對應檢查（例本機開發）。
- `WorkflowValidator` class constructor 亦新增相同三個參數（signature parity）。
- 原 v1.0 透過 `catalog_path` 載入 `known_types` 的行為保留為 fallback，未被 deprecate。

### 4. 實作提示

- 按 top → node → connection → trigger → param → security 依序跑；前階段 error 後仍繼續跑（盡量把錯誤一次全回，讓 Builder retry 有完整資訊）。
- `path` 欄位（ValidationIssue）範例：`nodes[2].position`、`connections['HTTP Request']`、`nodes[1].parameters.url`。
- 遇 `node.parameters` 為 `None` 而非 `{}` → V-NODE-007 error（切勿靜默轉換）。
- V-PARAM-* 以 `NodeDefinition.parameters[*].schema_hint` 為 dispatch key；未標註 hint 的參數不跑 V-PARAM-002..008。
- V-PARAM-009 的 placeholder regex 必須先排除以 `={{` 或 `{{` 開頭的 n8n expression 字串，避免誤報。

### 5. 與 Retry 的耦合

- Validator output 會被 C1-1 builder retry 使用：每個 `ValidationIssue.message` 必須足以讓 LLM 理解怎麼改；模板盡量帶上 `node name` / field name 的具體線索。
- 當 `ValidationIssue.suggested_fix` 非 None 時，retry prompt（R2-3）應把該字串顯式嵌入 fix 區段，並指示 LLM **優先處理**帶有 `suggested_fix` 的規則（訊號比純 message 強）。
- `rule_class` 由 C1-1 v2.0 的 error-class router 消費：`catalog` → planner、`security` → planner / give_up、其餘 → builder fix_build。
- **Validator 不呼叫 Critic**：語意 plausibility 判斷是 C1-7 的職責。非重疊分工：
  - V-PARAM-* 處理**語法 / schema-driven** 問題（結構、列舉、regex、必填空值、placeholder 字面值）。
  - C1-7 Critic 處理**意圖與語意合理性**（例「這個 schedule 合理嗎」、「這份 workflow 達成使用者目標嗎」）。
  - 兩者都須通過才能 deploy；C1-7 僅在 V-* 全數通過後才被呼叫。

## Errors

Validator 本身不丟出 Python 例外；所有問題化作 `ValidationIssue`。唯一會 raise 的場景：
- `draft is None` → `TypeError`（caller bug）。
- `known_types is None` → `TypeError`。
- `node_definitions` 若傳入但其中任一項非 `NodeDefinition` instance → `TypeError`。

**Data model note**：本版新增 `ValidationIssue.rule_class`（必填）與 `ValidationIssue.suggested_fix`（選填）；D0-2 下次 bump 將同步。

## Acceptance Criteria

- [ ] 上表 19 條規則全部在 unit test 各有一個 fixture 觸發（positive + negative）。
- [ ] 「Manual Trigger → Set」最小有效 workflow → `ok=True`, errors=[]。
- [ ] 移除 trigger node → `V-TRIG-001` error。
- [ ] 讓 connections key 用 node id 而非 name → `V-CONN-001` error。
- [ ] `parameters=None` 觸發 V-NODE-007；`continueOnFail=True` 觸發 V-NODE-008 warn（不擋部署）。
- [ ] 所有 errors 的 `message` 都能讓 builder retry prompt 模板（R2-3 §3）內嵌後 LLM 能理解。
- [ ] V-PARAM-001..009 每條都有 fixture test（正反例）。
- [ ] V-SEC-001 對 `executeCommand` 觸發 error；可透過 `blocklist=set()` 關閉。
- [ ] `rule_class` 在所有 19 舊規則 + 新 V-PARAM/V-SEC 都非 None。
- [ ] 帶 `node_definitions=None` 呼叫 `validate_workflow` 時，V-PARAM-* 全部略過且不 raise。
- [ ] placeholder 偵測（V-PARAM-009）不會誤報 n8n expression `={{$json.id}}`。
- [ ] V-PARAM-009 對 `your-api-key`、`TODO`、`<fill_in>`、`example.com` 四類樣本全部觸發 ERROR。
- [ ] V-PARAM-009 遞迴偵測巢狀 parameters（`body.url="TODO"`）且 path 正確展開至 `nodes[N].parameters.body.url`。
- [ ] V-PARAM-009 在未傳 `node_definitions` 時仍執行（與 V-PARAM-001..008 不同）。
- [ ] V-PARAM-009 `rule_class == "parameter_quality"`，`suggested_fix` 包含 matched substring 與 param path。
- [ ] `suggested_fix` 至少在 V-PARAM-001、V-PARAM-007、V-PARAM-009、V-NODE-004 四條規則上填入具體字串。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版（19 條 deterministic rules） |
| v1.1.0 | 2026-04-21 | 新增 V-PARAM-001..009（語意 schema）與 V-SEC-001/V-SEC-002；ValidationIssue 新增 rule_class 與 suggested_fix；validate_workflow 簽章加入 node_definitions/blocklist/warnlist |
| v1.1.1 | 2026-04-22 | V-PARAM-009 升級：severity warning → error；獨立 rule_class `parameter_quality`；定義權威 placeholder regex 與 suggested_fix 格式；不依賴 `node_definitions`（C1-7 Critic 未實作前的 short-term workaround）。對應 backend bottleneck analysis P0 條目 |
