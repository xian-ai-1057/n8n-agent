# Spec Gap Report

> 分析範圍：`docs/L1-components/C1-1 ~ C1-8` vs `backend/app/agent/`、`backend/app/rag/`、`backend/app/api/`、`backend/tests/`

## Executive Summary

1. **Spec ↔ code drift 是災難性的**。Spec 已推進到 v2（C1-1、C1-5、C1-6）和 v1.1（C1-2、C1-4），新增了 Critic（C1-7）、Security（C1-8）、HITL plan confirmation、per-step builder loop、connections_linker、SSE streaming、V-PARAM-*/V-SEC-* 規則等。**這些全部沒有實作。** Code 仍停在 v1.0 baseline。任何工程師讀 spec 會以為這些功能存在，在不存在的基礎上設計下游改動。

2. **Traceability ID 從未被實作**。全 backend 樹中 `grep -rn "# C1-"` 零命中。CLAUDE.md 的「engineer 加 `# C1-x:ID` 標注」規則一直被忽略，目前無任何機械化手段追蹤 spec-code 對應關係。

3. **Builder 在大型 / AI Agent workflow 上的失敗根因已確認**：(1) 單次 bulk LLM call 輸出全部 nodes + connections，>6 nodes 時 prompt trimming 砍掉一半 definitions；(2) AI connection type（`ai_languageModel`、`ai_tool`）完全沒有 spec 定義，任何含 LangChain nodes 的 workflow 必然失敗。

---

## 1. 規格模糊性問題

### C1-1 §2.3（build_step_loop）— per-step prompt contents 未定義
**問題**：Spec 說「per-step LLM call」+ 給該步的 NodeDefinition，但未定義前面已建好的 `built_nodes` 是否要塞進 prompt（只給 name/type？還是含 parameters？），也沒定義「前一步 invalid 時本步是否延後」。

**影響**：不同實作可能 (a) 不傳前文，導致後續節點與前面脫節（Set 節點不知道前面 HTTP 的 field name）；(b) 全部傳，prompt 爆炸。

**建議補強**：明定 prompt 只含 `previous_nodes=[{name, type}]` 結構化摘要，≤10 項，不含 parameters。

### C1-1 §2.4（connections_linker）— AI connection topology 未定義
**問題**：Spec 寫「無 `intent="condition"` 即 skip 走純 Python 線性連接」，但沒定義 AI Agent 節點（`ai_tool` / `ai_languageModel` / `ai_memory`）的連接方式、switch 第 N 個 output 對應哪一步。

**影響**：AI Agent workflow、switch + merge workflow 直接無法建成。

**建議補強**：新增 `B-CONN-01`/`B-CONN-02`，明定 AI connection 的 composition pattern 與 switch output index convention。

### C1-4 V-PARAM-001 — 「非空」的判定邊界
**問題**：`0`（整數零）、`False`（bool）算非空嗎？n8n 的 `limit: 0` 是合法值。Expression `"={{ $json.x }}"` runtime 可能 evaluate 為空，此時算 pass 還是 fail？

**建議補強**：明列定義：`value is not None and value != "" and value != [] and value != {}`，數字 0 與 bool False 算有效。Expression 字串一律視為有效（交給 Critic）。

### C1-1 §4 Retry Strategy — replan 的 state 保留未定義
**問題**：Replan 時 `state.plan`、`state.built_nodes`、`state.discovery_hits` 哪些清空、哪些保留沒有定義。

**建議補強**：明定 replan 前 state reset：保留 `user_message`, `discovery_hits`, `retry_count`, `messages`；清 `plan, built_nodes, connections, draft, validation`。

### C1-5 §5 — `ChatResponse.ok` 最終判定
**問題**：dry-run 模式（無 API key）下 validator 通過但沒 deploy，`ok` 該是什麼？Code 在 `routes.py::_state_to_response` 自己加了邏輯，但 spec 沒明定。

**建議補強**：補 `A-CHAT-OK` rule，列出 `(dry_run × validator.ok × critic.pass × deploy_success)` 四種組合與對應 `ok` 值。

---

## 2. Orphan Implementations（代碼有、Spec 沒有）

