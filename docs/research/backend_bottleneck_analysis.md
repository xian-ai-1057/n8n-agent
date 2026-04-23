# Backend Bottleneck Analysis

> 分析方法：靜態讀碼，逐檔追蹤 state 傳遞鏈，對照 graph edge 驗證路徑，檢查異常路徑結果。

## Executive Summary

1. **Builder 的 timeout → empty list → silent failure chain** 是整條 pipeline 最不 debug-friendly 的路徑：builder timeout 退化成空 list → validator 報結構錯 → fix prompt 拿空 previous output 修 → LLM 困惑。任何 LLM slow path 下使用者看到的錯誤都模糊，retry 做無效工作。

2. **Spec/code drift**：CLAUDE.md 宣稱 7-node pipeline + 3-layer RAG，但 code 只有 5-node + 2-layer。`connections_linker` 和 `critic` 完全不存在。連線正確性完全仰賴 builder LLM 同時輸出 nodes + connections，semantic 層錯誤直接 deploy 到 n8n。

3. **`chat_request_timeout_sec == llm_timeout_sec`（都 180s）**：讓 retry 在慢模型上形同虛設。第一次 timeout 後 retry 立刻超 chat budget。

---

## Node-by-Node Analysis

### `planner.py` (`plan_step`)

**功能**：user_message → RAG discovery 檢索 top-k (default 8) → 1 次 structured LLM call → 回傳 `list[StepPlan]`

**LLM calls**：1 次

**效能問題**：
- `retriever.search_discovery(user_message, k=8)` 只用原始 user_message 當 query，對多意圖需求（「從 Slack 通知觸發後存到 Google Sheets 並發 email」）單一 embedding 會把訊號平均掉，召回品質差。缺 query rewriting / multi-query。
- Prompt 沒有 char budget 保護（builder 有，planner 沒有）。若 discovery 命中 8 個長描述節點，prompt 可輕易突破模型 context。

**可靠性問題**：
- `StepPlan.candidate_node_types` 的 `min_length=1, max_length=5` 沒有驗證 candidates 必須來自 discovery_hits。Prompt rule 是純提示約束，LLM 仍可能幻覺出不存在的 type，進入 builder 的 `get_detail()` 回傳 None 後 silently 走空殼路徑。
- Prompt rule 要求 `steps[0].intent 必須是 trigger`，但 `StepPlan` model 沒對 `steps[0]` 強制 pydantic 驗。
- `except Exception as exc`（L64）吞任意 exception，但 **retry_count 不增加**，graph 繼續走到 validator 才炸，錯誤訊息最終才曝光。

**改善方向**：
- 為 planner prompt 加入 char budget 保護。
- 在 `PlannerOutput` 加 `model_validator` 強制 `candidate_node_types ⊆ discovery_hit_types`。
- Query expansion：user_message 先拆 "trigger intent" + "action intent" 分別 embed，merge 結果。

---

### `builder.py` (`build_nodes`)

**功能**：最核心也最危險的 node。plan → 每個 step 抓 `get_detail()` → 組 prompt → 1 次 structured LLM call → 全部 nodes + connections 一起輸出。

**LLM calls**：1 次（stage: `builder` fresh / `fix` retry）

**效能問題**：
- `_collect_candidates`（L33-54）對每個 step 只取第 0 個 candidate（L46），完全浪費 planner 提供的 5 個 ranked candidates。第一個無 detail 直接走空殼，不嘗試第二個。
- O(S) 次 `get_detail` 個別 Chroma 查詢，沒 batch。`VectorStore.get_by_ids()` 支援 list，可一次取完。
- Prompt 截斷邏輯（L130-149）：`keep = max(1, keep // 2)` 從**尾部砍** definitions。Tail definitions 不等於「最不重要」，可能正是 output 節點定義。複雜 workflow (>10 nodes) 在此 silently 降級品質，無任何訊號。
- 整個 workflow（全部 nodes + connections）塞進一個 LLM call。>15 節點時 JSON schema output 退化率非線性上升。

**可靠性問題（P0 級）**：

**最嚴重 silent failure**（L157-165）：
```python
except LLMTimeoutError as exc:
    return {"built_nodes": [], "connections": [], ...}  # 空 list 不是 error！
```
→ graph 繼續走 assembler → validator 報 V-TOP-002（empty nodes）+ V-TRIG-001 → 進 fix_build。
→ **fix prompt 是基於 `state.built_nodes`（空）+ errors 來寫的**，LLM 被要求「修上次 output」但 previous 根本是空 — 完全偏離修正語意。

**`is_retry` 判斷邏輯**（L111）：`state.built_nodes` truthy 檢查在 timeout 回傳空 list 後變 False → 走 builder prompt 而非 fix prompt → 放棄「修上次」的語意。

**Connection 名稱漂移**：`BuilderOutput.connections` schema 只驗 source_name/target_name 是 string，沒驗 names 對得上同批 nodes 的 name set。跨欄位錯誤一路搬到 validator 才發現，多跑一輪 retry。

