# C1-1：Agent Graph（LangGraph）

> **版本**: v2.0.4 ｜ **狀態**: Draft ｜ **前置**: D0-1, D0-2, D0-3 v1.1, C1-2 v1.1, C1-4 v1.1, C1-7, C1-8 ｜ **Prompts**: R2-3

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
      │ completeness_check│              │  (v1.1-impl 新增；fix_build 不經過此節點)
      └────┬──────────────┘              │
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
- `build_step_loop → {build_step_loop, completeness_check}`（self-loop until `current_step_idx == len(plan)`；v1.1-impl 改為落到 completeness_check 而非直接 connections_linker）
- `completeness_check → connections_linker`（硬邊；見 B-COMP-01）
- `validator → {critic, fix_build, replan, give_up}`（由 `route_by_error_class` 決定）
- `critic → {deploy, fix_build, give_up}`
- `replan → await_plan_approval`（重新規劃後再請使用者確認一次）
- `fix_build → assembler`（**不** 經過 completeness_check；見 B-COMP-01）

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

#### 2.4a completeness_check（v1.1-impl，新增）

| 項目 | 內容 |
|---|---|
| 讀 | `plan`, `candidates`, `built_nodes` |
| 寫 | `built_nodes`（可能 append 一或多個 skeleton node）, `messages` |
| LLM | 否（純 Python + 批次 RAG 查詢） |
| 流程 | 1) 比對 `plan[*].step_id` vs `built_nodes[*].step_id`，找出缺漏的 step。2) 對缺漏 step 呼叫 `retriever.get_definitions_by_types([chosen_type])`（B-CAND-01 批次 API）取得 `NodeDefinition`。3) 為每個缺漏 step 注入 skeleton `BuiltNode`。4) 每注入一個 skeleton 寫一筆 `messages` diagnostic。 |

本節點存在意義：Builder LLM 在 per-step loop 前的 v1 bulk 模式下（或 v2 per-step 偶發失誤）可能「少生成」某個 step 對應的節點，使 `built_nodes` 數量 < `plan` 數量。缺漏在 assembler / validator 階段不會直接被捕捉（validator 僅檢查現有 node 的結構與拓樸），而是以「功能不完整」的形式流到 critic 或最終交付；本節點以結構對齊的方式把「計畫但未建」的缺口填上 skeleton，讓後續 validator 能以 V-PARAM-* 規則抓到缺參數，轉為明確的 fix_build 路徑。

新增節點於 `build → connections_linker` 之間插入；**fix_build 路徑不經過** completeness_check（fix loop 是在 `built_nodes` 已成形後做 surgical 修正，不應再注入 skeleton）。

詳細規則見下方 B-COMP-01 ~ B-COMP-05。

##### B-COMP-01: completeness_check 節點必須在 build 與 assemble 之間執行

**Statement**: Graph 必須在 `build` 節點與 `assemble` 節點之間插入新節點 `completeness_check`。執行順序為 `plan → build → completeness_check → assemble → validate → deploy`。`fix_build → assemble` 硬邊保持不變，**不**經過 completeness_check。

**Rationale**: completeness_check 的語意是「第一次 build 完成後的結構對齊」。fix_build 的輸入已是「previously built + validator errors」，再注入 skeleton 會與 fix 的修補語意衝突，讓 fix prompt 誤把自己產出的 node 當成 skeleton 處理。

**Affected files**:
- `backend/app/agent/graph.py`（`build_graph` 新增 `add_node("completeness_check", ...)`；將 `build → assemble` 邊改為 `build → completeness_check → assemble`；`_after_build` 條件邊的 ok 分支 target 從 `"assemble"` 改為 `"completeness_check"`；`fix_build → assemble` 不變）
- `backend/app/agent/completeness.py`（新檔，`completeness_check_step(state, retriever) -> dict`；工廠 `_make_completeness_check_node(retriever)`）