| 功能/邏輯 | 所在檔案 | 影響評估 | 建議 |
|-----------|----------|----------|------|
| **V-NODE-002W**（UUID soft warning） | `validator.py:231` | Med — ID 重用風險 | 回填 C1-4 §1 |
| **V-TOP-005**（read-only top-level fields warn） | `validator.py:158` | Low | 補 C1-4 §1 或改只 drop 不 warn |
| **V-NODE-004-warn**（catalog 不可讀 fallback warn） | `validator.py:277` | Med — 違背 spec 的前置條件 | 補 C1-4 §Errors 或刪掉 fallback 改 raise |
| **Builder prompt 動態裁剪**（defs halving loop） | `builder.py:130-149` | **High** — builder 失敗的核心原因 | **緊急** 補 `B-PROMPT-01`：定義 prompt 預算、優先保留規則、裁剪後 flag |
| **Builder `is_retry` 判斷啟發式** | `builder.py:111-116` | Med — graph.py 與 builder 兩端推斷，易走鐘 | 把 retry mode 寫入 state（`builder_mode: Literal["build","fix"]`）並加入 spec |
| **Assembler branch layout**（y offset 傳播） | `assembler.py:37-92` | Low | 補 C1-1 §2.5 對 branch layout 的正式說法 |
| **Retriever 在 `get_retriever()` 靜默降級到 stub** | `retriever_protocol.py:141-162` | Med — eval 看起來能跑但 retrieval 品質差 | 補 `R-INIT-01`：明定何時允許 fallback |
| **Deployer dry-run mode** | `deployer.py:27-39` | Low | C1-1 §2.10 補 dry-run messages 格式 |
| **routes.py 的 `_connections_list_to_map`** | `routes.py:189` | Med — spec 說 client 負責，但 routes 又做一次 | 釐清責任，只在一處轉換 |

---

## 3. 高風險規格漏洞（可能導致 builder 失敗）

### 漏洞 1：大型 workflow (>4 nodes) 的 prompt 預算沒 spec（P0）
**位置**：C1-1 v1 bulk builder，無 prompt size 控制規則。

**風險**：6 節點 workflow，6 個 definition 各約 500 chars，加上 plan + message + 指令，約 6KB 超過 7B model 的 3-4K context。實作的 halving loop 砍掉一半 definitions，builder 只能 hallucinate 剩餘節點。

**建議補強**：
- 實作 C1-1 v2 per-step builder（正解）；或
- 補 `B-PROMPT-01`：prompt_char_budget = min(context_window × 0.6, 8000)，裁剪優先級 = `schema details > plan details > few-shot examples`，裁剪後必 log `builder.defs_trimmed` event。

### 漏洞 2：AI Agent / tool-calling workflow 完全未定義（P0）
**位置**：`ConnectionType` 含 14 種 type，但 C1-1 和 builder prompt 零相關指引。

**風險**：使用者要求「帶 memory 的 AI chat agent」，planner 選對 types 但 builder 不知道：
- AI Agent 需要 3 種 connection type（`ai_languageModel`、`ai_memory`、`ai_tool`）
- 這些 connection 方向**反向**（tool 節點 supply to agent，不是 agent output to tool）

**建議補強**：新增 AI Agent connection topology spec（C1-9 或 C1-1 §2.12）。補 V-CONN-006（AI Agent 必須有 ≥1 `ai_languageModel`）、V-CONN-007（ai 類 connection 方向規則）。

### 漏洞 3：Placeholder 值直接部署（P0）
**位置**：C1-7 Critic 未實作，C1-4 V-PARAM-009（placeholder 偵測）也未實作。

**風險**：`url="TODO"`、`url=""`、`url="<fill_in>"` 通過 validator 直接 deploy。這是使用者回報 builder 失敗的最常見症狀。

**建議補強（短期，不等 Critic）**：V-PARAM-009 做 best-effort regex 偵測：`TODO|FIXME|<fill_in>|xxx|example\.com|your-api-key`，命中即 error/warn。

### 漏洞 4：`candidate_node_types` 幻覺無防護（P1）
**位置**：C1-1 §2.1 prompt 規則說只能用 `discovery_hits` 的 type，但 `StepPlan` pydantic 只驗非空字串。

**風險**：Small model 上約 15% 案例幻覺出 discovery 沒有的 type，builder `get_detail` 回 None → 空殼節點。

**建議補強**：新增 `P-CAND-01`：planner 後用 Python 過濾 `candidate_node_types`，移除不在 `discovery_hits` 的 type；過濾後 empty → 觸發 replan（`V-PLAN-001`）。

### 漏洞 5：Connection 完整性驗證不足（P1）
**位置**：C1-4 V-CONN-001/002/003 + 004/005（warn only）。

**缺口**：
- `source_output_index` 是否在合理範圍（switch 4 output 時 index=5 是錯）
- 同一 edge 不重複出現
- Connection name 拼寫差異（"Schedule Trigger" vs "ScheduleTrigger"）

**建議補強**：新增 V-CONN-006（output index 範圍）、V-CONN-007（無重複 edge）、V-CONN-008（fuzzy name matching WARN，Levenshtein ≤ 2 建議修正）。

### 漏洞 6：LLM structured-output 失敗的 fallback 未定義（P1）
**位置**：C1-1 §Errors 只覆蓋 planner 失敗，沒覆蓋 builder 同類失敗。

**風險**：Qwen2.5-7B 在 json_schema 模式對超過 3 層嵌套的 schema 常 timeout 或產無效 JSON。目前 `builder.py:166-172` 直接 END，但這應 retry（transient LLM 抖動，不是邏輯錯）。

**建議補強**：補 C1-1 §Errors retry 矩陣：`OutputParserException` → retry 1 次（temperature +0.2）；`httpx.TimeoutException` → retry 1 次（timeout ×1.5）；JSON schema violation → give_up。

