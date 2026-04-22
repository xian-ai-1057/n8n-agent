# C1-1：Agent Graph（LangGraph）

> **版本**: v2.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-1, D0-2, D0-3 v1.1, C1-2 v1.1, C1-4 v1.1, C1-7, C1-8 ｜ **Prompts**: R2-3

## Purpose

定義 LangGraph state machine 的節點契約、邊、retry 策略、HITL（human-in-the-loop）斷點與 prompt 引用。v2.0 相較 v1.0 有三項結構性變化：

1. **Builder 從「一次 bulk 產出」改為「per-step 迴圈」**：每個 StepPlan 一個 LLM call，降低長 JSON hallucination 率，且讓 prompt context 更專注於單一 `NodeDefinition`。
2. **Retry 依 `rule_class` 分流**：validator 錯誤依來源分類路由到 planner / builder / give_up，不再一律回 builder。
3. **HITL：Plan 後可中斷等待使用者確認**：預設啟用；確認後才進 build 子圖，避免白跑錯誤 plan。

同時整合 C1-7 critic 節點（validator 通過後跑語意檢查）與 C1-2 v1.1 的 templates 檢索（planner / builder 皆拿來當 few-shot）。

## Inputs

- `AgentState`（D0-2 v1.1；新增 `plan_approved`、`current_step_idx`、`critic`、`templates`、`fix_target`、`session_id`、`connections_built`）
- Retriever（C1-2 v1.1，含 `search_templates_by_query` / `search_templates_by_types` / `filter_by_coverage`）
- Validator（C1-4 v1.1，輸入需含 `node_definitions` 才會跑 V-PARAM-*）
- Critic（C1-7）
- n8n client（C1-3）
- Prompts（R2-3）
- LLM 工廠（D0-3 v1.1 分階段 model / temperature）

## Outputs

- 一個 `compile(checkpointer=...)` 的 `StateGraph`，可透過 `invoke()`（非 HITL）或 `stream()`（HITL / streaming UI）驅動。
- 每節點對 `AgentState` 的讀寫契約。
- 必要時在 `build_step_loop` 前 `interrupt_before` 暫停，等待 API 呼叫 `resume`。

## Contracts

### 1. 圖結構

```
           ┌──────────┐
start ───▶ │ planner  │
           └────┬─────┘
                │ plan + templates
                ▼
        ┌─────────────────┐
        │ await_plan      │  (HITL 斷點；hitl_enabled=False 時直接放行)
        │   _approval     │
        └────┬────────────┘
             │ plan_approved=True
             ▼
      ┌───────────────────┐
      │ build_step_loop   │  ◀───────────┐  (retry_target=builder)
      │  (per-step LLM)   │              │
      └────┬──────────────┘              │
           │ 全部 step 完成              │
           ▼                             │
      ┌───────────────────┐              │
      │ connections       │              │
      │   _linker         │              │
      └────┬──────────────┘              │
           ▼                             │
      ┌───────────────────┐              │
      │ assembler         │              │
      └────┬──────────────┘              │
           ▼                             │
      ┌───────────────────┐              │
      │ validator         │              │
      └────┬──────────────┘              │
           │                             │
           ▼                             │
    route_by_error_class:                │
      - ok              → critic         │
      - class=catalog   → replan ────────┐
      - class=security  → give_up        │
      - class=structural│parameter│topology
                        → fix_build ─────┘
      - retry 用盡      → give_up

    critic:
      - pass            → deploy → END
      - block + retry   → fix_build ─────┘
      - retry 用盡      → give_up
```

條件邊：
- `await_plan_approval → {build_step_loop, END}`（若使用者 reject plan，END 帶 `error="plan_rejected"`）
- `build_step_loop → {build_step_loop, connections_linker}`（self-loop until `current_step_idx == len(plan)`）
- `validator → {critic, fix_build, replan, give_up}`（由 `route_by_error_class` 決定）
- `critic → {deploy, fix_build, give_up}`
- `replan → await_plan_approval`（重新規劃後再請使用者確認一次）