**Function signature**:
```python
# backend/app/agent/completeness.py
def completeness_check_step(
    state: AgentState, retriever: RetrieverProtocol
) -> dict[str, Any]:
    """Inject skeleton BuiltNode for plan steps missing from built_nodes.

    Returns a delta dict with (at most) updated built_nodes and messages.
    If no steps are missing, returns {} (no-op; fast path).
    """
```

**Examples**:
- ✅ `plan=[s1,s2,s3]`、`built_nodes=[bn(step_id=s1), bn(step_id=s2), bn(step_id=s3)]` → no-op，回傳 `{}`
- ✅ `plan=[s1,s2,s3]`、`built_nodes=[bn(step_id=s1), bn(step_id=s3)]` → 為 s2 注入 1 個 skeleton,`built_nodes` 最終 3 個
- ❌ 不允許：建圖時忘了把 `_after_build` 的 `"assemble"` target 改到 `"completeness_check"`,導致節點被跳過

**Test scenarios**: 見 B-COMP-05。

**Security note**: N/A

##### B-COMP-02: BuiltNode 與 BuilderStepOutput 新增 step_id 欄位

**Statement**: `BuiltNode` 必須新增可選欄位 `step_id: str | None = None`。此欄位 **不得** 序列化到 n8n workflow JSON（n8n 端不認此欄位）；assembler 在 emit n8n JSON 時必須 drop 它。builder prompt 輸出 schema (`BuilderOutput` / `BuilderStepOutput`) 必須要求 LLM 在每個 node 輸出對應的 `step_id`。若 LLM 未輸出 step_id（欄位為 None），completeness_check 視為 **無法對齊**，該 node 不被視為涵蓋任何 step（等同全部 step 缺失）。

**Rationale**: 現有 builder 回傳的 `BuiltNode` 沒有回指對應的 plan step,只能靠「位置 index 一一對應」做對齊,遇到 LLM 少生成或順序錯亂即失準。`step_id` 為穩定 key,比 `name`（由 assembler 後處理）更早可用。

**Affected files**:
- `backend/app/models/workflow.py`（`BuiltNode` 加 `step_id: str | None = None`，並於 `model_config` 確認 `populate_by_name=True`）
- `backend/app/agent/builder.py`（`BuilderOutput` / `BuilderStepOutput` schema 加 `step_id`；prompt 新增要求「每個 node 輸出 step_id，值須等於對應 StepPlan.step_id」）
- `backend/app/agent/prompts/builder.md`（或等效 per-step prompt）加 few-shot 展示 step_id 輸出
- `backend/app/agent/assembler.py`（emit n8n JSON 時使用 `model_dump(exclude={"step_id"})` 或等效方式避免外洩）

**Function signature**:
```python
class BuiltNode(BaseModel):
    # ... existing fields
    step_id: str | None = Field(
        default=None,
        description=(
            "Back-reference to StepPlan.step_id that this node implements. "
            "Internal-only; not serialised to n8n wire format."
        ),
    )
```

**Examples**:
- ✅ LLM 輸出 `{"name": "HTTP Request", "type": "...", "step_id": "step_2", ...}` → `BuiltNode.step_id="step_2"`
- ✅ assembler emit n8n JSON：`{"name": "HTTP Request", "type": "...", "typeVersion": 1, ...}`（無 step_id 欄位）
- ❌ assembler 若把 step_id 寫入 workflow JSON → n8n 端部署可能噴不認識的欄位（視版本而定）

**Test scenarios**: 見 B-COMP-05。

**Security note**: N/A。但：step_id 從 LLM 輸出回收，不得直接 trust 作為檔案路徑 / URL；在本 spec 語境僅做字串比對,OK。

##### B-COMP-03: Skeleton 注入規則

**Statement**: 對每個 `plan[i].step_id` 不存在於任何 `built_nodes[*].step_id` 的 step，completeness_check 必須注入一個 skeleton `BuiltNode`，欄位規則為：

