# D0-4 MVP Verification Results

**執行日期**: 2026-04-20
**LLM**: `qwen3:8b` (Ollama, Q4_K_M, 8.2B, `method="json_schema"`)
**Embed**: `embeddinggemma:latest`
**n8n**: `n8nio/n8n:1.123.31` @ `localhost:5678`

---

## 1. 驗收情境結果

### Scenario 1：排程 + 外呼 + 存檔
> 每小時抓 https://api.github.com/zen 存到 Google Sheet

| Run | Planner | Builder | Validator | Deploy | 總計 | Retry | 節點 |
|---|---|---|---|---|---|---|---|
| r1 | 15.7s | 98.5s | 0.003s | 0.27s | **114.8s** ✅ | 0 | scheduleTrigger → httpRequest → googleSheets |
| r2 | 23.2s | 120.7s | 0.003s | 0.07s | **144.3s** ✅ | 0 | 同上 |

**成功率 2/2**。部署 workflow_url 可開、結構正確。

### Scenario 2：Webhook 條件分支
> 收到 webhook 後，body.type=='urgent' 則發 Slack，否則發 Gmail

| Run | Planner | Builder | Validator | Deploy | 總計 | Retry | 節點 |
|---|---|---|---|---|---|---|---|
| r1 | 51.3s | 71.8s | 0.001s | 0.14s | **123.6s** ✅ | 0 | webhook → respondToWebhook → slack / gmail |
| r2 | 38.2s | ≥1400s (killed) | — | — | **stall** ❌ | — | — |

**成功率 1/2**。r2 在 builder grammar-constrained decoding 卡死（見 §3）。
**品質問題**：Planner 把「判斷 body.type」映射到 `respondToWebhook` 而非 `if`，語意不正確。Discovery retrieval 對「條件判斷」類詞彙排序不足，是 Phase v2 需優化的 RAG 問題。

### Scenario 3：錯誤恢復（trigger-less 誘導）
> 一個 workflow 只有一個 HTTP Request 節點去抓 .../zen，沒有 trigger

| Run | Planner | Builder | Validator | Deploy | 總計 | Retry | 節點 |
|---|---|---|---|---|---|---|---|
| r1 | 24.0s | 22.7s | 0.003s | 0.05s | **47.0s** ✅ | 0 | workflowTrigger → github |

**Retry 未觸發**：Planner 自動補 trigger（符合 n8n 慣例），workflow 合法，validator 直接過。無法觀察 retry 路徑。
**結論**：本 MVP 的 Planner + 現有 validator 規則組合下，僅靠自然語言 prompt 難以**必然**誘導 validator 失敗。Retry 機制在真實場景應由以下情況觸發：
1. LLM 輸出 JSON 解析失敗（罕見，json_schema 已約束）
2. 節點 name 重複、連線指向不存在節點（LLM 偶發錯誤）
3. typeVersion 與 registry 不符

→ 需以「注入式 unit test」驗證 retry loop（已在 `tests/unit/test_graph_retry.py` 由程式面直接覆蓋），不納入 E2E 驗收。

---

## 2. 驗收結論（對照 Plan 門檻）

| 門檻 | 結果 |
|---|---|
| S1 & S2 在 3 次中至少 2 次成功 | S1 ✅ 2/2；S2 ⚠️ 1/2（1 次卡死、1 次成功） |
| S3 retry 2/3 | ❌ 無法以 E2E 方式誘導 retry（改以 unit test 覆蓋） |
| Log 可追 plan → retrieved → workflow_json → deploy_id | ✅ |

**總體**：Deploy pipeline 端到端可用；S1 穩定；S2 成功但存在一個**偶發卡死**（見 §3）+ 一個 Planner **品質問題**（應選 `if` 節點）。

---

## 3. 已知問題