`MAX_RETRIES = 2`（planner 與 builder 共用 budget；`state.retry_count` 單一欄位遞增）。

### 2. 節點契約

每節點仍為 `(state: AgentState) -> dict`。

#### 2.1 planner

| 項目 | 內容 |
|---|---|
| 讀 | `user_message`, `fix_target`（若為 replan 路徑帶有 `validation.errors` / `critic.concerns`） |
| 寫 | `plan`, `discovery_hits`, `templates`, `messages` |
| LLM | 是；`PLANNER_MODEL` / `PLANNER_TEMPERATURE`；`with_structured_output(PlannerOutput)` |
| 流程 | 1) 若 `QUERY_REWRITE_ENABLED=1`，先跑 query rewrite（C1-2 §8）。 2) `retriever.search_discovery(user_message, k=8)` — 內部已含 rerank（C1-2 §9）。 3) `retriever.filter_by_coverage(hits)` 把 `has_detail=True` 的候選前移。 4) `retriever.search_templates_by_query(user_message, k=3)` 取相似 workflow。 5) 組 planner prompt（R2-3 §1 v1.1），含 hits、templates、以及（replan 時）前次錯誤。 6) 呼叫 LLM → `PlannerOutput.steps`。 |

Prompt 規則：`candidate_node_types` 只能出自 `discovery_hits`；`templates` 作為上下文 hint，不強制抄襲。

**Replan 觸發條件**：`fix_target="planner"`（由 `route_by_error_class` 設定，通常因 V-NODE-004 unknown type 或 V-SEC-001 blocklist 命中）。replan 時 planner 被要求**排除**上次選過的問題 type。

#### 2.2 await_plan_approval（HITL 斷點）

| 項目 | 內容 |
|---|---|
| 讀 | `plan`, `hitl_enabled`（來自 config）|
| 寫 | `plan_approved`, `messages` |
| LLM | 否 |
| 流程 | 若 `hitl_enabled=False`（env `HITL_ENABLED=0` 或 `run_cli` 呼叫）：直接 `plan_approved=True` 通過。 否則：graph `interrupt_before` 此節點之後。API 端以 session_id 儲存 state；使用者呼叫 `/chat/{session_id}/confirm-plan` 帶入 `{approved: bool, edited_plan?: list[StepPlan]}`；handler 把 state merge 回後再 `resume`。 |

斷點實作：`compile(interrupt_before=["build_step_loop"])` + `MemorySaver` checkpointer（或未來 Redis）。`session_id` 當 thread id。

`edited_plan` 若帶入，覆蓋 `state.plan`，並把 `state.messages` 追加一筆 `{role:"user", content:"plan edited: ..."}`。

#### 2.3 build_step_loop（per-step 迴圈）

| 項目 | 內容 |
|---|---|
| 讀 | `plan`, `current_step_idx`, `templates`, `built_nodes`（累積） |
| 寫 | `built_nodes`（append 一個 BuiltNode）, `candidates`, `current_step_idx`（+1）, `messages` |
| LLM | 是；`BUILDER_MODEL` / `BUILDER_TEMPERATURE`（或 fix 時 `FIX_MODEL` / `FIX_TEMPERATURE`） |
| Prompt | `prompts/builder_step.md`（R2-3 §2 v1.1，per-step 版）；retry 時 `prompts/fix_step.md` |
| 流程 | 1) 取 `step = plan[current_step_idx]`。 2) 對 `step.candidate_node_types[0]` 呼叫 `retriever.get_detail(type)`；若無，依 C1-2 §5 四段降級（近似類別 → templates 參數實例 → empty shell）。 3) 以 `retriever.search_templates_by_types([chosen_type], k=2)` 取相似節點的 parameters 範例，塞入 prompt 作為 few-shot。 4) 呼叫 LLM → 產一個 `BuiltNode`。 5) append 到 `state.built_nodes`；`current_step_idx += 1`。 |

條件邊：`current_step_idx < len(plan)` → 回自己；否則 → `connections_linker`。