| 欄位 | 值 |
|---|---|
| `step_id` | `plan[i].step_id` |
| `type` | 對應 `candidates[*]` 中 `step_id==plan[i].step_id` 的 `chosen_type` |
| `type_version` | `candidate.definition.type_version` 若 `definition is not None`；否則 `1.0`（float） |
| `name` | `f"Missing step {step_id}"`（assembler 後續會統一重命名；此值僅為 placeholder 避免 validator name 衝突） |
| `parameters` | `{"_completeness_injected": "TODO: fill required parameters for this node"}` |
| `position` | `[0.0, 0.0]`（assembler 會重算 layout） |
| 其他欄位 | 全部使用 model 預設值（None 或 default factory） |

若 `candidates` 中找不到對應 `step_id`（planner / builder 間不一致），視為**無法注入**，log WARNING 後 **skip 該 step**，不中斷流程；最後 `built_nodes` 數量仍可能 < `plan` 數量，讓 validator 自然抓到拓樸錯誤並走 fix_build。

每注入一個 skeleton，`state.messages` append 一筆：
```python
{"role": "completeness", "content": f"injected skeleton for missing step {step_id} (type={chosen_type})"}
```

若 skip，append：
```python
{"role": "completeness", "content": f"skip missing step {step_id}: no matching candidate"}
```

**Rationale**:
- `_completeness_injected` 參數讓 validator V-PARAM-* 幾乎必抓到錯（required 欄位缺失）→ 明確觸發 fix_build，比悄悄放空 dict 更能暴露問題。
- `type_version` fallback 到 `1.0` 與 builder 現行處理無 definition 時的行為一致。
- `name` 使用明顯的 "Missing step ..." 便於 debug log。

**Affected files**:
- `backend/app/agent/completeness.py`（實作注入邏輯）

**Function signature**:
```python
def _build_skeleton(
    step: StepPlan, candidate: NodeCandidate | None
) -> BuiltNode | None:
    """Return None iff candidate is None (→ skip logic per B-COMP-03)."""
```

**Examples**:
- ✅ Missing s2, candidate.chosen_type="n8n-nodes-base.set", definition.type_version=3.4
  → skeleton: `{step_id:"s2", name:"Missing step s2", type:"n8n-nodes-base.set", typeVersion:3.4, position:[0,0], parameters:{"_completeness_injected":"TODO: fill required parameters for this node"}}`
- ✅ Missing s2, candidate.chosen_type 存在但 definition is None
  → skeleton: `{..., typeVersion:1.0, ...}`
- ✅ Missing s2, 無對應 candidate → skeleton=None,skip 並寫 diagnostic message

**Test scenarios**: 見 B-COMP-05。

**Security note**: `_completeness_injected` 是保留 key，**不得**與真實 n8n parameter key 衝突（底線前綴 + 明確命名降低碰撞風險）。validator 端無需特別處理此 key，讓它照常觸發 required-param 錯誤即可。

##### B-COMP-04: RAG 查不到時 graceful skip

**Statement**: completeness_check 在對缺漏 step 查 `retriever.get_definitions_by_types(types)` 時，若 batch 回傳值對應 key 為 None（RAG 無此 type 的 detail），**不得**中斷或 raise；須退到 `definition=None` 路徑（依 B-COMP-03 使用 `type_version=1.0`），並寫一筆 `messages` diagnostic：
```python
{"role": "completeness", "content": f"no RAG detail for type {chosen_type} (step {step_id}); injecting with typeVersion=1.0"}
```

**Rationale**: detailed catalog 覆蓋率 ~30/529；若 chosen_type 不在已收錄範圍，仍應完成結構對齊,不要讓整個 completeness_check 失敗掉整條 pipeline。

**Affected files**:
- `backend/app/agent/completeness.py`

