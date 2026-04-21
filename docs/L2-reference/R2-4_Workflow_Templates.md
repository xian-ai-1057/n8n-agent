# R2-4：Workflow Templates RAG Collection

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: R2-1, R2-2, C1-2

## Purpose

新增第三個 Chroma collection `workflow_templates`，用來存放「可執行等級」的 n8n workflow JSON 作為 few-shot 範例。給 Planner 參考「類似需求的 workflow 長什麼樣」以輔助步驟拆解；給 Builder 參考「真實的 `parameters` 值」以輔助參數填寫。

目前 Planner / Builder 都只靠 `prompts/*.md` 中一則手寫 few-shot 支撐；同時 `catalog_detailed` 覆蓋率僅 30 / 529，Builder 在 detail miss 時幾乎無參考可用。這個 collection 是該問題在 MVP 階段的主要緩解手段——在生成時動態拉取 top-k 個最相近的完整 workflow，作為 in-context 範例注入 prompt。

## Inputs

- 來源資料夾：`data/templates/*.json`。每檔為一份完整的 n8n-format workflow JSON（格式同 R2-1）。
- 每個 `*.json` 需搭配同名 sidecar：`data/templates/<stem>.meta.yaml`，欄位如下：

  | 欄位 | 型別 | 必填 | 說明 |
  |---|---|---|---|
  | `name` | string | ✅ | 模板顯示名 |
  | `description` | string | ✅ | 模板描述（繁中或英文皆可） |
  | `use_case` | string | ✅ | 一句話敘述用途（中英皆可），用於 embedding |
  | `category` | string | ✅ | 分類（例：`notification`, `etl`, `ai-agent`, `scraping`） |

- Embedding 端點：OpenAI 相容 `$OPENAI_BASE_URL/embeddings`，模型 `$EMBED_MODEL`（沿用 C1-2）。
- Chroma persistent client：`$CHROMA_PATH`（沿用 C1-2）。
- 模板可來自 n8n community template gallery 匯出或本地自建庫；MVP 目標 ≥ 20 份。

## Outputs

- 第三個 persistent Chroma collection：`workflow_templates`，與 `catalog_discovery` / `catalog_detailed` 並列於同一 `CHROMA_PATH`。
- Python 檢索 API：`Retriever.search_templates_by_query` 與 `Retriever.search_templates_by_types`（於 Phase 2-D 加入 `backend/app/rag/retriever.py`）。
- Ingest script：`backend/app/rag/ingest_templates.py`，由 `scripts/bootstrap_rag.py --only templates` 觸發。

## Contracts

### 1. Collection 設計

| Collection | 來源 | Embedding 文本 |
|---|---|---|
| `workflow_templates` | `data/templates/*.json` + sidecar `*.meta.yaml` | 見下方 document shape |

**Document shape（送入 embedder 的字串）**：

```
title: {name} | text: {description}\n用途: {use_case}\n節點組成: {comma_sep_types}
```

其中 `comma_sep_types` = 由 workflow JSON 萃取 `nodes[*].type` 去重後以 `, ` 串接。Embedding 的 document-side prompt 約定沿用 C1-2 §1。

Chroma 初始化：

```python
client = chromadb.PersistentClient(path=CHROMA_PATH)
templates = client.get_or_create_collection(
    name="workflow_templates",
    metadata={"hnsw:space": "cosine"},
)
```

### 2. Metadata

因 Chroma metadata 僅支援 scalar（str / int / float / bool），複雜欄位以 JSON 字串存放。

```python
{
  "template_id": str,          # filename stem（唯一）
  "name": str,
  "description": str,
  "use_case": str,
  "category": str,
  "node_types": str,           # json.dumps(list[str])；retrieve 時反序列化
  "n_nodes": int,
  "raw": str,                  # json.dumps(完整 workflow JSON)；用於 hydrate WorkflowTemplate
}
```

Document id：`template_id`（= filename stem）。重複 upsert 覆蓋同 id。

### 3. Retrieval API

新增於 `backend/app/rag/retriever.py`：

```python
from pydantic import BaseModel

class WorkflowTemplate(BaseModel):
    template_id: str
    name: str
    description: str
    node_types: list[str]
    workflow_json: dict  # 完整 n8n workflow（由 metadata.raw 反序列化）

class Retriever:
    # ... 既有 search_discovery / get_detail / search_detail ...

    def search_templates_by_query(
        self, query: str, k: int = 3
    ) -> list[WorkflowTemplate]:
        """Planner 用：回傳語意上最相近的 k 個完整 workflow 範例。"""

    def search_templates_by_types(
        self, required_types: list[str], k: int = 3
    ) -> list[WorkflowTemplate]:
        """Builder 用：以節點組成為主的檢索。

        流程：
        (a) 以 `required_types` 組一個 synthetic query：
            "節點組成: {', '.join(required_types)}"
        (b) embedding 取 top-(k*3) 候選（cosine）。
        (c) 對每個候選計 Jaccard(required_types, template.node_types)，降序排。
        (d) 取前 k 回傳；若 Jaccard tie，則保留 cosine 序。
        """
```

