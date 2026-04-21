# C1-2：RAG（雙層節點索引）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, R2-2

## Purpose

規範「discovery + detailed」雙 ChromaDB collection 的 ingest 與 retrieve 契約。Planner 用 discovery 廣度檢索選 type；Builder 用 detailed 深度取得參數 schema。

## Inputs

- `data/nodes/catalog_discovery.json`（由 `scripts/xlsx_to_catalog.py` 從 `n8n_official_nodes_reference.xlsx` 產出；529 筆，格式見 R2-2）
- `data/nodes/definitions/*.json`（30 筆詳細節點，格式見 R2-2）
- OpenAI 相容 embedding 端點：`$OPENAI_BASE_URL/embeddings`，模型 `$EMBED_MODEL`（預設 `BAAI/bge-m3`）
- `CHROMA_PATH` 環境變數

## Outputs

- 兩個 persistent Chroma collection：
  - `catalog_discovery`（529 items）
  - `catalog_detailed`（~30 items）
- Python API：`search_discovery`, `get_detail`, `search_detail`。

## Contracts

### 1. Collection 設計

| Collection | 來源 | Embedding 文本 | Metadata |
|---|---|---|---|
| `catalog_discovery` | `catalog_discovery.json` | `f"{display_name}. Category: {category}. {description}"` | `{type, display_name, category, default_type_version?}` |
| `catalog_detailed` | `definitions/*.json` | `f"{display_name}. {description}. Parameters: {param_summary}"` | `{type, display_name, category, type_version, parameters_json}` |

`param_summary` = 取 `parameters[*].display_name` join "、"（避免把完整 JSON 塞進 embedding 文本）。
`parameters_json` = `json.dumps(definition.parameters)` 字串；retrieve 時由 `get_detail` 反序列化。

Chroma collection 初始化（實作上 embedding 由 backend 端預先產生後傳入 `upsert/query`，
Chroma 本身不持有 embedding function，詳見 `backend/app/rag/store.py`）：

```python
client = chromadb.PersistentClient(path=CHROMA_PATH)
discovery = client.get_or_create_collection(
    name="catalog_discovery",
    metadata={"hnsw:space": "cosine"},
)
# Embedding 透過 OpenAIEmbedder（langchain_openai.OpenAIEmbeddings）：
# base_url = OPENAI_BASE_URL, api_key = OPENAI_API_KEY, model = EMBED_MODEL
```

### 2. Ingest API（scripts / ingest modules）

```python
# backend/app/rag/ingest_discovery.py
def ingest_discovery(catalog_path: str, *, force: bool = False) -> int:
    """Upsert all rows from catalog_discovery.json. Returns count ingested.
    If force, delete then re-create the collection."""

# backend/app/rag/ingest_detailed.py
def ingest_detailed(definitions_dir: str, *, force: bool = False) -> int:
    """Upsert all *.json under definitions_dir."""
```

Document id：用 `type`（保證唯一）。因此再 ingest 時 upsert 會覆蓋同一 id。

`scripts/bootstrap_rag.py` 先呼叫 `ingest_discovery`，再 `ingest_detailed`，各印 "ingested N"。

### 3. Retriever API

```python
# backend/app/rag/retriever.py
from pydantic import BaseModel

class DiscoveryHit(BaseModel):
    type: str
    display_name: str
    category: str
    description: str
    score: float  # 1 - cosine_distance, higher is better


class Retriever:
    def __init__(self, client, embed_model: str): ...

    def search_discovery(self, query: str, k: int = 8) -> list[DiscoveryHit]:
        """Used by Planner. Returns top-k by cosine similarity."""

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        """Used by Builder. Exact lookup by type; None if missing from detailed index."""

    def search_detail(self, query: str, k: int = 3) -> list[NodeDefinition]:
        """Fallback path when Builder needs to bag additional context."""
```

### 4. Re-ingest 觸發條件

| 觸發 | 動作 |
|---|---|
| 編輯 `data/nodes/definitions/*.json` | `python scripts/bootstrap_rag.py --only detailed` |
| 重新跑 `xlsx_to_catalog.py` | `python scripts/bootstrap_rag.py --only discovery` |
| Embed model 換 tag | `python scripts/bootstrap_rag.py --force` |
| `CHROMA_PATH` 改位置 | 同上 |

`--only` 與 `--force` 為 CLI flag（Phase 2-A 實作）。

### 5. 降級策略

Builder 呼叫 `get_detail(type)` 回 None 時：
1. 呼叫 `search_detail(step_description, k=3)` 嘗試近似命中 — 若同類別（例如 Slack 缺，但查到 Telegram）視為提示，不直接使用。
2. 若仍無，產出 "空殼節點"：`BuiltNode(parameters={}, ...)` 並把 `messages` 追加 `{"role": "system", "content": "<type> not in detailed index; user must fill params in n8n UI"}`。

### 6. 驗收查詢（plan P2-A）

| Query | 期望 top-3 含 |
|---|---|
| "發 Slack 訊息" | `n8n-nodes-base.slack` |
| "排程觸發" | `n8n-nodes-base.scheduleTrigger` |
| "HTTP GET" | `n8n-nodes-base.httpRequest` |
| "條件分支" | `n8n-nodes-base.if` 或 `n8n-nodes-base.switch` |

## Errors

| 情境 | 行為 |
|---|---|
| embedding 端點不可達 | ingest / retriever 直接 raise `EmbedderUnavailable`（不吞） |
| collection 不存在（未跑 ingest） | retriever 啟動時 raise `RagNotInitialized` |
| `definitions/*.json` 解析失敗 | ingest 印 warning 並 skip 該檔（不中斷整批） |
| 同 `type` 跨兩檔 | 視為資料錯誤，raise |

## Acceptance Criteria

- [ ] `python scripts/bootstrap_rag.py` 成功後，`catalog_discovery.count()` == 529，`catalog_detailed.count()` == 30（±1 容差視 Phase 1-B 取捨）。
- [ ] §6 四個 query 皆在 top-3 命中預期 type。
- [ ] `get_detail("n8n-nodes-base.httpRequest")` 回完整 `NodeDefinition`，`parameters` 非空。
- [ ] `get_detail("n8n-nodes-base.<不存在>")` 回 `None` 不 raise。
- [ ] `--force` 重跑後 count 與首次一致（upsert 正確）。