**Examples**:
- ✅ `retriever.get_definitions_by_types(["a.unknown"])` → `{"a.unknown": None}` → skeleton 以 type_version=1.0 注入 + diagnostic
- ❌ 不允許：raise、或直接 skip 該 step（skip 僅在無 candidate 時，B-COMP-03）

**Test scenarios**: 見 B-COMP-05。

**Security note**: N/A

##### B-COMP-05: Test scenarios（集中列出）

test-engineer 需實作以下測試（pytest），所有檔案放在 `backend/tests/`：

1. **test_completeness_noop**：`plan=[s1,s2]`, `built_nodes` 兩個 node 的 `step_id` 完整對齊 → `completeness_check_step` 回傳 `{}`，`built_nodes` 長度不變，無 completeness 角色 message。
2. **test_completeness_single_missing**：`plan=[s1,s2,s3]`, `built_nodes` 缺 s2（只有 s1、s3）→ 回傳 delta 中 `built_nodes` 長度 3；s2 位置的 skeleton `type_version==definition.type_version`；`parameters` 含 `_completeness_injected` key；有 1 筆 `role="completeness"` message。
3. **test_completeness_multiple_missing**：`plan=[s1..s4]`, `built_nodes` 只有 s1 → 3 個 skeleton 被注入；messages 有 3 筆 completeness diagnostic。
4. **test_completeness_rag_miss**：缺 s2，retriever mock `get_definitions_by_types` 回 `{"x.unknown": None}` → skeleton `type_version==1.0`；有額外一筆 "no RAG detail" diagnostic。
5. **test_completeness_no_candidate**：缺 s2,但 `state.candidates` 中找不到 step_id=s2 的 candidate → 不注入 skeleton；有一筆 "skip missing step s2" diagnostic；`built_nodes` 長度仍少於 plan（後續由 validator 自然抓）。
6. **test_completeness_builtnode_without_step_id**：`built_nodes` 中某個 node `step_id is None`（模擬 LLM 未輸出）→ 此 node 不涵蓋任何 step；completeness 把所有 plan step 都視為 missing 並嘗試注入。
7. **test_graph_wiring_completeness_inserted**：build_graph 後 inspect node list 含 `completeness_check`；`build` 節點成功後的下一個節點是 `completeness_check`（非 `assemble`）；`fix_build` 的出邊仍為 `assemble`（不經 completeness_check）。
8. **test_assembler_drops_step_id**：`BuiltNode(step_id="s1", ...)` 經 assembler 後產出的 workflow JSON dict **不包含** `step_id` key。

Eval harness 另加 1 條 prompt case（`tests/eval/prompts.yaml`）：輸入會觸發 ≥4 step 的 plan，斷言最終 `len(workflow.nodes) == len(state.plan)`；即使 LLM 少生成也能因 completeness_check 補齊。

**Security note**: N/A



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
| `completeness_failed` | completeness_check 無法完成結構對齊（罕見） | completeness_check（Errors §C） |
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

**v1.1-impl 補充（B-COMP-01 上線後）**：`_after_build` 條件邊的 ok 分支 target 從 `"assemble"` 改為 `"completeness_check"`。timeout/failed 仍直接 `"give_up"`，不經 completeness_check。對應測試 `test_graph_wiring.py` 需：(a) 新增驗證 `build` ok → `completeness_check` 路徑；(b) 保持 `building_timeout` / `building_failed` 下 completeness_check 節點從未被呼叫。

### Errors §C. completeness_check 節點錯誤（v1.1-impl）

completeness_check 的設計目標之一就是 **graceful**。以下為明確規範：