`WorkflowTemplate.workflow_json` 必為 dict（`json.loads(metadata["raw"])`）；`node_types` 為 `json.loads(metadata["node_types"])`。

### 4. Ingest 模組

`backend/app/rag/ingest_templates.py`：

```python
def ingest_templates(templates_dir: str, *, force: bool = False) -> int:
    """Upsert all *.json + sidecar meta under templates_dir. Returns count ingested."""
```

處理流程：
1. 掃 `data/templates/*.json`；對每檔讀同名 `*.meta.yaml`。
2. 以 R2-1 的 `WorkflowValidator` 驗證 workflow JSON。僅把 **V-TOP / V-NODE / V-CONN** 類錯誤視為 fatal；V-TRIG 類警告可接受（部分模板是片段、允許缺 trigger）。
3. 任一必要欄位缺失或 fatal 錯誤 → 印 WARN 並 skip 該檔，不中斷整批。
4. 合法項目以 batch size 16 呼叫 embedder 與 `collection.upsert`。
5. `force=True` 時先 `client.delete_collection("workflow_templates")` 再重建。

CLI：`python scripts/bootstrap_rag.py --only templates`（擴充 C1-2 §2 的 `--only` 參數支援 `templates`）。

### 5. 上游消費（與 R2-3 交叉參照）

- **Planner**：prompt 中注入 `retriever.search_templates_by_query(user_message, k=3)` 的結果（各範例只放 `name` + `description` + `node_types`，不塞完整 JSON，避免 token 爆炸）。
- **Builder**：prompt 中注入 `retriever.search_templates_by_types(plan.node_types, k=3)` 的結果（放完整 `workflow_json` 節點片段，讓 Builder 抄參數）。此路徑取代 `prompts/builder.md` 中目前唯一的硬編碼 few-shot。
- 上述整合的正式 prompt 欄位由 R2-3 後續版本補上；本 spec 僅規範 retrieval 契約。

## Errors

| 情境 | 行為 |
|---|---|
| sidecar `*.meta.yaml` 缺失或解析失敗 | 該檔 skip + WARN（不中斷整批） |
| workflow JSON 不合 R2-1 core schema（V-TOP / V-NODE / V-CONN 任一 fatal） | 該檔 skip + WARN |
| meta 缺必要欄位（`name` / `description` / `use_case` / `category`） | 該檔 skip + WARN |
| 兩份 template 出現相同 `template_id` | raise（視為資料錯誤，須人工處理） |
| embedding 端點不可達 | raise `EmbedderUnavailable`（同 C1-2） |
| `workflow_templates` collection 未建立而 retriever 被呼叫 | raise `RagNotInitialized`（同 C1-2） |

## Acceptance Criteria

- [ ] `python scripts/bootstrap_rag.py --only templates` 成功後，`workflow_templates.count() >= 20`。
- [ ] `search_templates_by_types(['n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.httpRequest', 'n8n-nodes-base.googleSheets'], k=3)` 回傳 top-1 的 `node_types` 必須同時包含 `n8n-nodes-base.scheduleTrigger` 與 `n8n-nodes-base.httpRequest`。
- [ ] `search_templates_by_query('發 Slack 訊息通知', k=3)` 回傳的 3 個結果其 `node_types` 全部包含 `n8n-nodes-base.slack`。
- [ ] 已入庫的每一個 template 皆通過 R2-1 V-TOP / V-NODE / V-CONN 核心驗證。
- [ ] 刻意放入壞掉的 template（缺 meta 或 schema 不合）時，ingest 不中斷整批，且其他合法 template 正常入庫。
- [ ] `search_templates_by_query(...)` 與 `search_templates_by_types(...)` 回傳的 `WorkflowTemplate.workflow_json` 為可直接餵給 Builder 的 dict。
- [ ] 對同一份 template 重複 ingest（無 `--force`）不會產生重複項——count 保持穩定。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-21 | 初版：新增 `workflow_templates` collection 以解決 few-shot 覆蓋不足與 `catalog_detailed` 30/529 的參數來源空洞問題 |
