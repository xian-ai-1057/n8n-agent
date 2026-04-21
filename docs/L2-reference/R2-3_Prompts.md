# R2-3：Prompts（Planner / Builder / Fix）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: C1-1, D0-2

## Purpose

定義 MVP 三段 prompt 的正式版（zh-Hant 指令 + English schema 欄位名），含 few-shot。Phase 2-B 直接存為 `backend/app/agent/prompts/{planner,builder,fix}.md`。

## Inputs

- 使用者自然語言需求
- Discovery RAG 命中結果（Planner）
- NodeDefinition JSON（Builder）
- 前次失敗輸出 + ValidationReport.errors（Fix）

## Outputs

- LLM 結構化輸出（by `with_structured_output(Model, method="json_schema")`）。

## Contracts — 共同規範

- LLM：`$LLM_MODEL`（預設 `Qwen/Qwen2.5-7B-Instruct`）via `ChatOpenAI`，端點為任意 OpenAI 相容伺服器（vllm / OpenAI / LiteLLM）。
- 不使用 `format="json"`；使用 `json_schema` 約束解碼。
- 語系策略：指令用 Traditional Chinese，JSON schema 欄位名保持 English；範例內容可雙語混用但以英文為主。
- 禁止 prompt 內出現 backtick 以外的 markdown 標題（避免與 LangChain prompt 模板互干擾）。

---

## §1 Planner Prompt

**檔案**：`prompts/planner.md`
**Input 變數**：`{user_message}`, `{discovery_hits}`（已格式化為列表字串）
**Output model**：`PlannerOutput { steps: list[StepPlan] }`（C1-1 §3）

### 1.1 Prompt（逐字）

```
你是 n8n workflow 規劃師。你的任務是把使用者需求拆解為有序步驟，並為每一步推薦 1~3 個候選 n8n node type。

==== 使用者需求 ====
{user_message}

==== 可用節點候選（由 RAG 檢索而來，含 type / display_name / category / description） ====
{discovery_hits}

==== 規則（必遵） ====
1. candidate_node_types 只能從「可用節點候選」清單挑選，**絕對不可**自己發明 type 字串。
2. steps[0].intent 必須是 "trigger"；整個 plan 中恰好一個 trigger。
3. step_id 為 "step_1"、"step_2"、… 依序。
4. description 為繁體中文，≤ 100 字。
5. reason 說明為何這些 candidate types 合適，≤ 150 字。
6. 若使用者需求含條件分支，加一個 intent="condition" 的步驟（通常 If 或 Switch）。
7. 若需要資料轉換（組欄位、計算），加 intent="transform" 的步驟（通常 Set 或 Code）。
8. 不要加重複步驟；不要加「測試」或「驗證」步驟。

==== 輸出格式 ====
僅輸出符合 PlannerOutput JSON schema 的 JSON，不要加解釋文字。
```

### 1.2 Few-shot（1 例）

```
# Input
user_message: "每小時抓 https://api.github.com/zen 存到 Google Sheet"
discovery_hits:
- n8n-nodes-base.scheduleTrigger | Schedule Trigger | Core Nodes | Triggers on a schedule
- n8n-nodes-base.httpRequest    | HTTP Request     | Core Nodes | Makes HTTP requests
- n8n-nodes-base.googleSheets   | Google Sheets    | Productivity | Read/write spreadsheets
- n8n-nodes-base.set            | Set              | Core Nodes | Set field values

# Output
{
  "steps": [
    {
      "step_id": "step_1",
      "description": "每小時觸發 workflow",
      "intent": "trigger",
      "candidate_node_types": ["n8n-nodes-base.scheduleTrigger"],
      "reason": "排程類觸發，對應 scheduleTrigger。"
    },
    {
      "step_id": "step_2",
      "description": "呼叫 GitHub Zen API 取得一行禪語",
      "intent": "action",
      "candidate_node_types": ["n8n-nodes-base.httpRequest"],
      "reason": "以 GET 取得文字內容，httpRequest 最符合。"
    },
    {
      "step_id": "step_3",
      "description": "將取得內容附加到 Google Sheet",
      "intent": "output",
      "candidate_node_types": ["n8n-nodes-base.googleSheets"],
      "reason": "寫入試算表，googleSheets 最符合。"
    }
  ]
}
```