**Retry 模式**：若 `fix_target="builder"`（由 route 設定），state 含 `validation.errors` 或 `critic.concerns` 時，改跑 fix prompt，但仍 **逐步**（不是一次全重跑）—— 只重建被 errors 指涉的 `node_name` 對應的那一步；其他步驟的 `BuiltNode` 保留。讀取 `retry_count` 決定 fix 或 build。

#### 2.3.1 Candidate detail collection（v1.1-impl，bulk builder 也適用）

當前 v1 bulk builder 的 `_collect_candidates` 每個 step 只試第 0 個 candidate 且呼叫 `retriever.get_detail` O(S) 次 Chroma round-trip，是效能與可靠性雙重瓶頸。以下規則在 v1 bulk 與 v2 per-step 皆強制執行。

##### B-CAND-01: 批次 detail 查詢

**Statement**: Builder 準備 prompt definitions 時，必須以**單次** `retriever.get_definitions_by_types(types: list[str])` 批次查詢，而非 per-step 逐次 `get_detail`。`retriever` 介面（`RetrieverProtocol`）需新增此方法並回傳 `dict[str, NodeDefinition | None]`（input type → definition 或 None）。底層 `ChromaStore.get_by_ids` 已支援 list，需一路暴露至 retriever 層。

**Rationale**: 15-step plan 從 15 次 Chroma round-trip（約 30-80ms 每次）降至 1 次，latency 節省 ~400ms。且集中查詢便於觀察「哪些 type 無 detail」與 fallback 決策（B-CAND-02）。

**Affected files**:
- `backend/app/agent/retriever_protocol.py`（`RetrieverProtocol` 加 `get_definitions_by_types(types: list[str]) -> dict[str, NodeDefinition | None]`；stub `_FilesystemStubRetriever` 補對應實作）
- `backend/app/rag/retriever.py`（`Retriever` 類新增 method：`self._store.get_by_ids(COLLECTION_DETAILED, types)` → hydrate dict）
- `backend/app/agent/builder.py`（`_collect_candidates` 重寫使用新批次 API）

**Function signature**:
```python
# In RetrieverProtocol + Retriever + _FilesystemStubRetriever:
def get_definitions_by_types(
    self, types: list[str]
) -> dict[str, NodeDefinition | None]: ...
```

**Examples**:
- Input: `["n8n-nodes-base.httpRequest", "n8n-nodes-base.set", "unknown.type"]`
- Output: `{"n8n-nodes-base.httpRequest": <NodeDefinition>, "n8n-nodes-base.set": <NodeDefinition>, "unknown.type": None}`
- 與舊 `get_detail` 一致語意：未 index 的 type 對應值為 `None`（不拋例外）

**Test scenarios**:
- 批次輸入 3 種 type，其中 1 種未 index → 回傳 dict 含 3 keys，第 3 個 value 為 None
- 空 list 輸入 → 回傳 `{}`，**不**呼叫底層 store
- 重複 type 輸入 → 去重後查詢，但 output dict 保留所有輸入 keys（value 共用 reference）
- Stub retriever 與 Chroma retriever 行為一致（契約測試）

**Security note**: N/A

##### B-CAND-02: Candidate fallback 迴圈

**Statement**: 當 `step.candidate_node_types[0]` 在 B-CAND-01 批次結果中為 None（無 detail），builder **必須**依序嘗試 `candidate_node_types[1]`、`[2]`... 直到找到有 detail 的 type，才放入 `NodeCandidate.chosen_type`。若全部 candidates 皆無 detail，才走空殼路徑（`chosen_type = candidate_node_types[0]`, `definition = None`）並在 `messages` append 一筆 diagnostic 說明。

**Rationale**: Planner 提供最多 5 個 ranked candidates 是為了容錯；只試第 0 個浪費此設計。且 detail 命中率（~30/529）偏低，fallback 可顯著提升 builder 成功率。

**Affected files**:
- `backend/app/agent/builder.py`（`_collect_candidates` 加入 fallback loop）
- `backend/app/models/planning.py`（`NodeCandidate` 可選新增 `fallback_index: int = 0` 紀錄實際命中第幾個 candidate；僅觀察性，不影響 prompt schema）