### 漏洞 7：`retry_count` 遞增時機（P2）
**位置**：`_make_fix_build_node` 先 `retry_count += 1` 再跑 builder；若 builder 自己拋 exception，retry_count 已被消耗。

**風險**：有時使用者看到「僅 retry 1 次就 give_up」，因為第 1 次 retry 因 transient error 死掉，count 已用完。

**建議補強**：retry_count 只在「validator 真的跑完且回 ok=False」時才遞增，不在進入 fix_build 之初遞增。

### 漏洞 8：Assembler cycle / diamond layout 未定義（P2）
**位置**：C1-1 §2.3 只講線性與 branch。

**風險**：AI Agent memory connection 天然形成 cycle；switch 後接 Merge 形成 diamond。`_assign_positions` 的 `while changed` 遇到 cycle 理論上可能震盪（目前因 `setdefault` first-write-wins 實際不會，但純屬巧合）。

**建議補強**：明定 assembler 只處理 DAG，遇 cycle 即 raise；新增 V-CONN-009 偵測 cycle。

---

## 4. 測試覆蓋缺口

| Spec Acceptance Criteria | 測試檔案 | 狀態 |
|---|---|---|
| C1-1 §Acceptance #1（5 nodes + 1 conditional edge） | `test_graph_wiring.py` | ✅ |
| C1-1 v2（HITL、replan、per-step、critic） | — | ❌ 全缺（功能未實作） |
| C1-2 discovery.count()==529, detailed.count()==30 | `test_retriever.py` | ⚠ mock store，未驗真實 count |
| C1-2 6 條自然語言查詢 top-3 命中 | — | ❌ live test 被跳過 |
| C1-2 v1.1（rerank/query_rewrite/templates） | — | ❌ 全缺（功能未實作） |
| C1-3 6 條 Acceptance | `test_n8n_client_unit.py` + `test_n8n_live.py` | ✅ 大多覆蓋 |
| C1-4 19 條 rule 各有 positive + negative | `test_validator.py` | ✅ 完整 |
| C1-4 v1.1（V-PARAM-001..009、V-SEC-001/002） | — | ❌ 全缺 |
| C1-5 SSE、HITL confirm、rate limit | — | ❌ 全缺 |
| C1-7 Critic Acceptance | — | ❌ 全缺 |
| C1-8 Security Acceptance | — | ❌ 全缺 |

**Eval harness（prompts.yaml）缺口**：
- 無 AI Agent / tool-calling 場景（對應漏洞 2）
- 無 credential binding 測試（對應 C1-7 Critic `unbound_credential`）
- 無 HITL plan-edit 路徑
- 無「deliberate prompt injection」negative prompt（C1-8）
- 無「故意要 executeCommand」security block 測試（C1-8）

**Unit test 品質觀察**：
- `test_validator.py` 品質極高，是整個 codebase 測試最紮實的部分 ✅
- `test_graph_wiring.py` 所有 happy path 只用 2 節點 workflow，**沒有大型 workflow 測試**（直接反映漏洞 1 的 blind spot）

---

## 5. 建議優先補強的 Spec 條目

### P0（builder 失敗的直接根因）

1. **C1-1 v2 per-step builder 實作** + 補 spec 中「previous_nodes 在 prompt 中的呈現格式」（漏洞 1）
2. **新增 AI Agent connection topology spec**（漏洞 2）
3. **V-PARAM-009（placeholder 偵測）短期獨立實作**，不等 Critic（漏洞 3）
4. **B-PROMPT-01 prompt trimming rule**（orphan #4 + 漏洞 1）

### P1（可靠性提升）

5. **P-CAND-01 candidate hallucination guard**（漏洞 4）
6. **V-CONN-006/007/008 connection 完整性規則**（漏洞 5）
7. **C1-1 §Errors LLM transient error retry 矩陣**（漏洞 3）
8. **C1-1 v2 §4 retry_count 遞增時機規範**（漏洞 6）
9. **A-CHAT-OK ChatResponse.ok 判定矩陣**（C1-5 ambiguity）

### P2（防呆與 observability）

10. **C1-2 §5 tier-2 fallback 的 BuiltNode.type 保留規則**
11. **V-CONN-009 cycle detection**（漏洞 7）
12. **建立 `docs/L1-components/IMPL_STATUS.md`**：spec version vs impl version 矩陣表，下次 audit 直接比這張表

### 執行建議

**不要**試圖一次補齊所有 v2 spec 的實作（會再次陷入 spec-code drift）。建議：

1. 先 freeze 一個 **「C1-* v1.1-impl」** 快照，把 spec 降回「實際已實作」版本，v2 功能明確標記為 `[NOT IMPLEMENTED]`。
2. 按 P0 順序逐條開 PR，每條 PR 的 spec-guardian review 必須驗證 `# C1-x:ID` 標注存在（目前整個 codebase 零標注）。
3. 新增 pytest mark：`pytest.mark.spec("V-PARAM-001")`，用 plugin 驗 spec id 存在，建立機械化雙向追溯。
