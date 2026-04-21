# n8n Workflow Builder Agent — Spec Index

> **狀態**: MVP Phase 0 Specs ｜ **SSOT**: `/Users/kee/.claude/plans/n8n-workflow-builder-agent-snazzy-otter.md`

本目錄為 MVP spec-driven 文件集。所有實作（backend/frontend/data）必須對齊這裡的契約；實作時在原始碼頂端以註解標註 spec id（如 `# Implements C1-4`）以利追溯。

## L0 — System

| Spec | 檔案 | 摘要 |
|---|---|---|
| D0-1 | [L0-system/D0-1_Architecture.md](L0-system/D0-1_Architecture.md) | 系統總覽、元件圖、單輪對話資料流、技術決策表、MVP 範圍邊界 |
| D0-2 | [L0-system/D0-2_Data_Model.md](L0-system/D0-2_Data_Model.md) | Pydantic v2 SSOT：StepPlan / BuiltNode / Connection / WorkflowDraft / ValidationReport / AgentState 等 |
| D0-3 | [L0-system/D0-3_Dev_Ops.md](L0-system/D0-3_Dev_Ops.md) | 本機啟動、環境變數、run 指令、測試策略、目錄結構 |

## L1 — Components

| Spec | 檔案 | 摘要 |
|---|---|---|
| C1-1 | [L1-components/C1-1_Agent_Graph.md](L1-components/C1-1_Agent_Graph.md) | LangGraph 節點契約（planner / builder / assembler / validator / deployer）、retry 策略 |
| C1-2 | [L1-components/C1-2_RAG.md](L1-components/C1-2_RAG.md) | 雙層 Chroma index（discovery / detailed）、ingest 與 retriever API |
| C1-3 | [L1-components/C1-3_n8n_Client.md](L1-components/C1-3_n8n_Client.md) | n8n REST 端點、auth、read-only 欄位去除、例外對應 |
| C1-4 | [L1-components/C1-4_Validator.md](L1-components/C1-4_Validator.md) | Deterministic rule 清單、severity、訊息模板 |
| C1-5 | [L1-components/C1-5_API.md](L1-components/C1-5_API.md) | FastAPI `/chat`、`/health` 路由合約 |
| C1-6 | [L1-components/C1-6_UI.md](L1-components/C1-6_UI.md) | Streamlit 介面、訊息格式、錯誤顯示 |

## L2 — Reference

| Spec | 檔案 | 摘要 |
|---|---|---|
| R2-1 | [L2-reference/R2-1_n8n_Workflow_Schema.md](L2-reference/R2-1_n8n_Workflow_Schema.md) | n8n 1.123.x workflow JSON 正確 schema、read-only 欄位、connections 細節 |
| R2-2 | [L2-reference/R2-2_Node_Catalog_Schema.md](L2-reference/R2-2_Node_Catalog_Schema.md) | `catalog_discovery.json` 與 `definitions/<slug>.json` 欄位定義 |
| R2-3 | [L2-reference/R2-3_Prompts.md](L2-reference/R2-3_Prompts.md) | Planner / Builder / Fix 三段 prompt 正式版（含 few-shot） |

## 如何使用這些規格

1. **先讀，再動**：Phase 1 之後任何實作任務，先讀對應 spec 再寫程式碼。
2. **引用**：在原始碼頂端註解 spec id，例如：
   ```python
   """Implements C1-4 Validator — see docs/L1-components/C1-4_Validator.md"""
   ```
3. **SSOT 優先**：遇到欄位型別衝突時，以 **D0-2 Data Model** 為準；遇到 n8n schema 問題時，以 **R2-1** 為準。
4. **六段結構**：每份 spec 必含 Purpose / Inputs / Outputs / Contracts / Errors / Acceptance Criteria 六段。實作完成需滿足 Acceptance Criteria 全部項目。
5. **變更流程**：若實作中發現 spec 有誤或不足，**先改 spec、再改程式碼**，避免漂移。

## MVP 範圍提醒

In：單輪「描述 → workflow JSON → 部署 → 回傳 URL」、validator 失敗最多 retry 2 次。
Out：憑證管理 UI、真實執行回灌、多使用者、per-edit tool-calling、多輪精修。（細節見 D0-1 §MVP 範圍邊界）