**改善方向**：
- **P0 優先**：Timeout 應 raise 到 graph 層走 give_up，不退化成空 list。
- `_collect_candidates` 改用 `retriever.get_by_ids(all_chosen_types)` 一次性 batch 查詢。
- 實作「failed candidate fallback」：first candidate 無 detail → 試第二個 candidate。
- `BuilderOutput` 加 `model_validator(mode="after")`：connection endpoint names 必須在 nodes name set 中。

---

### `connections_linker`（**不存在**）

CLAUDE.md 宣稱的 7-node pipeline 有此節點，但 `graph.py` 沒有。Connection 是 builder 同一次 LLM call 的輸出，這是多節點 workflow 最容易出錯之處（LLM 常把 source_name 寫成 node type 或 id）。

**影響**：任何含 AI Agent / LangChain nodes 的 workflow，connection type 必然錯誤（應用 `ai_languageModel`/`ai_tool`，builder 不知道）。

**改善方向**：補一個 deterministic connections_linker（根據 plan 順序線性串接 + 特殊節點特殊處理），可大幅減少 V-CONN-* 類錯誤。

---

### `assembler.py` (`assemble_step`)

**功能**：純函數，無 LLM。給 node 補 uuid / 排 x-y position / 取 workflow name / 設 settings。

**LLM calls**：0

**效能問題**：`_assign_positions` 最壞 O(N²)，N<30 可忽略。

**可靠性問題**：
- 若 builder 回傳空 list，assembler 仍產出空 `WorkflowDraft` → validator 炸 V-TOP-002 + V-TRIG-001，但不明確指出「上游 builder 回空」。
- `_assign_positions` 對「多個 incoming edges 的節點」不 propagate y-offset（L84 `if len(sources) == 1`），merge-back 節點 y 會重疊。視覺 bug，非功能 bug。

**改善方向**：開頭加 guard：`built_nodes` 為空且 `state.error` 為 None → 設明確 `state.error = "assembler_received_empty_nodes"`，讓 graph 早結束。

---

### `validator.py` + `validator_node.py` (`validate_step`)

**功能**：19 條 deterministic rule（V-TOP / V-NODE / V-CONN / V-TRIG）。無 LLM。

**LLM calls**：0

**效能問題**：
- `validator_node.validate_step` 每次呼叫都 `WorkflowValidator()` 新建（L24）→ 每次 retry 都重讀 `catalog_discovery.json`（~500 entries）。應 module 層 cache 一個 instance。

**可靠性問題**：
- V-NODE-004 的 `known_types` 從 `catalog_discovery.json` 讀，但 catalog 和 Chroma 可能不同步（只跑 ingest_discovery 未跑 ingest_detailed）。Validator 說 OK 但 builder 根本抓不到 detail。
- V-CONN-004/005 只發 warning，孤立節點不擋 deploy。唯一 output node 忘接入，使用者在 n8n UI 才發現。

**改善方向**：
- Validator 實例化 module-level singleton cache。
- `known_types` 應從 Chroma discovery collection 動態讀，避免 catalog/Chroma drift。
- 新增 V-CONN-006：non-trigger node 若 inbound+outbound 都空，升級為 error。

---

### `critic`（**不存在**）

CLAUDE.md 宣稱的 critic node 缺席。`placeholder` 值（`url="TODO"`、`url=""`）完全不被攔截，直接 deploy 到 n8n，使用者拿到跑不起來的 workflow。這是使用者回報 builder 失敗的常見症狀。

**改善方向**：短期：至少把 placeholder 偵測（TODO/FIXME/<fill_in>/xxx/your-api-key regex）加為 V-PARAM-009，不等 Critic 實作。長期：在 validator 之後 deploy 之前插入 critic，1 次 LLM call（temperature=0）。

---

### `deployer.py` (`deploy_step`)

**功能**：POST draft 到 n8n。無 API key → dry-run。

**LLM calls**：0

**可靠性問題**：
- 只 catch `N8nApiError`，HTTP 層其他異常（`httpx.ConnectError`）逃逸到 LangGraph → HTTP 500，難以 debug。
- 沒有 retry（n8n 短暫 5xx 直接認定失敗）。

**改善方向**：加 httpx 層 retry（最多 2 次，指數 backoff）；明確 catch `httpx.HTTPError` 轉 `N8nUnavailable`。

---

## RAG 設計分析

### 實際是 2 層，不是 3 層

CLAUDE.md 說「3 層 RAG」，但只有：
- `catalog_discovery`：500+ 全量 node discovery（embedding search，planner 用）
- `catalog_detailed`：~30 node 詳細 parameter schema（exact ID query，builder 用）

第 3 層（templates / few-shot examples）不存在。

### 雙層分工邏輯（本身設計正確）

- Planner 廣搜（k=8 embedding similarity）→ LLM 選 type
- Builder 定向取（exact ID query，不用 embedding）→ 避免 wrong-param 幻覺

### 關鍵問題