**Function signature**:
```python
def _collect_candidates(
    plan: list[StepPlan], retriever: RetrieverProtocol
) -> tuple[list[NodeCandidate], list[NodeDefinition]]:
    # 1. 收 all_types = flatten(step.candidate_node_types for step in plan)
    # 2. details = retriever.get_definitions_by_types(unique(all_types))
    # 3. per step: for idx, t in enumerate(step.candidate_node_types):
    #      if details.get(t): chosen=t, defn=details[t], fallback_index=idx; break
    #    else: chosen=candidate_node_types[0], defn=None, fallback_index=-1
    # 4. return ordered candidates + deduped defs
```

**Examples**:
- Step candidates: `["a.unknown", "n8n-nodes-base.set", "n8n-nodes-base.noOp"]`
  - detail map: `{"a.unknown": None, "n8n-nodes-base.set": <defn>, "n8n-nodes-base.noOp": <defn>}`
  - 結果: `chosen_type="n8n-nodes-base.set"`, `fallback_index=1`, messages 新增一筆 `{role:"builder", content:"fallback: step=... skipped 1 no-detail candidate(s)"}`
- Step candidates: `["a", "b", "c"]`, 全 None
  - 結果: `chosen_type="a"`, `definition=None`, `fallback_index=-1`, messages 新增 `{role:"builder", content:"fallback_exhausted: step=... all 3 candidates lack detail"}`
- Step candidates: `["n8n-nodes-base.set"]`, 第 0 個有 detail
  - 結果: `chosen_type="n8n-nodes-base.set"`, `fallback_index=0`, 不新增 diagnostic message

**Test scenarios**:
- 單一 step，first candidate 無 detail，second 有 → chosen = second；fallback_index == 1
- 單一 step，全部 candidates 無 detail → chosen = first；definition is None；diagnostic message appended
- 多 step，只有部分 step 需要 fallback → 只對應 step 有 diagnostic
- Empty plan → 回傳 `([], [])`，不呼叫 retriever
- `candidate_node_types` 為空 list（planner bug）→ step 被 skip，不報錯（現行行為保留）

**Security note**: N/A。但如果 fallback 命中率 > 20% 長期成立，表示 planner 與 detailed catalog 有系統性 drift，應報到 C1-2 treat。

#### 2.4 connections_linker

| 項目 | 內容 |
|---|---|
| 讀 | `plan`, `built_nodes` |
| 寫 | `connections`, `messages` |
| LLM | 是（獨立 call）；`BUILDER_MODEL`；`with_structured_output(ConnectionsOutput)` |
| Prompt | `prompts/connections.md`（R2-3 §4 新增） |
| 流程 | 輸入 node name / type 列表 + plan，輸出 `list[Connection]`。單獨一次 LLM call 專注連線，降低與 node param 互相干擾。 |

簡單線性流程（無 condition intent）可 skip LLM 走純 Python：`for i in range(len-1): connections.append(Connection(built_nodes[i].name, built_nodes[i+1].name))`。由 `should_skip_llm_linker(plan)` 判斷（plan 內無 `intent=="condition"` 即 skip）。

#### 2.5 assembler

| 項目 | 內容 |
|---|---|
| 讀 | `built_nodes`, `connections`, `user_message` |
| 寫 | `draft` |
| LLM | 否 |

不變；維持 v1.0 語意（UUID 指派、position layout、derive name、`settings.executionOrder="v1"`）。

#### 2.6 validator

| 項目 | 內容 |
|---|---|
| 讀 | `draft`, `candidates`（提供 `node_definitions` 給 V-PARAM-*） |
| 寫 | `validation`, `messages` |
| LLM | 否（C1-4 v1.1） |
| 流程 | 1) 從 `candidates[*].definition` 組 `node_definitions: dict[str, NodeDefinition]`。 2) 帶 `blocklist` / `warnlist`（來自 env via C1-8）呼叫 `validate_workflow`。 3) 把 `ValidationReport.errors` 以 JSON 字串 append 到 `messages`（role=`validator`）。 |