| 場景 | 行為 |
|---|---|
| RAG (`get_definitions_by_types`) raise 任意例外 | catch 後把該 batch 視為全部 None；繼續流程；`messages` append diagnostic。**不**寫 `state.error`。 |
| `plan` 為 None 或空 list | 立即 no-op 回傳 `{}`；不報錯。 |
| `candidates` 為 None 或空 list | 無法為任何 missing step 注入；每個 missing step 走 skip 路徑（B-COMP-03）。 |
| `BuiltNode.step_id=None`（builder 未輸出） | 該 node 視為不涵蓋任何 step；其他 step 照常比對/注入。 |
| 同一 step_id 在 `built_nodes` 出現 >1 次 | 視為已涵蓋（不注入 skeleton）；不視為錯誤。 |
| 注入 skeleton 時 Pydantic 驗證失敗（理論上不應發生） | raise `RuntimeError`，寫 `state.error="completeness_failed: {detail}"`；graph 路由至 give_up（需在 graph.py 加 try/except 並有對應條件邊或 node-level guard）。 |

新增 `state.error` prefix：`completeness_failed`（補入 B-TIMEOUT-02 表）。

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
- [ ] `completeness_check` 節點在 `build → connections_linker/assemble` 之間被呼叫一次；`fix_build` 路徑繞過它（B-COMP-01）。
- [ ] `BuiltNode.step_id` 欄位不出現於最終 n8n workflow JSON（B-COMP-02）。
- [ ] 當 builder 少生成一個 step 對應的節點，completeness_check 成功注入 skeleton，最終 `len(built_nodes) == len(plan)`（B-COMP-03）。
- [ ] RAG 對 missing step 的 type 查不到 detail 時，skeleton 以 `type_version=1.0` 注入，流程不中斷（B-COMP-04）。

### Errors §D. HITL shipping(v2.0.3 — 把 v2.0.2 已 spec 但未 impl 的 HITL 落地)

C1-1 v2.0.2 §2.2 / §5 / §8 已完整描述 HITL 行為(`await_plan_approval` + `MemorySaver` + `interrupt_before=["build_step_loop"]`),但目前 graph.py 尚未實作。`C1-9` chat layer 的 `confirm_plan` tool 直接依賴 HITL 路徑;以下兩條為「必須 ship」的硬性 acceptance entries。

#### C1-1:HITL-SHIP-01: `MemorySaver` + `interrupt_before` 落地

**Statement**: `build_graph(retriever, *, deploy_enabled=True, hitl_enabled=True)` 必須:(a) 當 `hitl_enabled=True` 時 instantiate `MemorySaver()` 作為 checkpointer(必須是 process-wide singleton,resume 才找得到 session);(b) compile 時帶 `interrupt_before=["await_plan_approval"]`(見下方 anchor 說明);(c) `hitl_enabled=False`(`run_cli` 路徑)時 checkpointer=None、不 interrupt。同時 export 兩個新 helper:`run_graph_until_interrupt(session_id, user_message) -> AgentState`(跑到 await_plan_approval 中斷,回中斷時 state 快照)與 `resume_graph_with_confirmation(session_id, approved, edited_plan=None) -> AgentState`(從 checkpoint resume 跑到 END)。

**Interrupt anchor 澄清**(reconcile 自實作 review):本檔 v2.0.0 §8 範例寫的是 `interrupt_before=["build_step_loop"]`,但實際落地以 `interrupt_before=["await_plan_approval"]` 為準,理由:
1. `await_plan_approval` 是 §1 圖中明確的 HITL gate node;
2. 使用者決策必須在 gate node *執行前* 注入(`update_state` 寫 `plan_approved`),才能讓 gate 後的條件邊正確路由;
3. 當前 code 仍是 v1 bulk `build` 節點(命名為 `build` 而非 `build_step_loop`),anchor 落在 gate 才能跨 v1/v2 builder 一致繼承 HITL wiring。

語意上等價:使用者必先確認才會跑到任何 builder 工作。當 builder 從 v1 bulk 升級到 v2 per-step 時,本 anchor 不需異動(`build_step_loop` 在 gate 之後)。

**Rationale**: HITL 的 spec 已存在但未 ship,導致 chat layer(C1-9)無法落地 confirm_plan tool。落地此條 = 解鎖整個 chat-first pipeline。

