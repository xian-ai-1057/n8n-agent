# C1-7：Critic（LLM-as-critic）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: C1-1, C1-4, D0-2, R2-3

## Purpose

C1-4 Validator 是 deterministic pure-Python 檢查，只能判斷 JSON 結構是否合法（欄位存在、型別正確、連接指向既有節點等），但無法判斷工作流「在語意上是否能達成使用者想做的事」。舉例：

- `httpRequest.parameters.url = ""` 在 C1-4 眼中是合法字串，但顯然跑不起來。
- `httpRequest.parameters.url = "TODO"` / `"<fill_in>"` 同上，屬 placeholder 殘留。
- `scheduleTrigger.rule.interval = []`：schedule 沒有設實際頻率。
- Planner 原意是「每小時抓 API」，但 builder 把 `method` 填成 `POST` 且 `url` 指向首頁——語法通過但語意錯。

Critic 節點使用一次小型 LLM 呼叫來做「這份工作流真的會跑起來並解決使用者問題嗎？」的審查。它**不取代** C1-4，而是補語意空白；**兩者都必須通過才能 deploy**。

Critic 設計原則：

- Fail-open：Critic 崩潰 / 逾時時不阻擋 deploy（見 §Errors），因為語意判斷不確定性高，不該成為 single point of failure。
- 廉價：單次 call、無 retry、低溫（temperature=0），目標 ≤ 10s。
- 範圍窄：只看「C1-4 看不到的東西」，避免與 deterministic rule 重複計數。

## Inputs

- `WorkflowDraft`（D0-2 §5）——**僅在 C1-4 已 `ok=True` 後才進入 Critic**。
- 原始 `user_message: str`（使用者 intent 的 ground truth）。
- `plan: list[StepPlan]`（D0-2 §3，用於 intent grounding；每個 StepPlan 的 `description` 是 Critic 比對的參考意圖）。
- LLM handle：`ChatOpenAI(model=CRITIC_MODEL or LLM_MODEL, base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY).with_structured_output(CriticReport, method="json_schema")`，`temperature=0`。
- Prompt：`backend/app/agent/prompts/critic.md`（R2-3 更新時補全；見 §Contracts 4）。

## Outputs

Pydantic model `CriticReport`（Phase 1-B 一併加入 `models/critic.py`；D0-2 v1.1 會把此區塊納入 SSOT）：

```python
# models/critic.py
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class CriticConcern(BaseModel):
    """One semantic concern raised by the Critic LLM."""

    severity: Literal["block", "warn"]
    node_name: str | None = None
    field: str | None = None          # dotted path, e.g. "parameters.url"
    rule: str                         # tag from the fixed taxonomy (see Contracts §3)
    message: str = Field(..., max_length=200)
    suggested_fix: str = Field(..., max_length=200)  # consumed by fix_build prompt


class CriticReport(BaseModel):
    """Output of the Critic node. pass_=True iff no concern has severity='block'."""

    pass_: bool = Field(alias="pass")
    concerns: list[CriticConcern] = Field(default_factory=list)
    latency_ms: int = 0

    model_config = {"populate_by_name": True}
```

呼叫端必須以 `model_dump(by_alias=True)` 序列化以保留 `"pass"` 鍵（Python 保留字需以 alias 繞開）。

## Contracts

### 1. Graph 中的呼叫位置

- Critic 節點位於 Validator **之後**、Deployer **之前**（C1-1 v2.0 會同步更新圖結構）。
- 路由：

```
validator ──ok=True──▶ critic ──pass=True──▶ deployer → END
                          │
                          └──pass=False──▶ fix_build ──▶ assembler ──▶ validator ──▶ ...
validator ──ok=False──▶ fix_build（既有路徑，不經 critic）
```

- **Critic 絕不在 validator 失敗時執行**（節省 LLM 成本；也避免 critic 被 malformed draft 干擾）。
- Critic 的 retry 與 validator retry **共用同一個 `retry_count` 預算**（`MAX_RETRIES = 2`，見 C1-1 §4 與本檔 §7）。

### 2. 批評範圍（Scope of Critique）

Critic 的 prompt 只能問以下問題（超出此清單的語意判斷不在 MVP 範圍）：