#### 2.7 route_by_error_class（條件邊）

純函式、無 state 寫入：

```python
def route_by_error_class(state: AgentState) -> Literal["critic","fix_build","replan","give_up"]:
    v = state.validation
    if v is None:
        return "give_up"
    if v.ok:
        return "critic"
    # 取第一個 error class（規則：security > catalog > 其他）
    classes = {e.rule_class for e in v.errors}
    if "security" in classes:
        return "give_up"           # 安全層級失敗不 retry
    if state.retry_count >= MAX_RETRIES:
        return "give_up"
    if "catalog" in classes:
        # V-NODE-004 unknown type — 回 planner 重挑
        return "replan"
    return "fix_build"             # structural / parameter / topology
```

路由同時把 `fix_target` 寫回 state：`"planner"` for replan、`"builder"` for fix_build。

**replan 在 HITL 模式下會再經一次 await_plan_approval**（使用者可能想直接改 plan 而非重跑）。

#### 2.8 fix_build

Fix 是 `build_step_loop` 的另一個進入邊，而非獨立節點。進入 fix_build 前 `route_by_error_class` 會：
- `retry_count += 1`
- `current_step_idx = 0` **只** 當 errors 涉及多步時；單一 `node_name` 時 `current_step_idx = index_of(node_name)` 精準回溯到出錯步驟。
- `fix_target = "builder"`

Fix prompt（`prompts/fix_step.md`）把 validator errors + critic concerns + 前次 `BuiltNode` 塞入，要求產新版。

#### 2.9 critic（C1-7）

| 項目 | 內容 |
|---|---|
| 讀 | `draft`, `user_message`, `plan` |
| 寫 | `critic`, `messages` |
| LLM | 是；`CRITIC_MODEL` / `CRITIC_TEMPERATURE=0`；`with_structured_output(CriticReport)` |
| 流程 | 只在 validator `ok=True` 後呼叫。逾時 / 例外 fail-open（`pass=True` + messages 警告）— 詳 C1-7 §Errors。 |

條件邊：
- `critic.pass=True` → `deploy`
- `critic.pass=False` 且 `retry_count<MAX_RETRIES` → `fix_build`（`fix_target="builder"`，concerns 塞進 fix prompt）
- 否則 → `give_up`

#### 2.10 deployer

不變（C1-1 v1 §2.5）；寫 `workflow_id` / `workflow_url`。

#### 2.11 give_up

| 項目 | 內容 |
|---|---|
| 讀 | `validation`, `critic`, `retry_count` |
| 寫 | `error`, `messages` |

`error` 訊息格式化：`"{cause} after {retry_count} retries; {n_val_errors} validator errors, {n_critic} critic concerns"`，其中 cause ∈ {`"validator failed"`, `"critic failed"`, `"security blocked"`, `"plan rejected"`}。

### 3. 結構化輸出 Schema

所有 LLM call 都用 `with_structured_output(Model, method="json_schema")`。

```python
class PlannerOutput(BaseModel):
    steps: list[StepPlan]

class BuilderStepOutput(BaseModel):
    """Per-step builder — 一次產一個節點."""
    node: BuiltNode

class ConnectionsOutput(BaseModel):
    connections: list[Connection]

# CriticReport 見 C1-7。
```

**棄用**：v1.0 的 `BuilderOutput { nodes, connections }` 在 v2.0 拆為 `BuilderStepOutput`（迴圈內）+ `ConnectionsOutput`（迴圈後）。

### 4. Retry 策略（整理）

| 條件 | 行為 | retry_count |
|---|---|---|
| validator.ok 且 critic.pass | deploy | 不動 |
| security error | give_up | 不動 |
| catalog error | replan（回 planner） | +1 |
| structural / parameter / topology error | fix_build | +1 |
| critic block | fix_build | +1 |
| `retry_count >= MAX_RETRIES` 且仍失敗 | give_up | — |

