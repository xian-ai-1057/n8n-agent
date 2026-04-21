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
| Planner 產生 0 steps | validator 會抓 `V-TOP-002` 後照 class routing 處理（catalog-class → replan） |
| Per-step builder 逾時（單一步驟） | 視為該步驟失敗；`route_by_error_class` 會在下一輪 validator 看到 missing node 後 fix_build |
| `current_step_idx` 超出 plan 範圍（程式 bug） | `fix_build` 節點前置檢查若 `current_step_idx >= len(plan)` 直接 clamp；不 raise |
| HITL timeout（使用者 30min 未 confirm） | `session_id` 被 MemorySaver GC；下次 `confirm-plan` 回 404（C1-5 v2 處理） |
| Critic 逾時 | fail-open（C1-7 §Errors） |
| Deployer 失敗 | `state.error`，不 retry（同 v1） |

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
