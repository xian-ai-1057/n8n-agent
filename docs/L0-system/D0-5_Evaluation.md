# D0-5：Evaluation Harness

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: C1-1, C1-2, C1-4, C1-7

## Purpose

Agent 有多種只會在統計尺度上顯現的失效模式（retrieval miss rate、parameter hallucination rate、critic pass rate）。沒有 harness 的情況下，prompt / embedder / model 的任一次變更都可能造成沉默退化（silent regression）。本 spec 定義一個輕量、可重現、離線執行的 evaluation harness：以固定 golden prompt set 作比對基準，並在 merge 前作為閘門（gate）攔下退化。

與 D0-3 §6 測試策略的分工：unit / e2e 測試確保「程式對 spec 正確」；本 harness 確保「模型+prompt+RAG 的統計品質不退化」。

## Inputs

- `tests/eval/prompts.yaml`：golden prompt set（見 §Contracts 1）。
- 可用的 backend + RAG + LLM endpoint；eval 可選擇直接打 `/chat` 或呼叫 `run_cli` 以便加速。
- 可選：baseline metrics 檔 `tests/eval/baseline.json`，作為 regression 比較基準。
- 可選：`tests/eval/llm_cache/` 快取目錄（`--mock-llm` 模式使用）。

## Outputs

- `tests/eval/report/{timestamp}.json`：per-prompt stage metrics（結構化）。
- `tests/eval/report/{timestamp}.md`：Markdown summary，供 human review 與 PR comment。
- Exit code：全部 gate 通過為 0，否則非 0（CI 直接擋 merge）。

## Contracts

### 1. Golden prompt set 結構（`prompts.yaml`）

```yaml
version: 1
prompts:
  - id: github_zen_to_sheet
    message: "每小時抓 https://api.github.com/zen 存到 Google Sheet"
    expect:
      plan:
        min_steps: 3
        required_intents: [trigger, action, output]
        required_types:
          - n8n-nodes-base.scheduleTrigger
          - n8n-nodes-base.httpRequest
          - n8n-nodes-base.googleSheets
      validator_ok: true
      critic_pass: true
      deploy: skip        # eval 期間不真的 POST 到 n8n
    tags: [schedule, http, sheet]
  - id: slack_notify_on_webhook
    message: "收到 webhook POST 就發 Slack 通知"
    expect:
      plan:
        required_types: [n8n-nodes-base.webhook, n8n-nodes-base.slack]
      validator_ok: true
    tags: [webhook, output_slack]
  # ... 20+ prompts
```

最少 20 筆 prompt，且以下 tag 每種至少 2 筆：

`schedule_trigger`, `webhook_trigger`, `manual_trigger`, `branching_if`, `branching_switch`, `transform_set`, `transform_code`, `http_request`, `output_slack`, `output_sheet`, `output_email`, `multi_step_chain`, `detail_missing_type`（故意命中一個不存在於 `catalog_detailed` 的節點類型，測 graceful degradation）。

### 2. 每筆 prompt 計算的 metrics

| metric | 公式 |
|---|---|
| `plan.hit_rate` | 對 `required_types` 取平均：該 type 出現在任一 `step.candidate_node_types` 記 1，否則 0 |
| `plan.intent_match` | 每個 `required_intents` 都出現在某個 `step.intent` 才記 1 |
| `retrieval.recall_at_8` | `count(required_types ∈ discovery_hits[:8]) / count(required_types)` |
| `retrieval.mrr` | `1 / rank`（第一個命中的 required_type），全無命中記 0 |
| `builder.validator_pass` | `validation.ok == True` 記 1 |
| `critic.pass` | `critic.pass == True` 記 1 |
| `e2e.success` | `validator_pass AND critic_pass` 才記 1 |
| `latency.total_ms` | wall-clock 時間 |
| `tokens.{planner,builder,critic}` | 由 LLM callback handler 取得（若可暴露，否則 null） |

### 3. Aggregate metrics

- 對所有 prompt 取平均作為 run-level metric。
- 另輸出 per-tag 的平均，讓我們能看出例如 `branching_*` 整體表現不佳的弱類別。
- 輸出 per-prompt 的明細，供 regression gate 逐筆比對。

### 4. Gates（exit code != 0 即 fail）

**Relative gates（相對於 baseline）**：

- `retrieval.recall_at_8` 絕對值退化不得超過 5%。
- `e2e.success` 不允許任何退化。
- 任何在 baseline 中 pass 的 prompt 必須在本次仍 pass（禁止 per-prompt regression，即使 aggregate 變好也不允許）。

**Absolute floors（首次設定用，後續逐步收緊）**：

- `retrieval.recall_at_8 >= 0.70`
- `builder.validator_pass >= 0.80`
- `e2e.success >= 0.60`

### 5. CLI

```bash
python -m app.eval run                               # 跑完整 suite，輸出帶 timestamp 的 report
python -m app.eval run --ids github_zen_to_sheet,slack_notify_on_webhook
python -m app.eval baseline save                     # 把當前 run 升級為 baseline
python -m app.eval compare latest                    # diff latest run vs baseline
```

實作位置：`backend/app/eval/`（新模組，對應 D0-3 目錄結構）。

### 6. CI 整合

- Pre-merge CI job：執行 `python -m app.eval run --mock-llm`，以快取的 LLM 回應重放，控制 CI 時間與成本。
- Nightly job：以真 LLM 端點執行完整 eval，結果回寫至報表區。

**Mocking 約定**：

- Cache key = `sha256(prompt_text + model + temperature)`。
- Cache 檔放在 `tests/eval/llm_cache/`，以 key 為檔名。
- 在 `--mock-llm` 模式下若 cache miss，CLI 直接失敗並列出缺失的 key；**不允許自動回落到真 LLM**，強迫開發者明確更新 cache。

### 7. 資料生命週期

- `prompts.yaml`：手動維護、版本化進 repo。
- `baseline.json`：僅能透過 `baseline save` 命令更新，且該更新必須以 PR 形式送審（不在 CI 自動 bump）。
- `tests/eval/report/`：gitignore；若某次 summary 需要被引用，以手動 commit 該筆 markdown 的方式納管。

## Errors

| 情境 | 行為 |
|---|---|
| `prompts.yaml` schema 無效 | CLI exit code 2，並印出第一筆有問題的 prompt id 與欄位 |
| LLM endpoint 無法連線 | CLI exit code 3，訊息明確指出 `OPENAI_BASE_URL` 與健檢步驟 |
| `--mock-llm` 模式下 cache miss | CLI exit code 4，列出所有缺失的 cache key 與對應 prompt id |
| `baseline.json` 缺失 | CLI 以 warning 模式提示，跳過 regression gates，僅套用 absolute floors |
| Report 目錄無法寫入 | 直接 raise，不 swallow（與 D0-3 Chroma 目錄權限錯一致） |

## Acceptance Criteria

- [ ] `prompts.yaml` 包含 ≥ 20 筆覆蓋上述 tag 的 prompt。
- [ ] `python -m app.eval run` 在本機跑完並寫出 JSON + Markdown 報告。
- [ ] `--mock-llm` 模式下在 < 30s 完成全部 prompts（CI 預算）。
- [ ] 可針對單一 prompt 再跑 (`--ids`)，方便除錯。
- [ ] 報告的 per-tag 分數存在，能看出弱類別。
- [ ] CI job 失敗時，PR 上能看到具體是哪個 prompt 退化了哪個 metric。

### 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-21 | 初版：建立 golden prompt + metrics + CI gate 的 eval harness |