`MAX_RETRIES = 2`。replan 與 fix_build 共用同一個 counter，避免「先 replan 1 次 + fix 2 次 = 3 輪 LLM」的成本失控。

### 5. HITL 控制

**Config**：`HITL_ENABLED`（env / Settings，預設 `1`）。亦可透過 `run_cli(hitl=False)` 強制關閉供 CI / eval 使用。

**API 互動**（細節見 C1-5 v2.0）：
- `POST /chat` 在 HITL 模式下回傳 `{session_id, plan, workflow_json: null, status: "awaiting_plan_approval"}`。
- `POST /chat/{session_id}/confirm-plan` 帶 `{approved: bool, edited_plan?: list[StepPlan]}`。`approved=false` → graph END 帶 `error="plan_rejected"`。
- Checkpointer：`MemorySaver()` for MVP；TTL=30min by `session_id`。逾時則 state 被 GC，再次呼叫 `confirm-plan` 得 404。

**replan 與 HITL 互動**：replan 後必經 `await_plan_approval`；使用者可依新 plan 再決定。為避免無限 replan，replan 次數計入 `retry_count`。

### 6. Messages 格式

`state.messages` 新增 role：`critic`, `router`, `hitl`。整體 shape 不變：
```python
{"role": "planner"|"builder"|"validator"|"critic"|"router"|"hitl"|"deployer"|"system"|"user", "content": str}
```

`router` 角色用於記錄 `route_by_error_class` 的決策（方便 debug）。

### 7. 觀察性

每節點進出各記一筆結構化 log：
- `stage`（節點名）
- `retry_count`
- `current_step_idx`（builder loop）
- `latency_ms`
- `tokens_prompt` / `tokens_completion`（若 LLM handler 提供）
- `ok`（節點是否成功完成；不代表最終 validator 結果）

Eval harness（D0-5）會消費這些欄位。

### 8. 組裝範例

```python
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

def build_graph(
    retriever,
    *,
    deploy_enabled: bool = True,
    hitl_enabled: bool = True,
) -> CompiledGraph:
    g = StateGraph(AgentState)
    g.add_node("planner", _make_planner(retriever))
    g.add_node("await_plan_approval", _await_plan_approval(hitl_enabled))
    g.add_node("build_step_loop", _make_build_step(retriever))
    g.add_node("connections_linker", _make_connections_linker())
    g.add_node("assembler", assemble_step)
    g.add_node("validator", validate_step)
    g.add_node("critic", critic_step)
    g.add_node("fix_build", _make_fix_step(retriever))   # same node family as build_step_loop
    g.add_node("deployer", deploy_step if deploy_enabled else _dry_run_deploy)
    g.add_node("give_up", _give_up_step)

    g.add_edge(START, "planner")
    g.add_edge("planner", "await_plan_approval")
    g.add_conditional_edges("await_plan_approval",
                            lambda s: "build_step_loop" if s.plan_approved else "give_up")
    g.add_conditional_edges("build_step_loop",
                            lambda s: "build_step_loop" if s.current_step_idx < len(s.plan)
                                      else "connections_linker")
    g.add_edge("connections_linker", "assembler")
    g.add_edge("assembler", "validator")
    g.add_conditional_edges("validator", route_by_error_class)
    g.add_conditional_edges("critic", _after_critic)
    g.add_edge("fix_build", "build_step_loop")
    g.add_edge("deployer", END)
    g.add_edge("give_up", END)

    checkpointer = MemorySaver() if hitl_enabled else None
    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["build_step_loop"] if hitl_enabled else [],
    )
```

## Errors

