# n8n Source Code Insights

> 研究方法：使用 `gh api` 對 https://github.com/n8n-io/n8n 做 targeted 探索，
> 重點讀取 `packages/workflow/src/` 的型別定義與核心邏輯。

---

## 關鍵發現

### 1. Workflow JSON 結構

n8n workflow 的核心資料結構（`IWorkflowBase`）：

```typescript
interface IWorkflowBase {
  id: string;
  name: string;
  active: boolean;
  isArchived: boolean;
  nodes: INode[];           // flat array，不是 map
  connections: IConnections; // nested map，見下方
  settings?: IWorkflowSettings;
  staticData?: IDataObject;
  pinData?: IPinData;
  versionId?: string;
}
```

**INode 結構**（builder 需要精確產生的格式）：

```typescript
interface INode {
  id: string;           // UUID v4
  name: string;         // 人類可讀的唯一名稱（用於 connections）
  type: string;         // e.g. "n8n-nodes-base.slack"
  typeVersion: number;  // e.g. 2 或 2.4
  position: [number, number]; // [x, y] 像素座標
  parameters: INodeParameters;
  disabled?: boolean;
  credentials?: INodeCredentials;
  retryOnFail?: boolean;
  maxTries?: number;
  continueOnFail?: boolean;
  onError?: OnError;
}
```

**IConnections 的實際結構**（三層 nested map，是最容易出錯的地方）：

```typescript
type IConnections = {
  [sourceNodeName: string]: {   // level 1: 來源節點的 display name
    [connectionType: string]: { // level 2: NodeConnectionType (e.g. "main", "ai_tool")
      [outputIndex: number]: Array<{  // level 3: 第幾個 output port (0-indexed)
        node: string;                  // target node name (display name!)
        type: NodeConnectionType;      // 重複一次 type
        index: number;                 // target 的第幾個 input port
      } | null> | null              // null 表示這個 output port 沒接任何東西
    }
  }
}
```

**重要**：connections 的 key 是 **display name**（`node.name`），不是 `node.id`。
builder 產生 connection 時 source/target 必須與 node.name 完全相同（大小寫敏感）。

---

### 2. NodeConnectionType — 完整型別系統

```typescript
const NodeConnectionTypes = {
  AiAgent:          'ai_agent',
  AiChain:          'ai_chain',
  AiDocument:       'ai_document',
  AiEmbedding:      'ai_embedding',
  AiLanguageModel:  'ai_languageModel',
  AiMemory:         'ai_memory',
  AiOutputParser:   'ai_outputParser',
  AiRetriever:      'ai_retriever',
  AiReranker:       'ai_reranker',
  AiTextSplitter:   'ai_textSplitter',
  AiTool:           'ai_tool',
  AiVectorStore:    'ai_vectorStore',
  Main:             'main',           // 普通資料流
} as const;
```

**關鍵規則**：
- `main` type 是普通 node 之間的資料流（一個 output 可接多個 target）
- `ai_*` types 是 LangChain node 之間的「supply」關係，**連線方向是反向的**：
  - AI Tool 節點「supply」to AI Agent（連線 map 是：AI Tool → AI Agent 的 input port）
  - AI LM 節點 supply → AI Agent 的 `ai_languageModel` port
  - 這些 connection 的 index 通常是 0

---

### 3. Node Schema 定義（INodeTypeDescription）

每個 n8n node 的「能力宣告」：

```typescript
interface INodeTypeDescription extends INodeTypeBaseDescription {
  version: number | number[];        // 支援的版本（可以是 array）
  inputs: Array<NodeConnectionType | INodeInputConfiguration> | ExpressionString;
  outputs: Array<NodeConnectionType | INodeOutputConfiguration> | ExpressionString;
  properties: INodeProperties[];     // 所有 UI 參數
  credentials?: INodeCredentialDescription[];
  polling?: true;
  webhook?: ...; trigger?: ...;     // trigger 能力
}
```

**INodeProperties — 參數 Schema**（builder 生成 parameters 時的依據）：

```typescript
interface INodeProperties {
  displayName: string;
  name: string;                      // 在 parameters 中的 key
  type: NodePropertyTypes;           // 'string'|'number'|'boolean'|'options'|'collection'...
  default: NodeParameterValueType;
  required?: boolean;
  displayOptions?: IDisplayOptions;  // 條件顯示/隱藏邏輯
  options?: Array<INodePropertyOptions | INodeProperties>;  // for type='options'
  validateType?: FieldType;          // 執行時型別驗證
}
```

**displayOptions — 動態條件 schema**（告訴 UI 何時顯示/隱藏某個 parameter）：