**Affected files**:
- `backend/app/agent/graph.py`(build_graph 補 checkpointer + interrupt_before;新 helper 兩個)
- `backend/app/models/agent_state.py`(見 HITL-SHIP-02)

**Function signature**:
```python
# C1-1:HITL-SHIP-01
from langgraph.checkpoint.memory import MemorySaver

def build_graph(
    retriever, *, deploy_enabled: bool = True, hitl_enabled: bool = True
) -> CompiledGraph:
    # ... existing nodes/edges ...
    # C1-1:HITL-SHIP-01 — anchor interrupt on await_plan_approval per §interrupt anchor 澄清
    checkpointer = _get_hitl_checkpointer() if hitl_enabled else None  # process-wide singleton
    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["await_plan_approval"] if hitl_enabled else [],
    )

def run_graph_until_interrupt(
    session_id: str, user_message: str, *, retriever=None
) -> AgentState: ...

def resume_graph_with_confirmation(
    session_id: str, approved: bool, edited_plan: list[StepPlan] | None = None
) -> AgentState: ...
```

**Examples**:
- ✅ `run_graph_until_interrupt("sess_xxx", "...")` → 回 AgentState 含 plan,plan_approved=False
- ✅ `resume_graph_with_confirmation("sess_xxx", True)` → 跑完 END,workflow_url 填
- ✅ `resume_graph_with_confirmation("sess_xxx", False)` → state.error="plan_rejected"
- ❌ resume 不存在 session_id → raise `SessionNotFound`(C1-5 404 處理)
- ⚠️ Stage mismatch(graph 快照在非 await_plan_approval 中斷點):**v1 acceptance** — `resume_graph_with_confirmation` 不主動偵測;由 endpoint 層的 `except Exception` fallback 撐住(回 500 而非 409)。**Follow-up**:後續若有 SSE / 多 client 同時 confirm 的競態場景,須加 graph 層 stage probe 並 raise `SessionStageMismatch`,endpoint 對應回 409。本 v1 實作不阻擋 ship。

**Test scenarios**:
- `test_hitl_graph_interrupts_at_await_plan_approval`:hitl_enabled=True,run_graph_until_interrupt 後 graph 暫停,plan 已 populated
- `test_hitl_graph_resume_with_approval_completes`:resume(approved=True) 跑到 END
- `test_hitl_graph_resume_with_rejection_sets_error`:resume(approved=False) → state.error 含 "plan_rejected"
- `test_hitl_graph_resume_with_edited_plan_uses_new_plan`:edited_plan 帶入,後續 build 用新 plan
- `test_run_cli_unchanged_with_hitl_disabled`:既有 CLI 測試全綠,hitl_enabled=False 直接跑完
- `test_hitl_resume_unknown_session_raises`:resume 不存在 sid → SessionNotFound

**Security note**: `session_id` 來自 chat layer / API,sanitize 由 C1-9:CHAT-SEC-01 與 C1-5 既有 pattern validation 處理。

#### C1-1:HITL-SHIP-02: `AgentState` 新增 `session_id` 與 `plan_approved` 欄位

**Statement**: `AgentState` 補兩個欄位:`session_id: str | None = None`(僅 HITL 模式有值)與 `plan_approved: bool = False`(由 `await_plan_approval` 節點寫入)。同時新增 `await_plan_approval` 節點的 stub function `await_plan_approval_step(state) -> dict`(在 hitl_enabled=False 時直接回 `{"plan_approved": True}`,hitl_enabled=True 時 graph compile 帶 interrupt_before 故此函式只在 resume 後跑一次,讀取已被 resume payload 寫入的 plan_approved)。

**Rationale**: HITL state 需 thread 全程持續,且 resume 路徑需要區分「使用者已決定」與「graph 還沒問」。