| 場景 | 行為 |
|---|---|
| Planner LLM 輸出不合 schema | `state.error = "planning_failed"`，跳 END；不計 retry |
| Planner LLM timeout | `state.error = "planning_timeout"`，跳 END；不計 retry |
| Planner 產生 0 steps | validator 會抓 `V-TOP-002` 後照 class routing 處理（catalog-class → replan） |
| Per-step builder 逾時（單一步驟） | 見 B-TIMEOUT-01：raise `BuilderTimeoutError`；graph 條件邊路由至 `give_up`；不走 fix |
| Builder LLM 其他例外 | `state.error = "building_failed: {exc}"`；跳 END；不計 retry |
| `current_step_idx` 超出 plan 範圍（程式 bug） | `fix_build` 節點前置檢查若 `current_step_idx >= len(plan)` 直接 clamp；不 raise |
| HITL timeout（使用者 30min 未 confirm） | `session_id` 被 MemorySaver GC；下次 `confirm-plan` 回 404（C1-5 v2 處理） |
| Critic 逾時 | fail-open（C1-7 §Errors） |
| Deployer 失敗 | `state.error`，不 retry（同 v1） |

### Errors §A. Builder timeout handling（v1.1-impl，對應當前 code）

當前 code 為 v1.0 bulk builder（single-call 同時吐 `nodes` + `connections`），per-step v2 尚未實作。在 bulk builder 上下文下，timeout 語意須修正以避免 silent failure：

#### B-TIMEOUT-01: Builder timeout 必須 raise，不得退化為空 list

**Statement**: 當 `invoke_with_timeout` 在 builder / fix stage 拋出 `LLMTimeoutError`，builder node **不得**回傳 `{"built_nodes": [], "connections": [], ...}` 讓 graph 繼續走 assembler。必須改為拋出新例外 `BuilderTimeoutError`（繼承 `RuntimeError`），讓 LangGraph node wrapper catch 並寫入 `state.error = "building_timeout: {cause}"`，條件邊將狀態路由至 `give_up`。

**Rationale**: 空 `built_nodes` 會讓 assembler 產出無 node 的 `WorkflowDraft` → validator 報 V-TOP-002 + V-TRIG-001（結構錯）→ `fix_build` 被叫起來「修上次 output」，但上次根本是空。Fix prompt 的語意完全走歪，retry 做無效工作。

**Affected files**:
- `backend/app/agent/builder.py`（新增 `BuilderTimeoutError`；`except LLMTimeoutError` 分支由 return 改為 raise）
- `backend/app/agent/graph.py`（node factory 外層 catch `BuilderTimeoutError`，寫 `error` 欄位後讓條件邊路由至 `give_up`）
- `backend/app/models/agent_state.py`（`error` 欄位語意規範見 B-TIMEOUT-02）

**Examples**:
- ❌ 舊行為: timeout → return empty → assembler → validator V-TOP-002 → fix_build → LLM 困惑
- ✅ 新行為: timeout → raise BuilderTimeoutError → graph catches → `state.error="building_timeout: ..."` → give_up → END

**Test scenarios**:
- Mock `invoke_with_timeout` 拋 `LLMTimeoutError` → graph 最終 `state.error` 以 `building_timeout:` 開頭、retry_count 未增、validation 欄為 None
- 同情境下 `state.built_nodes` 保持空 list 但**不**被 assembler 消費
- 同情境下 `_after_validate` 條件邊**不**被觸發（因 validate 從未跑）

**Security note**: N/A

#### B-TIMEOUT-02: `state.error` 分類 prefix

**Statement**: `AgentState.error` 欄位使用 `{category}: {detail}` 格式，category 為下列之一：

| category | 語意 | 來源節點 |
|---|---|---|
| `planning_failed` | planner LLM schema / logic 錯誤 | planner |
| `planning_timeout` | planner LLM 逾時 | planner |
| `building_failed` | builder LLM 其他例外 | builder |
| `building_timeout` | builder LLM 逾時（新增） | builder（B-TIMEOUT-01 抛） |
| `assembler_*` | assembler guard 錯誤 | assembler |
| `validator_*` | validator 執行錯誤（非 ValidationIssue） | validator_node |
| `deploy_failed` | n8n client 錯誤 | deployer |
| `give_up` | retry 用盡 / security block | give_up node |
| `plan_rejected` | HITL 拒絕 plan（v2） | await_plan_approval |

**Rationale**: Debug 與 observability 需能快速分流錯誤來源；frontend 依 prefix 顯示不同 UI 訊息；eval harness 依 prefix 計錯誤類型分佈。