---

## §2 Builder Prompt

**檔案**：`prompts/builder.md`
**Input 變數**：`{user_message}`, `{plan_json}`, `{definitions_json}`（依 plan 的候選 type 逐一附上 NodeDefinition）
**Output model**：`BuilderOutput { nodes: list[BuiltNode], connections: list[Connection] }`

### 2.1 Prompt（逐字）

```
你是 n8n workflow 建構器。根據規劃與節點 schema，產出合法的 BuiltNode 列表與 Connection 列表。

==== 使用者需求 ====
{user_message}

==== 步驟規劃（StepPlan 陣列） ====
{plan_json}

==== 節點詳細 schema（NodeDefinition 陣列，依候選 type 取得） ====
{definitions_json}

==== 規則（必遵） ====
1. 每個 StepPlan 對應**恰好一個** BuiltNode；依 step_id 順序產出。
2. BuiltNode.type 必須出自步驟的 candidate_node_types；優先選第一個。
3. BuiltNode.type_version 使用提供的 NodeDefinition.type_version；**不要**自行改版本。
4. BuiltNode.parameters **只能**使用 NodeDefinition.parameters 中列出的 name；未列出的 key 一律省略（不要猜）。
5. 若 NodeDefinition 未提供（definitions_json 無對應項），BuiltNode.parameters 設為 {}，並在 name 後保持原步驟描述——此節點視為「空殼」，後續由使用者手動補。
6. BuiltNode.name 使用 display_name；若同一 workflow 會重複，請加尾碼 " 2"、" 3"。
7. BuiltNode.position 依步驟索引 i（0-based）給 [240 + i*220, 300]；分支節點若往下走加 y+=100。
8. BuiltNode.id 產生為 uuid v4 字串。
9. Connection 必須以 **node name**（不是 id）串接；線性流程即 step_1 → step_2 → …；分支節點 If/Switch 使用 source_output_index 0/1。
10. credentials、disabled、onError、notes 等選填欄位**除非必要一律省略**。

==== 輸出格式 ====
僅輸出符合 BuilderOutput JSON schema 的 JSON。
```

### 2.2 Few-shot（1 例）

```
# Input
user_message: "手動觸發後設一個 greeting='hello' 欄位"
plan_json:
[
  {"step_id":"step_1","description":"手動觸發","intent":"trigger",
   "candidate_node_types":["n8n-nodes-base.manualTrigger"],"reason":"..."},
  {"step_id":"step_2","description":"設 greeting=hello","intent":"transform",
   "candidate_node_types":["n8n-nodes-base.set"],"reason":"..."}
]
definitions_json:
[
  {"type":"n8n-nodes-base.manualTrigger","type_version":1,"parameters":[]},
  {"type":"n8n-nodes-base.set","type_version":3.4,
   "parameters":[
     {"name":"assignments","type":"fixedCollection","required":true,"default":{}},
     {"name":"options","type":"collection","required":false,"default":{}}
   ]}
]

# Output
{
  "nodes": [
    {
      "id": "c7e1b9b7-8e0b-4e23-9b10-000000000001",
      "name": "Manual Trigger",
      "type": "n8n-nodes-base.manualTrigger",
      "typeVersion": 1,
      "position": [240, 300],
      "parameters": {}
    },
    {
      "id": "c7e1b9b7-8e0b-4e23-9b10-000000000002",
      "name": "Set",
      "type": "n8n-nodes-base.set",
      "typeVersion": 3.4,
      "position": [460, 300],
      "parameters": {
        "assignments": {
          "assignments": [
            {"id": "a1", "name": "greeting", "value": "hello", "type": "string"}
          ]
        },
        "options": {}
      }
    }
  ],
  "connections": [
    {
      "source_name": "Manual Trigger",
      "source_output_index": 0,
      "target_name": "Set",
      "target_input_index": 0,
      "type": "main"
    }
  ]
}
```

---

## §3 Fix Prompt（Validator retry 路徑）