- 約 30 個 curated detailed node 之外的節點，builder 必然走空殼路徑（`parameters={}`）。即使 planner 選到正確 type，使用者也得自己補參數。
- Planner 把 hits 轉純字串時（`format_discovery_hits`）**丟掉了 `has_detail` flag**。LLM 不知道哪個 candidate 有完整 schema，哪個是空殼候選。
- Embedding 文件格式是 `display_name\n類別: {cat}\n{desc}\n關鍵字: {kw}`（英文混中文），對使用者的中文 query 召回品質仰賴 embedding model 泛化能力（BGE-m3 尚可，其他模型可能急劇下滑）。
- `_FilesystemStubRetriever.search_discovery` 用 token overlap scoring（粗糙），測試環境行為與 production 背離。

---

## Graph 架構分析

### 實際 edge 結構

```
START → plan → build → assemble → validate ─┬→ deploy → END
                ↑                             ├→ fix_build → assemble (loop)
                └─────────────────────────────│
                                              └→ give_up → END
```

- `plan → build`：硬邊，無條件。Planner error → build 仍被呼叫。
- `build → assemble`：硬邊。Builder timeout 空 list → assemble 仍跑（silent failure 起點）。
- `agent_max_retries=2` 保護 fix 迴圈（OK）。

### State 管理

`AgentState` 是單一大 pydantic model，每次 delta 手動 append messages（`state.messages + [...]`）。若有人在 delta 裡只 set 部分欄位忘了 `+ state.messages`，會 silently 清空歷史訊息。

建議用 LangGraph `Annotated[list, add_messages]` reducer，讓 append type-safe。

### 效能問題

每次 `/chat` 都：
1. `get_retriever()` 重建 embedder + 重連 Chroma
2. `build_graph()` 重 compile LangGraph graph
3. `get_llm(schema, stage=...)` 每次 new 一個 ChatOpenAI/ChatOllama

在低流量場景可忽略，在多併發或 long-running server 下會累積成可觀 p99 latency。
建議 app startup 建一次，inject 到 graph（已有 `retriever` 參數，但 routes 沒用）。

---

## Config 分析

### 問題

| 問題 | 影響 |
|------|------|
| `chat_request_timeout_sec == llm_timeout_sec == 180s` | 第一次 LLM timeout 後 retry 立刻超 chat budget，`max_retries=2` 形同虛設 |
| `builder_prompt_char_budget=12000`（字元而非 token） | 對中文 ~4-6k tokens，OK for 32k ctx model；換 8k model 就爆 |
| `rag_detailed_k=3` 幾乎不被用 | `get_detail()` 不吃 k；只有 `search_detailed` fallback 才用 |
| 無 `critic_enabled` / `connections_linker_enabled` flag | 未來補實作時 config 層沒預留開關 |

### 耦合問題

- `validator.py` L151 反向 import `n8n.client.READ_ONLY_TOP_LEVEL_FIELDS`（agent 層→n8n 層，local import 繞過循環）
- `routes.py` 直接 import `run_cli`（CLI helper 當 production entry point）

---

## 優先改善項目

| 優先級 | 問題 | 影響 | 改善方向 |
|--------|------|------|----------|
| **P0** | Builder timeout 退化成空 list → silent failure → fix prompt 修空 | 任何 LLM slow path 下 retry 做無效工作 | Timeout raise 到 graph，走 give_up；fix prompt 只在 built_nodes 非空時啟用 |
| **P0** | `connections_linker` / `critic` 不存在，CLAUDE.md 說有 | AI Agent workflow 100% 失敗；placeholder 直接 deploy | 決策：補實作或更新 spec；短期先補 V-PARAM-009 |
| **P0** | `chat_timeout == llm_timeout`，retry 超 budget | max_retries=2 形同虛設 | chat budget ≥ 3× per-call（e.g. 240/60） |
| **P1** | `_collect_candidates` O(S) 個別 Chroma 查詢 | 15 節點 workflow 多 ~500ms | `get_by_ids(all_types)` batch 查詢 |
| **P1** | Builder 尾部截斷 definitions 無訊號 | Workflow 後段節點 silently 降級為空殼 | 依 intent 權重保留；log `defs_trimmed` event |
| **P1** | Planner candidates 不驗是否在 discovery，builder 不 fallback 次選 | 幻覺 type → 空殼節點，使用者看不出哪裡錯 | PlannerOutput model_validator；builder fallback 試第 2 candidate |
| **P2** | Validator 每次 retry 重讀 catalog JSON | 無謂 I/O | Module-level singleton |
| **P2** | 每次 /chat 重建 retriever / graph / embedder | 高流量下 p99 latency | App startup 建一次並 inject |
| **P2** | `BuilderOutput` 不驗 connection endpoints 對應 nodes | 跨欄位錯誤拖到 validator 才發現 | 加 `model_validator(mode="after")` |
| **P3** | Planner `has_detail` flag 未傳給 LLM | 容易推薦無 detail 的 type | `format_discovery_hits` 加標記 |
| **P3** | Validator 跨層 import n8n.client constants | 架構耦合 | 提取 `app.n8n.constants` |