```typescript
interface IDisplayOptions {
  show?: {
    '@version'?: number[];                    // 版本條件
    [paramName: string]: Array<NodeParameterValue | DisplayCondition>;
  };
  hide?: {
    [paramName: string]: Array<NodeParameterValue | DisplayCondition>;
  };
}
```

實際範例（只在 operation='message' 時顯示 channel 欄位）：
```typescript
displayOptions: {
  show: { operation: ['message', 'reply'] }
}
```

---

### 4. Validation 機制

n8n 官方的 validation 分兩層：

**Layer 1：Node-level validation（`node-validation.ts`）**
```typescript
// 只驗 credentials，不驗 parameters
function validateNodeCredentials(node, nodeType): NodeCredentialIssue[]

// 驗 connection 連通性（有沒有被連）
function isNodeConnected(nodeName, connections, connectionsByDestination): boolean

// 判斷是否是 trigger-like
function isTriggerLikeNode(nodeType): boolean
```

**Layer 2：Workflow-level validation（`workflow-validation.ts`）**
```typescript
// 唯一的 workflow-level validator：只驗「至少有一個 trigger」
function validateWorkflowHasTriggerLikeNode(
  nodes, nodeTypes, ignoreNodeTypes?
): { isValid: boolean; error?: string }
```

**重要發現**：n8n 官方在 `packages/workflow` 層的 validation **極簡**，只驗 trigger 存在。
**大量的 parameter validation 是在 Editor-UI 端（前端）做的**，不是 server-side。
→ 這意味著我們的 backend validator（C1-4）的設計方向是正確的，但 n8n 自己也沒做 server-side parameter validation，我們不需要過度嚴格。

**graph-utils.ts 的 graph 分析能力**（可借鑑）：
```typescript
// 可以做的圖分析
getRootNodes()          // 找出沒有 incoming main edges 的節點（trigger candidates）
getLeafNodes()          // 找出沒有 outgoing edges 的節點（output nodes）
getInputEdges()         // 從外部進入子圖的 edges
getOutputEdges()        // 從子圖出去的 edges

// 錯誤類型（可直接借鑑）
type ExtractableErrorResult =
  | 'Multiple Input Nodes'
  | 'Multiple Output Nodes'
  | 'Input Edge To Non-Root Node'
  | 'Output Edge From Non-Leaf Node'
  | 'No Continuous Path From Root To Leaf'
```

**`Workflow` class 的 connection traversal**（可借鑑的 API）：
```typescript
class Workflow {
  // 建立兩個方向的 connection index
  connectionsBySourceNode: IConnections
  connectionsByDestinationNode: IConnections  // 從 source 反推

  // 圖遍歷 API
  getParentNodes(nodeName, type, depth): string[]
  getConnectedNodes(connections, nodeName, type, depth): string[]
  getParentNodesByDepth(nodeName, maxDepth): IConnectedNode[]  // BFS
}
```

---

### 5. Versioned Node 模式（可借鑑）

n8n 的 node 版本管理：

```typescript
// Slack.node.ts - 多版本入口
class Slack extends VersionedNodeType {
  constructor() {
    const baseDescription = { ... defaultVersion: 2.4 };
    const nodeVersions = {
      1: new SlackV1(baseDescription),
      2: new SlackV2(baseDescription),
      2.4: new SlackV2(baseDescription),  // 多個版本可共用實作
    };
    super(nodeVersions, baseDescription);
  }
}
```

→ node catalog 若要支援多版本，可以用類似 `{ [version]: definition }` 的 map。

---

### 6. Zod Schema validation（schemas.ts）

n8n 用 Zod 做 runtime validation（這是我們沒有做的）：

```typescript
// NodeParameters 的 recursive Zod schema
const INodeParameterResourceLocatorSchema = z.object({
  __rl: z.literal(true),
  mode: z.string(),
  value: z.union([z.string(), z.number(), z.null()]),
  ...
});

const FieldTypeSchema = z.enum([
  'boolean', 'number', 'string', 'dateTime', 'array', 'object', 'options', 'url', ...
]);
```

→ 我們的 Pydantic validator 相當於 n8n 的 Zod schema，設計方向一致。

---

## 可借鑑的設計模式

### 模式 A：Source/Destination 雙向 Connection Index

n8n 的 `Workflow` class 一次建立兩個方向的 connection index：
```python
# n8n 作法（Python 翻譯）
self.by_source = connections   # source → [targets]
self.by_dest = self._invert(connections)  # target → [sources]
```

**我們的 builder 應該做同樣的事**：驗連線合法性時需要兩個方向同時快速查詢。
目前 validator 每次驗都重建 dest index，可以 cache。

