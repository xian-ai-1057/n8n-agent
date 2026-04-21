# Evaluation Harness — Golden Prompts

本目錄是 D0-5 evaluation harness 的資料區（`docs/L0-system/D0-5_Evaluation.md` v1.0）。

```
tests/eval/
├── prompts.yaml        ← 本次 commit 新增；手動維護、版控
├── baseline.json       ← 首次 `python -m app.eval baseline save` 後產生；手動 PR
├── llm_cache/          ← CI 用（--mock-llm）；git-ignored
└── report/             ← 每次 run 產出的 json + md；git-ignored
```

## 目前狀態

- `prompts.yaml`：28 筆 golden prompt，覆蓋 D0-5 §1 所列 13 個 tag，每類 ≥ 2 筆。
- `baseline.json`：尚未建立。Eval harness 實作上線後，以首次成功 run 作為 baseline。
- Harness 程式碼：尚未實作（對應 `backend/app/eval/`）。

## Tag 覆蓋矩陣（prompts.yaml 現況）

| Tag | 筆數 |
|---|---|
| schedule_trigger | 8 |
| webhook_trigger | 8 |
| manual_trigger | 8 |
| branching_if | 4 |
| branching_switch | 4 |
| transform_set | 6 |
| transform_code | 7 |
| http_request | 11 |
| output_slack | 6 |
| output_sheet | 6 |
| output_email | 4 |
| multi_step_chain | 10 |
| detail_missing_type | 2 |

## 使用（harness 實作後）

```bash
# 完整 run（真 LLM 端點；約 30~90 分鐘，視 model 與並發而定）
python -m app.eval run

# 僅跑指定 prompts
python -m app.eval run --ids github_zen_to_sheet,webhook_urgent_branch

# CI 用；以 llm_cache 內容重放，< 30s
python -m app.eval run --mock-llm

# 首次建立 baseline
python -m app.eval baseline save

# 檢查當前 run vs baseline
python -m app.eval compare latest
```

## 擴充 prompts 的原則

1. **新 prompt 來自真實使用場景**，而非臆想：以 n8n 社群範本 / 使用者回報為樣本來源。
2. **每新增一個 tag** 至少同批補兩筆避免單點。
3. **`expect.required_types` 必須在 `data/nodes/catalog_discovery.json` 內**（執行前由 harness 驗）。
4. **`critic_pass: false` 需明確註解原因**（例如 detail_missing_type 的 unbound credential），讓 reviewer 看得懂為何放寬期望。
5. 更新 prompts 後請同步 `python -m app.eval baseline save`，並在 PR 描述說明 baseline 差異。

## 參考

- D0-5 Evaluation 規格：[`../../docs/L0-system/D0-5_Evaluation.md`](../../docs/L0-system/D0-5_Evaluation.md)
- C1-1 Agent Graph v2.0 （pipeline 階段）：[`../../docs/L1-components/C1-1_Agent_Graph.md`](../../docs/L1-components/C1-1_Agent_Graph.md)
- C1-2 RAG v1.1 （retrieval metrics 計算基礎）：[`../../docs/L1-components/C1-2_RAG.md`](../../docs/L1-components/C1-2_RAG.md)
- C1-4 Validator v1.1 / C1-7 Critic：decide pass/fail 的規則清單。