**Affected files**:
- `backend/app/models/agent_state.py`（補 docstring 明列允許 prefix）
- `backend/app/agent/planner.py`（已有 `planning_timeout` / `planning_failed`，保留）
- `backend/app/agent/builder.py`（新增 `building_timeout`；保留 `building_failed`）
- `backend/app/agent/graph.py`（`_give_up_step` 寫 `error` 時 prefix `give_up:`；保留既有 message）
- `backend/tests/test_graph_wiring.py` 需新增 prefix 驗證

**Examples**:
- ✅ `"building_timeout: LLM exceeded 180s (stage=builder)"`
- ✅ `"give_up: validator failed after 2 retries; 3 errors"`
- ❌ `"timeout"`（無 prefix）、`"building: timed out"`（格式不符）

**Test scenarios**:
- 每個 prefix 至少 1 個 wire-level test 驗證 graph end state
- `state.error.split(":", 1)[0]` 必屬上表 category 集合

**Security note**: N/A

### Errors §B. Builder graph edge routing（v1.1-impl）

`_after_validate` 條件邊維持 v1 行為；新增 `_after_build` 條件邊（或 node-level guard）：若 `state.error` 以 `building_timeout:` 或 `building_failed:` 開頭 → 直接路由至 `give_up`，**不**進 assembler。

```python
def _after_build(state: AgentState) -> str:
    if state.error and (
        state.error.startswith("building_timeout:")
        or state.error.startswith("building_failed:")
    ):
        return "give_up"
    return "assemble"
```

這條 edge 要在 `build → assemble` 硬邊處改為 conditional；測試 `test_graph_wiring.py` 需加一例：builder raise BuilderTimeoutError 時 assembler 節點從未被呼叫（可用 spy）。

## Acceptance Criteria

- [ ] Graph 以 10 個節點 + 4 條件邊實作完成，`compile()` 後可 `invoke()` 或 `stream()`。
- [ ] HITL `run_cli(hitl=False)` 一氣跑完全流程；`hitl=True` 時在 `build_step_loop` 前正確中斷。
- [ ] `await_plan_approval` 接到 `edited_plan` 後，後續 build 步驟以新 plan 執行。
- [ ] 觸發 V-NODE-004（unknown type）時路由至 `replan`，retry_count+1。
- [ ] 觸發 V-SEC-001（blocklist）時路由至 `give_up`，不再嘗試 retry。
- [ ] Critic block 後走 fix_build，concerns 正確出現在下一輪 prompt。
- [ ] Per-step loop 只重跑出錯步驟（當 `fix_target="builder"` 且 validation.errors 全部指向同一 `node_name`）。
- [ ] Linear plan（無 condition intent）時 `connections_linker` skip LLM 走純 Python。
- [ ] `retry_count` 在 replan + fix_build 混合情境下正確累計，不超過 `MAX_RETRIES`。
- [ ] 全流程 log 包含 `stage` / `retry_count` / `current_step_idx` / `latency_ms`。
- [ ] 10 筆 D0-5 golden prompts 中 ≥ 7 筆直接 `validator.ok & critic.pass`（基線目標，後續迭代拉高）。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版：5 節點 + 1 條件邊、bulk builder |
| v2.0.0 | 2026-04-21 | 重大結構變更：per-step builder 迴圈、rule_class 分流路由、HITL plan confirm、整合 C1-7 critic 與 C1-2 v1.1 templates。棄用 `BuilderOutput`，拆為 `BuilderStepOutput` + `ConnectionsOutput` |
| v2.0.1 | 2026-04-22 | 新增 v1.1-impl 過渡條目（適用 v1 bulk builder 現行 code，v2 重寫後自動繼承）：B-TIMEOUT-01（builder timeout 必 raise 不退化空 list）、B-TIMEOUT-02（`state.error` category prefix 規範）、B-CAND-01（retriever 批次 definitions 查詢）、B-CAND-02（candidate fallback 迴圈）。對應 backend bottleneck analysis P0/P1 條目 |