1. **Required parameters 是否有真值**：node 的關鍵參數是否為空字串、`"TODO"`、`"<fill_in>"`、`"xxx"`、`"example.com"` 這類 placeholder？
2. **Intent alignment**：每個 node 的 `parameters` 是否合理地對應 `StepPlan.description` 與 `user_message`？例如使用者說「每天把 A 表寄給 B」，但 builder 產出 `emailSend.toEmail = ""`。
3. **Credentials**：node type 通常需要 credential（如 `slack`、`gmail`、`httpRequest` 帶 auth）時，`credentials` 欄位是否有被引用？（MVP 只做「有/沒有」判斷，不驗 credential 內容。）
4. **Reachability**：從 trigger 出發沿著 connections 走，能否達到每個 action node？有沒有孤島節點？
5. **Schedule 合理性**：`scheduleTrigger` / `cron` 的頻率是否合理？例如 `every 1 second` 通常是 builder 誤解單位，應回 `implausible_schedule`。

**禁止重複 C1-4 規則**：Critic prompt 明確告知「下列問題已被 deterministic validator 覆蓋，你不應回報」——包含 `V-TOP-*`、`V-NODE-*`、`V-CONN-*`、`V-TRIG-*` 全部規則。若 Critic 回報了 C1-4 已涵蓋的問題，視為 prompt 迴歸 bug，不算有效 concern。

### 3. Rule 分類法（`CriticConcern.rule`）

固定字串集；新增新 tag 需要 spec bump（避免 prompt 飄移出未知 tag 導致下游處理失敗）。初版集合：

| rule | 適用情境 |
|---|---|
| `empty_required_param` | 必填參數為空字串 / None / 空 list |
| `placeholder_value` | 參數含明顯 placeholder（`TODO`、`<fill_in>`、`xxx`、`example.com`、`your-api-key` 等） |
| `intent_mismatch` | 參數值與 `user_message` / `StepPlan.description` 明顯不符 |
| `unbound_credential` | Node 類型預期要 credential，但 `credentials` 欄位為 None 或空 |
| `unreachable_node` | 從 trigger 沿 connections 無法到達此 node |
| `implausible_schedule` | `scheduleTrigger` / `cron` 頻率顯然有誤（太快、空 rule） |
| `wrong_http_method` | HTTP method 與 intent 不符（使用者說「查詢」卻用 POST） |
| `missing_auth_on_external_call` | `httpRequest` 打外部 API 但沒有 auth / header |

實作層建議：把上列字串放到 `app.agent.critic.RULE_TAGS: set[str]` 常數，並在接 LLM 輸出後做白名單檢查——未知 tag 視為解析失敗，走 fail-open 路徑。

### 4. Prompt 位置

- 檔案：`backend/app/agent/prompts/critic.md`（**新檔**，與 planner/builder/fix 同層）。
- 結構遵照既有 prompt 慣例（R2-3）：`# Role` → `# Rules` → `# Forbidden（C1-4 規則清單，禁止重複）` → `# Few-shot examples`（至少 2 正 2 負） → `# Output schema（CriticReport JSON）`。
- 本 spec 只規範**內容大綱**；完整 prompt 文字在 R2-3 下一次更新時補上，與 spec 分開 review。

### 5. Cost / Latency 預算

- **最多 1 次 LLM 呼叫**；Critic 本身**不做內部 retry**。
- 若 LLM 拋例外 / 逾時 / schema 不合 → 視為 `pass=True`（fail-open），並在 `state.messages` append 一條 `{"role": "critic", "content": "<reason>"}` warning。
- 目標 latency：7B 級模型下 ≤ 10s（p95）。若實測不達標，應降級為「僅對特定 node type 呼叫」而非加 timeout retry。
- `temperature = 0`；確保同一 draft 產出 stable 結論，便於 eval harness（D0-5）比對。

### 6. AgentState 貢獻

在 `AgentState`（D0-2 §7）新增一個欄位（D0-2 v1.1 會反映）：

```python
# 新增欄位（D0-2 v1.1）
critic: CriticReport | None = None
```

`messages` 的 `role` 列舉增加 `"critic"`（C1-1 §5 的 diagnostics schema 同步更新）。Validator 與 Critic 各自 append 自己的 diagnostics，不互相覆寫。