**Affected files**:
- `backend/app/models/agent_state.py`(加欄位)
- `backend/app/agent/graph.py`(node factory 與 conditional edge `await_plan_approval → {build_step_loop, give_up}`)
- `backend/tests/test_graph_wiring.py`(新增 wiring 驗證)

**Function signature**:
```python
# C1-1:HITL-SHIP-02
class AgentState(BaseModel):
    # ... 既有欄位 ...
    session_id: str | None = Field(
        default=None,
        description="LangGraph thread id, also used as chat session id (C1-9).",
    )
    plan_approved: bool = Field(
        default=False,
        description="Set True by await_plan_approval node after user confirms (or hitl=False).",
    )

def await_plan_approval_step(state: AgentState) -> dict[str, Any]: ...
```

**Examples**:
- ✅ hitl_enabled=False → `await_plan_approval_step` 立即回 `{"plan_approved": True}`
- ✅ hitl_enabled=True 且 graph 已 resume 帶 `{"plan_approved": True}` → 節點 read state 後直接放行
- ✅ `state.error="plan_rejected"` 由 `_after_plan_approval` 條件邊路由至 give_up 而非 build_step_loop

**Test scenarios**:
- `test_agent_state_session_id_default_none`
- `test_agent_state_plan_approved_default_false`
- `test_await_plan_approval_skips_when_hitl_disabled`
- `test_await_plan_approval_routes_give_up_on_rejection`
- `test_graph_wiring_has_await_plan_approval_node`

**Security note**: N/A(欄位本身無 security 影響;session_id 內容驗證見 C1-9:CHAT-SEC-01)。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版：5 節點 + 1 條件邊、bulk builder |
| v2.0.0 | 2026-04-21 | 重大結構變更：per-step builder 迴圈、rule_class 分流路由、HITL plan confirm、整合 C1-7 critic 與 C1-2 v1.1 templates。棄用 `BuilderOutput`，拆為 `BuilderStepOutput` + `ConnectionsOutput` |
| v2.0.1 | 2026-04-22 | 新增 v1.1-impl 過渡條目（適用 v1 bulk builder 現行 code，v2 重寫後自動繼承）：B-TIMEOUT-01（builder timeout 必 raise 不退化空 list）、B-TIMEOUT-02（`state.error` category prefix 規範）、B-CAND-01（retriever 批次 definitions 查詢）、B-CAND-02（candidate fallback 迴圈）。對應 backend bottleneck analysis P0/P1 條目 |
| v2.0.2 | 2026-04-24 | 新增 completeness_check 節點（v1.1-impl）：B-COMP-01（插入點與 fix_build 繞過）、B-COMP-02（`BuiltNode.step_id` 欄位）、B-COMP-03（skeleton 注入規則）、B-COMP-04（RAG miss graceful skip）、B-COMP-05（集中測試清單）。圖結構與條件邊、Errors §C、Acceptance Criteria 同步更新。`state.error` prefix 表新增 `completeness_failed` |
| v2.0.3 | 2026-04-25 | 新增 HITL shipping 條目(對應 C1-9 chat layer 依賴):HITL-SHIP-01(MemorySaver + interrupt_before + run_graph_until_interrupt / resume_graph_with_confirmation helpers)、HITL-SHIP-02(`AgentState` 補 `session_id` / `plan_approved` 欄位 + `await_plan_approval_step` 節點)。落地 v2.0.0 §2.2/§5/§8 已 spec 但未 impl 的 HITL 路徑 |
| v2.0.4 | 2026-04-25 | reconcile 落地細節:HITL-SHIP-01 Statement / 範例 / Acceptance 同步成 `interrupt_before=["await_plan_approval"]`(取代原 §8 範例 `["build_step_loop"]`),澄清 anchor 在 gate node 的理由(跨 v1 bulk / v2 per-step builder 命名都成立);新增 stage-mismatch v1 verdict(由 endpoint fallback 撐住,409 留待 follow-up);MemorySaver 改稱 process-wide singleton |