**檔案**：`prompts/fix.md`
**Input 變數**：`{user_message}`, `{previous_nodes_json}`, `{previous_connections_json}`, `{errors_json}`, `{definitions_json}`
**Output model**：同 Builder（`BuilderOutput`）

### 3.1 Prompt（逐字）

```
你是 n8n workflow 修補器。前一次產出未通過 validator；請依 errors 修正並回傳**完整**的 nodes/connections。

==== 使用者需求（供參考，不要偏離） ====
{user_message}

==== 前次輸出（需要修正） ====
nodes: {previous_nodes_json}
connections: {previous_connections_json}

==== Validator 錯誤（每筆含 rule_id / message / path / node_name） ====
{errors_json}

==== 可用節點 schema（與 Builder 相同） ====
{definitions_json}

==== 修正規則（必遵） ====
1. 目標是讓 validator **所有 error 都消失**（warning 可忽略）。
2. **保留原 node name** 盡量不改；若必須改，connections 也要同步更新 source/target 名稱。
3. 只改 errors 點到的欄位；其他內容不要無謂重寫。
4. 禁止使用 `continueOnFail`；若見 V-NODE-008，請改用 `on_error` 欄位並設為 `"continueRegularOutput"` 或直接刪掉。
5. V-CONN-001：connections 應以 source node **name** key；確認 source_name 使用 name 字串。
6. V-NODE-004（unknown type）：把該節點改為 plan 允許的 candidate type 中最相近者。
7. V-NODE-005（typeVersion 非數字）：一律改回整數或浮點數。
8. V-TRIG-001：若缺 trigger，補一個 manualTrigger。

==== 輸出格式 ====
僅輸出 BuilderOutput JSON。
```

### 3.2 Few-shot（1 例）

```
# Input
previous_nodes_json:
[{"id":"n1","name":"Manual","type":"n8n-nodes-base.manualTrigger","typeVersion":"v1","position":[240,300],"parameters":{}},
 {"id":"n2","name":"Set","type":"n8n-nodes-base.set","typeVersion":3.4,"position":[460,300],"parameters":{}}]
previous_connections_json:
[{"source_name":"n1","source_output_index":0,"target_name":"Set","target_input_index":0,"type":"main"}]
errors_json:
[
  {"rule_id":"V-NODE-005","severity":"error","message":"node 'Manual' typeVersion must be a number, got str","node_name":"Manual","path":"nodes[0].typeVersion"},
  {"rule_id":"V-CONN-001","severity":"error","message":"connection key 'n1' is not a known node name","node_name":null,"path":"connections['n1']"}
]

# Output
{
  "nodes": [
    {"id":"n1","name":"Manual","type":"n8n-nodes-base.manualTrigger","typeVersion":1,"position":[240,300],"parameters":{}},
    {"id":"n2","name":"Set","type":"n8n-nodes-base.set","typeVersion":3.4,"position":[460,300],"parameters":{}}
  ],
  "connections": [
    {"source_name":"Manual","source_output_index":0,"target_name":"Set","target_input_index":0,"type":"main"}
  ]
}
```

---

## Errors

| 情境 | 行為 |
|---|---|
| LLM 忽略 `candidate_node_types` 限制，產出未知 type | 交由 validator V-NODE-004 → fix prompt |
| LLM 偷帶 NodeDefinition 未列參數 | validator 未必擋；下一版考慮再加 schema-level 驗證 |
| LLM 回傳非 JSON（極少） | LangChain raise OutputParserException → C1-1 處理 |

## Acceptance Criteria

- [ ] 三份 prompt 以 `.md` 直接存檔、在 Jinja2 / f-string 模板中可被載入。
- [ ] 各自 few-shot 範例用 `PlannerOutput` / `BuilderOutput` model 驗證可通過。
- [ ] plan §Verification 三情境：prompt 跑 `qwen3.5:9b` 各 3 次，情境 1 & 2 有 ≥ 2/3 通過 validator（Phase 4 再調）。
- [ ] Fix prompt 能把故意帶錯 `typeVersion` 與 connection key 的輸入修成 pass。