### 7. 與 fix_build 的互動

當 `critic.pass_ == False`：

1. 過濾出 `severity == "block"` 的 concerns（`"warn"` 僅顯示給使用者，不驅動 retry）。
2. 把這些 block concerns 格式化後注入 fix_build prompt 的新區段 `==== Critic concerns ====`（與既有 `==== Validator errors ====` 並列）。每條 concern 輸出：
   ```
   - [rule=<rule>] node=<node_name> field=<field>
     message: <message>
     suggested_fix: <suggested_fix>
   ```
   `suggested_fix` **必須**顯式出現在 prompt 中，這是 Critic 對 fix 的主要訊號（與 Validator 的 `ValidationIssue.message` 扮演不同角色）。
3. `retry_count` 與 Validator 共享 `MAX_RETRIES = 2`。亦即：若 Validator 先失敗 1 次後通過、Critic 再失敗 1 次，即已耗掉預算，下一次 critic 再失敗就走 `give_up`（見 §Errors）。
4. 當 Critic 觸發 retry 時，`fix_build → assembler → validator → critic` 整條路徑重跑（因為 fix 後的新 draft 必須重新過 deterministic validator，不可跳過）。

## Errors

| 情境 | 行為 |
|---|---|
| Critic LLM 逾時 | 記 `messages` `{"role":"critic","content":"timeout"}`；視為 `pass=True`（fail-open）；`state.critic = CriticReport(pass=True, concerns=[], latency_ms=<timeout_ms>)` |
| Critic LLM 輸出不合 schema | LangChain raise `OutputParserException`；捕捉後同上 fail-open |
| Critic 回傳未知 `rule` tag | 將整份 report 視為 malformed；fail-open，messages 記 `"unknown rule tag: <tag>"` |
| `CRITIC_MODEL` 環境變數未設定 | 回退到 `LLM_MODEL`（與 planner/builder 同模型）；不視為錯誤 |
| 連續 2 次 critic block 仍通不過 | 走 `give_up`：`state.error = "critic failed after 2 retries"`，回傳最後一版 draft 與 concerns 供前端顯示；**不允許無限 critic-only retry** |
| Validator 失敗分支中誤呼叫 Critic | 視為 graph bug；`state.error = "critic called before validator passed"`，走 END |

fail-open 的理由：Critic 是 semantic judge，本質就會 false negative / false positive；若讓它阻擋部署，使用者體驗會因 LLM 不穩而劣化。寧可讓少數語意錯誤的 workflow 部署出去（使用者在 n8n UI 會看到），也不要讓所有部署被 critic 掐住。

## Acceptance Criteria

- [ ] Critic 能偵測 `httpRequest.parameters.url == ""` 並回 `empty_required_param` 的 block concern，`field = "parameters.url"`。
- [ ] Critic 能偵測 `scheduleTrigger.parameters.rule.interval == []` 並回 `implausible_schedule` 的 block concern。
- [ ] Critic 對一個「合法且參數完整填好」的 hello-world workflow（`Manual Trigger → Set`）回 `pass=True, concerns=[]`。
- [ ] Critic LLM 逾時或拋例外時，整條 pipeline 仍能走到 deployer（fail-open 行為在 unit test 以 mock LLM 驗證）。
- [ ] 當 critic 回報 block concerns 且 `suggested_fix` 被注入 fix_build prompt 時，LLM 能在下一輪修好 **≥ 60%** 的 critic block 案例（需 D0-5 eval harness 支援；以 20 個人工策劃案例為基準）。
- [ ] Critic 不會回報任何 C1-4 已涵蓋的規則（`V-*` ids 全部不出現在 Critic 輸出中；用 100 筆 malformed draft regression 測試）。
- [ ] `AgentState.critic` 與 `messages` 中 `role="critic"` 條目在全流程結束後存在並可被前端讀取。
- [ ] `CRITIC_MODEL` 未設定時，節點能正常以 `LLM_MODEL` 運行（啟動時 log 一次「falling back to LLM_MODEL」）。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-21 | 初版：引入 LLM-as-critic 補 deterministic validator 語意空白 |