### 3.1 Builder 偶發 stall（S2 r2）
- **現象**：`POST /api/chat` 回 200 OK 後，Ollama runner 降到 ~6% CPU、Python 客戶端 0.4% CPU，連線維持 ESTABLISHED 但不再有資料流動；持續 20+ 分鐘無回應。
- **判定**：qwen3:8b + `method="json_schema"` 對特定 prompt/schema 組合觸發 grammar-constrained decoding pathological case（已知 Ollama/llama.cpp 行為）。
- **緩解 v2**：
  1. 為 `ChatOllama.with_structured_output` 包 asyncio timeout（如 180s），超時視為驗證錯誤進 retry 路徑。
  2. Builder 按 plan step 切分多次較小 LLM call（目前一次生成整張 workflow）。
  3. 嘗試 `method="function_calling"` + 非 thinking 模型（qwen2.5）降低 decode 複雜度。

### 3.2 Planner 節點選型品質
- S2 把「判斷 body.type」選到 `respondToWebhook` 而非 `if`。
- **根因**：discovery_index 對中文條件類描述的向量檢索不夠準，top-1 被其他語意近似節點搶占。
- **v2**：在 discovery 索引的 embedding 前綴加任務範例（task-aware embedding prompt），或引入 keyword boost 規則。

---

## 4. 時序統計

| 階段 | 典型耗時 | 觀察 |
|---|---|---|
| Planner | 15-50s | 15-30s 常態；步驟多時增加 |
| Builder | 70-120s | 隨 plan 步驟數 + schema 複雜度線性增長；偶有 stall |
| Validator | <5ms | pure Python，微不足道 |
| Deployer | 50-300ms | n8n REST 無瓶頸 |
| **E2E** | **90-145s** | 不計卡死案例 |

---

## 5. 後續修復驗證 (2026-04-20 23:48)

以 §3 提及的兩個 v2 改進為目標，在同一日內落地：

**修復 1 — Builder LLM 加硬 timeout**（`app/agent/llm.py:invoke_with_timeout`）
- 用 daemon `threading.Thread` + `Event.wait(timeout=180s)` 包 `ChatOllama.invoke`。
- 超時丟 `LLMTimeoutError`；builder/planner 皆接住，builder 轉為空節點讓 validator/retry 接手。
- 不使用 `concurrent.futures.ThreadPoolExecutor`：其 worker 非 daemon，interpreter exit hook 會等 worker 完成，導致 stall 阻斷 python 退出。

**修復 2 — Planner 始終注入核心控制流節點**（`app/agent/planner.py:_augment_with_core_controls`）
- 啟動時一次性讀 catalog_discovery.json 抽出 `if/switch/filter/merge/set/code` 六個核心 type。
- 每次 planner 在 discovery retrieval 結果後附加這六個（去重），確保無論 embedding 是否命中，LLM 都能挑到。
- 解決 embeddinggemma 對中文條件語意「body.type=='urgent' 則發 X 否則 Y」無法排序出 `if` 的問題。

**S2 重跑結果**：

| 階段 | 耗時 | 備註 |
|---|---|---|
| Planner | 19.3s | steps=4 |
| Builder (initial) | 50.9s | validator ok=False errors=1 |
| Builder (fix, retry=1) | 28.4s | validator ok=True errors=0 |
| **總計** | **98.8s** | — |

**節點**：`webhook` → **`if`** → `slack` / `gmail`（語意正確分支 ✅；已由 `respondToWebhook` 誤判修正為 `if`）
**Retry 路徑實測成功**：自然發生的 validation error 觸發 `fix_build` 節點，重建後 validator pass。

→ 原 Scenario 3 的「retry 驗收」在修復後由 S2 重跑**自然覆蓋**（不需人為注入錯誤）。

## 6. 部署 artefact

| Scenario | workflow_id | URL |
|---|---|---|
| S1 r1 | `5AmrxZjYJLjaVypd` | http://localhost:5678/workflow/5AmrxZjYJLjaVypd |
| S1 r2 | `nFzqYkEUp5Sy47Io` | http://localhost:5678/workflow/nFzqYkEUp5Sy47Io |
| S2 r1 | `UDOdiRmRK5bXonXw` | http://localhost:5678/workflow/UDOdiRmRK5bXonXw |
| S3 r1 | `02DEQJzyVC6Ryc1o` | http://localhost:5678/workflow/02DEQJzyVC6Ryc1o |

