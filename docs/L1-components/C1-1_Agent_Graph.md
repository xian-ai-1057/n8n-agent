# C1-1：Agent Graph（LangGraph）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-1, D0-2 ｜ **Prompts**: R2-3

## Purpose

定義 LangGraph state machine 的節點契約、邊、retry 策略與 prompt 引用。Phase 2-B 依此實作 `backend/app/agent/graph.py` 及各節點檔。

## Inputs

- `AgentState`（D0-2 §7）
- Retriever（C1-2）
- n8n client（C1-3）
- Validator（C1-4）
- Prompts（R2-3）
- LLM：`ChatOllama(model=LLM_MODEL).with_structured_output(Model, method="json_schema")`

## Outputs

- 一個可 `compile()` 的 `StateGraph`，進 `AgentState(user_message=...)` 即回傳最終 `AgentState`。
- 每個節點對 `AgentState` 的讀寫契約。

## Contracts

### 1. 圖結構

```
        ┌──────────┐
start → │  planner │
        └────┬─────┘
             ▼
        ┌──────────┐
        │  builder │ ◀─────────── (validator 失敗且 retry<2)
        └────┬─────┘
             ▼
        ┌──────────┐
        │assembler │
        └────┬─────┘
             ▼
        ┌──────────┐          ok=True
        │validator │──────────────▶ ┌──────────┐
        └────┬─────┘                │ deployer │ → END
       ok=False                     └──────────┘
             │
             ▼
        retry_count < 2 ? builder : END(error)
```

條件邊使用 LangGraph 的 `add_conditional_edges`；`validator → {builder, deployer, END}`。

### 2. 節點契約

每個節點為 `(state: AgentState) -> dict`（LangGraph reducer 風格），回傳要更新的欄位。

#### 2.1 planner

| 項目 | 內容 |
|---|---|
| 讀 | `user_message` |
| 寫 | `plan`, `discovery_hits`, `messages` |
| LLM | 是（`with_structured_output(list[StepPlan])`） |
| Prompt | `prompts/planner.md`（R2-3 §1） |
| 流程 | 1) `retriever.search_discovery(user_message, k=8)` 2) 格式化候選 node types 併入 prompt 3) 呼叫 LLM 取得 `list[StepPlan]` |

Prompt 嚴格約束：Planner 只能從 top-k 結果中挑 `candidate_node_types`。

#### 2.2 builder

| 項目 | 內容 |
|---|---|
| 讀 | `plan`, `candidates`（retry 時可能含前次錯誤） |
| 寫 | `built_nodes`, `connections`, `candidates`, `messages` |
| LLM | 是（`with_structured_output(BuilderOutput)`，見 §3） |
| Prompt | `prompts/builder.md`（R2-3 §2）；retry 模式改用 `prompts/fix.md`（R2-3 §3） |
| 流程 | 1) 對每個 StepPlan 的第一候選 type，呼叫 `retriever.get_detail(type)`，未命中就標示空殼 2) 組 prompt（含 NodeDefinition JSON） 3) LLM 輸出 BuiltNode[] + Connection[] 4) 寫入 state |

**Retry 模式**：若 `state.validation.errors` 非空且 `retry_count >= 1`，改用 fix prompt，把前次 `built_nodes`、`connections` 與 `errors` 全塞進 prompt。

#### 2.3 assembler

| 項目 | 內容 |
|---|---|
| 讀 | `built_nodes`, `connections`, `user_message` |
| 寫 | `draft` |
| LLM | 否（pure code） |
| 流程 | 1) 從 `user_message` 產出 workflow `name`（取前 60 字） 2) 組 `WorkflowDraft(name, nodes=built_nodes, connections=connections, settings={"executionOrder": "v1"})` 3) 無其他加工 |

Position 策略：若 builder 未提供 position，assembler 以 `x = 240 + i*220, y = 300` 依 `built_nodes` 順序補齊（與 R2-3 Builder prompt 建議一致，作為防守網）。

#### 2.4 validator

| 項目 | 內容 |
|---|---|
| 讀 | `draft` |
| 寫 | `validation`, `retry_count`（僅在失敗分支由 conditional edge 遞增 — 見 §3）, `messages` |
| LLM | 否（pure code — C1-4） |
| 流程 | 1) 跑 C1-4 全部規則 2) 產 `ValidationReport` 3) 若 `ok=False`，把 `errors` 以 JSON 字串 append 到 `messages`（role=`validator`） |

#### 2.5 deployer

| 項目 | 內容 |
|---|---|
| 讀 | `draft` |
| 寫 | `workflow_id`, `workflow_url`, `error` |
| LLM | 否 |
| 流程 | 呼叫 `n8n_client.create_workflow(draft)` → 回 `WorkflowDeployResult`（C1-3） → 寫回 state |

### 3. BuilderOutput（LLM 結構化輸出 wrapper）

LangChain `with_structured_output` 需要單一 root model。Builder 使用：

```python
from pydantic import BaseModel
from app.models.workflow import BuiltNode, Connection

class BuilderOutput(BaseModel):
    nodes: list[BuiltNode]
    connections: list[Connection]
```

Planner 類似：

```python
class PlannerOutput(BaseModel):
    steps: list[StepPlan]
```

### 4. 條件邊邏輯

```python
def after_validator(state: AgentState) -> str:
    if state.validation and state.validation.ok:
        return "deployer"
    if state.retry_count < 2:
        return "builder_retry"  # 映射回 builder，並把 retry_count+1
    return END

# 在進 builder_retry 的邊界 transform：{"retry_count": state.retry_count + 1}
```

`MAX_RETRIES = 2`（定義為常數，不是魔術數字）。第 0 次是首跑、第 1 次與第 2 次是兩次 retry；超過即結束並把 `error_message` 設為 "validator failed after 2 retries"。

### 5. Messages 格式

`state.messages` 是 diagnostics trail，格式：

```python
{"role": "planner" | "builder" | "validator" | "deployer" | "system", "content": "<text or json>"}
```

Validator 失敗時 content 為 `json.dumps([issue.model_dump() for issue in errors])`，Builder 下一輪 retry 會讀到。

### 6. 觀察性

每個節點進出各記一條結構化 log（見 D0-3 §7），欄位包含 `stage`, `retry_count`, `latency_ms`, `ok`。

## Errors

| 場景 | 行為 |
|---|---|
| Planner LLM 輸出不合 schema | LangChain 會 raise `OutputParserException` → 捕捉後 `state.error = "planning_failed"`，直接跳 END |
| Builder LLM 輸出不合 schema | 同上；計入 retry_count，若 < 2 則重跑 builder（不經 validator），否則 END |
| Retriever 找不到 detailed 定義 | `NodeCandidate.definition = None`；Builder 產空殼（parameters={}）並在 `messages` 記錄 WARN |
| Deployer 失敗 | 將 `DeployError` 訊息寫入 `state.error`，不重試（Deploy 失敗多為 auth / 網路，retry 沒意義） |

## Acceptance Criteria

- [ ] Graph 以 5 個節點 + 1 個條件邊實作完成，compile 後可 `invoke`。
- [ ] 單次成功流程不超過 `MAX_RETRIES=2` 以外的 builder 呼叫（planner 1 + builder 1 + deployer 1）。
- [ ] Retry 分支實測可重生成通過 validator 的 draft（plan §Verification 情境 3）。
- [ ] 每個節點的 log 包含 `stage` 與 `latency_ms`。
- [ ] CLI `python -m app.agent.graph "<prompt>"` 輸出最終 `AgentState` JSON（plan §P2-B 驗收）。
