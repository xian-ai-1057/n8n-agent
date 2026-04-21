# n8n Workflow Builder Agent — Spec Index

> **狀態**: Phase 2 Specs（v2 refresh，2026-04-21）｜ **SSOT**: `/Users/kee/.claude/plans/n8n-workflow-builder-agent-snazzy-otter.md`

本目錄為 spec-driven 文件集。所有實作（backend/frontend/data）必須對齊這裡的契約；實作時在原始碼頂端以註解標註 spec id（如 `# Implements C1-4 v1.1`）以利追溯。

## L0 — System

| Spec | 版本 | 檔案 | 摘要 |
|---|---|---|---|
| D0-1 | v1.0 | [L0-system/D0-1_Architecture.md](L0-system/D0-1_Architecture.md) | 系統總覽、元件圖、單輪對話資料流、技術決策表、MVP 範圍邊界 |
| D0-2 | v1.1 | [L0-system/D0-2_Data_Model.md](L0-system/D0-2_Data_Model.md) | Pydantic v2 SSOT：AgentState / WorkflowDraft / ValidationReport / WorkflowTemplate / CriticReport 等 |
| D0-3 | v1.1 | [L0-system/D0-3_Dev_Ops.md](L0-system/D0-3_Dev_Ops.md) | 本機啟動、環境變數（含分階段 model / temperature / embedding profile）、測試策略 |
| D0-4 | v1.0 | [L0-system/D0-4_MVP_Results.md](L0-system/D0-4_MVP_Results.md) | MVP 驗收結果紀錄 |
| D0-5 | v1.0 | [L0-system/D0-5_Evaluation.md](L0-system/D0-5_Evaluation.md) | Eval harness：golden prompts、retrieval / e2e metrics、CI gate |

## L1 — Components

| Spec | 版本 | 檔案 | 摘要 |
|---|---|---|---|
| C1-1 | v2.0 | [L1-components/C1-1_Agent_Graph.md](L1-components/C1-1_Agent_Graph.md) | LangGraph：per-step build 迴圈、rule_class 分流、HITL plan confirm、critic 整合 |
| C1-2 | v1.1 | [L1-components/C1-2_RAG.md](L1-components/C1-2_RAG.md) | 三層 Chroma（discovery / detailed / templates）、embedding prompt profile、query rewrite、reranker |
| C1-3 | v1.0 | [L1-components/C1-3_n8n_Client.md](L1-components/C1-3_n8n_Client.md) | n8n REST 端點、auth、read-only 欄位去除、例外對應 |
| C1-4 | v1.1 | [L1-components/C1-4_Validator.md](L1-components/C1-4_Validator.md) | 19 條結構規則 + V-PARAM-001..009 + V-SEC-001/002；rule_class 分流 |
| C1-5 | v2.0 | [L1-components/C1-5_API.md](L1-components/C1-5_API.md) | `/chat`（SSE / HITL-JSON / one-shot）、`/chat/{session_id}/confirm-plan`、`/health` 三 collection |
| C1-6 | v2.0 | [L1-components/C1-6_UI.md](L1-components/C1-6_UI.md) | SSE 消費、plan 編輯審核、critic 結果獨立顯示、rule_class 色碼 |
| C1-7 | v1.0 | [L1-components/C1-7_Critic.md](L1-components/C1-7_Critic.md) | LLM-as-critic：補 deterministic validator 的語意空白；fail-open |
| C1-8 | v1.0 | [L1-components/C1-8_Security.md](L1-components/C1-8_Security.md) | Prompt-injection 容器化、secret masking、node blocklist、rate limit |

## L2 — Reference

| Spec | 版本 | 檔案 | 摘要 |
|---|---|---|---|
| R2-1 | v1.0 | [L2-reference/R2-1_n8n_Workflow_Schema.md](L2-reference/R2-1_n8n_Workflow_Schema.md) | n8n 1.123.x workflow JSON schema、read-only 欄位、connections 細節 |
| R2-2 | v1.1 | [L2-reference/R2-2_Node_Catalog_Schema.md](L2-reference/R2-2_Node_Catalog_Schema.md) | `catalog_discovery.json` / `definitions/*.json`；新增 `has_detail`、`schema_hint` 欄位 |
| R2-3 | v1.0 | [L2-reference/R2-3_Prompts.md](L2-reference/R2-3_Prompts.md) | Planner / Builder / Fix 三段 prompt（per-step 版與 critic / query_rewrite prompt 將於 v1.1 補齊） |
| R2-4 | v1.0 | [L2-reference/R2-4_Workflow_Templates.md](L2-reference/R2-4_Workflow_Templates.md) | `workflow_templates` collection：few-shot 來源；檢索 API |

## 如何使用這些規格

1. **先讀，再動**：Phase 1 之後任何實作任務，先讀對應 spec 再寫程式碼。
2. **引用**：在原始碼頂端註解 spec id，例如：
   ```python
   """Implements C1-4 Validator — see docs/L1-components/C1-4_Validator.md"""
   ```
3. **SSOT 優先**：遇到欄位型別衝突時，以 **D0-2 Data Model** 為準；遇到 n8n schema 問題時，以 **R2-1** 為準。
4. **六段結構**：每份 spec 必含 Purpose / Inputs / Outputs / Contracts / Errors / Acceptance Criteria 六段。實作完成需滿足 Acceptance Criteria 全部項目。
5. **變更流程**：若實作中發現 spec 有誤或不足，**先改 spec、再改程式碼**，避免漂移。

## MVP 範圍提醒（Phase 2 refresh）

In：
- 「描述 →（選擇性 plan 確認）→ per-step 生成 → validator + critic → 部署 → 回傳 URL」。
- HITL plan review：使用者在 build 前可編輯 `StepPlan`（C1-1 v2.0 §5、C1-5 v2.0 §4、C1-6 v2.0 §4）。
- Validator / Critic 失敗共用 retry budget，`MAX_RETRIES = 2`。
- SSE streaming：每階段事件即時推送到前端（C1-5 v2.0 §2）。
- 分階段 model / temperature 配置（D0-3 v1.1）。
- Eval harness + CI gate（D0-5）。
- Security：prompt-injection 容器化、secret masking、node blocklist、rate limit（C1-8）。

Out：
- 憑證管理 UI（使用者仍需在 n8n 自行綁 credentials）。
- 真實執行回灌（部署後不自動觸發 test execution）。
- 多使用者 / 多 session 並發的水平擴充（MVP 單 process `MemorySaver`）。
- Per-edit tool-calling（整張 workflow 生成，不逐節點對話修改）。
- 對外公開 API（仍設定為本機 Streamlit ↔ backend）。

（細節見 D0-1 §MVP 範圍邊界、C1-1 v2.0 §5、C1-5 v2.0 §6）