### 模式 B：displayOptions 做 conditional validation

n8n 的 `displayParameter()` 函數在 validate required parameters 前，先用 displayOptions 判斷這個 parameter 是否「應該顯示」，只有顯示中的 required parameter 才需要驗。

**我們的 V-PARAM-001 應該這樣實作**：
```python
def should_validate_param(node_params, param_def, node_type_desc):
    if param_def.get("displayOptions"):
        # 先評估 displayOptions
        if not display_parameter(node_params, param_def, node_type_desc):
            return False  # hidden, skip validation
    return True
```

### 模式 C：Connection Map 的 null 值語意

n8n 的 connection array 裡 `null` 表示「這個 output port 存在但沒接東西」（用於 switch node）：
```json
{
  "SwitchNode": {
    "main": [
      [{"node": "Branch1", "type": "main", "index": 0}],  // output 0
      null,                                                 // output 1 空接
      [{"node": "Branch2", "type": "main", "index": 0}]   // output 2
    ]
  }
}
```

→ 我們的 builder prompt 應該告訴 LLM 這個規則，確保 switch 節點能正確表示「跳過某個 branch」。

### 模式 D：節點 display name vs type name 的嚴格分離

n8n 在所有地方用 `node.name`（display name，可以是任何字串）作為 connection 的 key，
而 `node.type` 是 package qualified name（`n8n-nodes-base.slack`）。

**我們 builder 的 bug 根源**：LLM 常把 type 當成 connection 的 key 用。
→ 應在 builder prompt 明確說：「connections 裡的所有 node 名字必須與 nodes 陣列的 name 欄位完全相同」，並在 V-CONN 做 exact match 驗證（已有 V-CONN-002）。

---

## 對我們 builder 的具體建議

### 建議 1：借鑑 n8n 的 connection inversion 做快速驗證

```python
# 加入 WorkflowValidator（C1-4）
def _build_dest_index(self, connections):
    """IConnections → {target_name: [source_name, ...]}"""
    dest = defaultdict(list)
    for src, types in connections.items():
        for conn_type, outputs in types.items():
            for output in (outputs or []):
                for conn in (output or []):
                    if conn:
                        dest[conn["node"]].append(src)
    return dest
```

→ 用這個 index 做 V-CONN-004（孤立節點），效率從 O(N²) → O(N)。

### 建議 2：把 AI connection type 加入 builder prompt

n8n 官方定義了 14 種 connection type，其中 12 種是 `ai_*`。
我們的 builder prompt 目前只提到 `main` type，導致 AI Agent workflow 完全失敗。

**立即可以做的**：在 builder system prompt 加：
```
AI nodes 的 connection 使用 "ai_languageModel" / "ai_tool" / "ai_memory" type。
AI Agent 節點的 connections map：
  source=<AI_LM_Node>, type="ai_languageModel", target=<AI_Agent_Node>, index=0
  source=<Tool_Node>,   type="ai_tool",          target=<AI_Agent_Node>, index=0
```

### 建議 3：Workflow diff 功能（借鑑 workflow-diff.ts）

n8n 的 `workflow-diff.ts` 用於比較兩個 workflow 版本：
```typescript
function compareWorkflowsNodes(base, target): WorkflowDiff  // id-based
function compareConnections(base, target): ConnectionsDiff
```

→ Critic node（C1-7）可以借鑑這個：把 builder 輸出的 workflow 與「理想 template」做 diff，
  定位哪些 node 的 parameters 明顯偏差。

### 建議 4：Validation 只做 server-side 能做的事

n8n 官方告訴我們：**parameter-level validation 應在 UI 端做**，server-side 只驗結構。
→ V-PARAM-001（required field 非空）是合理的服務端驗證。
→ V-PARAM 的「parameter value 是否合邏輯」不該在 server-side 阻斷，改 Critic 做。

### 建議 5：用 n8n 的 STARTING_NODE_TYPES 做精確 trigger 判斷

```python
STARTING_NODE_TYPES = {
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.executeWorkflowTrigger",
    "n8n-nodes-base.errorTrigger",
    "n8n-nodes-base.formTrigger",
    "n8n-nodes-base.evaluationTrigger",
}
```

n8n 官方用「有 `poll/trigger/webhook` 方法」來判斷 trigger（runtime 判斷），
而 STARTING_NODE_TYPES 是給 UI 用的「開始位置」節點，兩個概念略不同。
→ 我們的 V-TRIG-001 用 `type.endswith("Trigger") or type.endswith("Webhook")` 是合理的 heuristic，
  與 n8n 官方的 `isTriggerLikeNode` 設計精神一致。
