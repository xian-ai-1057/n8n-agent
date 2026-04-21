# C1-2：RAG（雙層節點索引）

> **版本**: v1.1.0 ｜ **狀態**: Draft ｜ **前置**: D0-2, D0-3, R2-2, R2-4

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
| `catalog_discovery` | `catalog_discovery.json` | `f"{display_name}. Category: {category}. {description}"` | `{type, display_name, category, default_type_version?, has_detail}` |
| `catalog_detailed` | `definitions/*.json` | `f"{display_name}. {description}. Parameters: {param_summary}"` | `{type, display_name, category, type_version, parameters_json}` |
| `workflow_templates` | `data/templates/*.json` + sidecar `*.meta.yaml` | 詳見 R2-4 §1 | 詳見 R2-4 §2 |

`catalog_discovery` metadata 新增 `has_detail: bool`（由 ingest 依 `data/nodes/definitions/{slug}.json` 存在與否合成，見 R2-2 §6）；Planner 可用此欄位偏好「典型已收錄」節點（見 §3 `filter_by_coverage`、§5 降級鏈）。
`workflow_templates` collection 作為 Planner / Builder 的 few-shot 範例源，完整 ingest / retrieval 契約見 R2-4；本 spec §3 僅登錄其暴露給 Retriever 的兩支查詢 API。

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
        """Used by Planner. v1.1 起內部整合 query rewrite（§8）與 rerank（§9）:
        rewrite_query → 每個 q 取 top-(k * RERANKER_CANDIDATES_MULTIPLIER) → 按 type
        去重並 max-score wins 合併 → rerank 到 k。舊呼叫 shape 完全相容。"""

    def get_detail(self, node_type: str) -> NodeDefinition | None:
        """Used by Builder. Exact lookup by type; None if missing from detailed index."""

    def search_detail(self, query: str, k: int = 3) -> list[NodeDefinition]:
        """Fallback path when Builder needs to bag additional context."""

    # --- v1.1 新增：workflow_templates（R2-4） ---

    def search_templates_by_query(self, query: str, k: int = 3) -> list[WorkflowTemplate]:
        """Planner 用：以自然語言檢索 few-shot workflow 範例。契約見 R2-4 §3。"""

    def search_templates_by_types(
        self, required_types: list[str], k: int = 3
    ) -> list[WorkflowTemplate]:
        """Builder 用：以節點組成為主的檢索（embedding + Jaccard 重排）。契約見 R2-4 §3。"""

    # --- v1.1 新增：coverage-aware helper ---

    def filter_by_coverage(self, hits: list[DiscoveryHit]) -> list[DiscoveryHit]:
        """重排 hits：先放 `has_detail=True` 者，組內按 score 降序；再接 `has_detail=False`，
        組內亦按 score 降序。**永不 drop**；純粹是 nudge，讓 Planner / Builder 在候選同分
        時優先選擇已收錄 detailed 參數的 type，避免卡在空殼節點。"""
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

v1.1 起降級鏈延伸為四階（前三階為「避免走到空殼」的緩解路徑，第四階保留 v1.0 行為）：

1. **Coverage bias（Planner 端，選型前）**：Planner 在拿到 `search_discovery` 結果後，呼叫
   `retriever.filter_by_coverage(hits)` 重排，使 `has_detail=True` 者排前。此步僅影響「哪個 type
   被選入 Plan」，不影響後續 get_detail 路徑。
2. **同類別近似（Builder 端，get_detail 回 None 時）**：呼叫 `search_detail(step_description, k=3)`
   嘗試近似命中 — 若同 `category` 出現近鄰（例如 Slack 缺，但查到 Telegram），視為提示候選；
   Builder 可選用並以 `messages` 追加 warning，說明實際 type 與所用 template 的落差。
3. **Template 參數骨架（v1.1 新增）**：仍無法確定時，呼叫
   `search_templates_by_types([type], k=1)`；若回傳的 template 中含該 `type`，取該節點在 template
   內的 `parameters` 實例作為 **zero-shot 參數骨架**，填入 `BuiltNode.parameters`。Builder 應視為
   「未經 schema 驗證的樣板值」並以 `messages` 附 warning。
4. **空殼節點**（v1.0 行為保留）：以上三階皆失敗 → 產出 `BuiltNode(parameters={}, ...)` 並把
   `messages` 追加 `{"role": "system", "content": "<type> not in detailed index; user must fill params in n8n UI"}`。

### 6. 驗收查詢（plan P2-A）

| Query | 期望 top-3 含 |
|---|---|
| "發 Slack 訊息" | `n8n-nodes-base.slack` |
| "排程觸發" | `n8n-nodes-base.scheduleTrigger` |
| "HTTP GET" | `n8n-nodes-base.httpRequest` |
| "條件分支" | `n8n-nodes-base.if` 或 `n8n-nodes-base.switch` |
| "收到 webhook 觸發發 Slack 訊息" | `n8n-nodes-base.webhook`, `n8n-nodes-base.slack` |
| "body.type=='urgent' 則發 X 否則 Y" | `n8n-nodes-base.if` 或 `n8n-nodes-base.switch` |

後兩條在 v1.0（`embedder.py` 硬編 embeddinggemma prompt、對 `BAAI/bge-m3` 有 prompt mismatch）時
無法穩定命中；目前 `planner.py` 以硬編的 `_CORE_CONTROL_TYPES` seeding 臨時補救。完成 §7 profile
修正後，此兩條應能在 top-3 自然命中，屆時 `_CORE_CONTROL_TYPES` seeding 可從 `planner.py` 移除
（實作層任務，不屬本 spec 範圍 — 僅登錄為條件）。

### 7. Embedding Prompt Profiles（v1.1 新增）

v1.0 的 `backend/app/rag/embedder.py` 不分模型一律以 embeddinggemma 的
`"task: search result | query: {text}"` 包裹 query；這對預設 `BAAI/bge-m3` 與 OpenAI
`text-embedding-*` 系列皆為錯誤 prompt，會顯著壓低相似度品質（§6 後兩條驗收查詢失敗的主因）。
v1.1 引入 **embedding prompt profile** 將 prompt 包裝與 embedding 模型解耦。

**環境變數**：`EMBED_PROMPT_PROFILE`（見 D0-3 §2.1，預設 `auto`）。

**Profile 對照表**（`{text}` = 原始 query 字串；`{display_name}` / `{body}` = document-side 欄位）：

| profile | query prompt wrapper | document prompt wrapper |
|---|---|---|
| `embeddinggemma` | `"task: search result \| query: {text}"` | `"title: {display_name} \| text: {body}"` |
| `bge` | `"{text}"` | `"{text}"` |
| `openai` | `"{text}"` | `"{text}"` |
| `none` | `"{text}"` | `"{text}"` |
| `auto` | 依 `EMBED_MODEL` id 子字串推斷：包含 `embeddinggemma` / `gemma` → `embeddinggemma`；包含 `bge` → `bge`；包含 `text-embedding` → `openai`；否則 fallback 為 `none`。 | 同左 |

**套用位置**：僅 `OpenAIEmbedder.embed(text)`（query side）與 `OpenAIEmbedder.embed_batch(texts)`
（document side；批次內每筆都會走 document wrapper）。由 embedder 單點擁有 prompt 包裝後，
**ingest 端必須停止自行拼 `"title: ... | text: ..."`** —— 該包裝改由 profile 負責。
（實作時一併移除 `ingest_discovery.py` / `ingest_detailed.py` 的手工包裝，但此屬實作細節，
不在本 spec 範圍。）

**驗收**：

- 切換 `EMBED_PROMPT_PROFILE` 不應導致 `scripts/bootstrap_rag.py` 失敗；count 結果與 §Acceptance
  Criteria 首條一致。
- Profile 變更視同 embedding 空間改變 → 必須搭配 `--force` 重建 collection（沿用 §4 規則）。

### 8. Query Rewrite（Multi-Query，v1.1 新增）

為了提升 Planner discovery 階段的召回（尤其對自然語言句式、雙語混寫、錯字等），在 embedding
查詢前加入可停用的前置步驟：以單次小型 LLM 呼叫把使用者原句改寫為 1..3 個英文 keyword-style
query，合併後對每個改寫個別檢索再合併。

**契約**：

```python
def rewrite_query(user_message: str) -> list[str]:
    """Produce 1..3 rewritten queries to widen recall.

    Strategy: single small LLM call returning up to 3 short English keyword-style
    reformulations of the user's intent. The original message is ALWAYS included
    as rewrite[0]. Disabled if QUERY_REWRITE_ENABLED=0 (default: 1)."""


def search_discovery(query: str, k: int = 8) -> list[DiscoveryHit]:
    """Unchanged shape. Internally: for q in rewrite_query(query): embed+query;
    merge by type with max-score wins; return top-k."""
```

**Prompt**：位於 `backend/app/agent/prompts/query_rewrite.md`（由 R2-3 後續版本補上，本 spec 僅
宣告此檔案路徑為契約）。

**環境變數**：

| 變數 | 預設 | 說明 |
|---|---|---|
| `QUERY_REWRITE_ENABLED` | `1` | 設為 `0` 時 `rewrite_query` 立即回 `[user_message]` 且不呼叫 LLM。 |

**模型**：重用 `PLANNER_MODEL`（D0-3 §2.1），**不**新增專用 config；溫度沿用
`PLANNER_TEMPERATURE`。

**合併規則**：對所有改寫結果，按 `type` 去重、同 `type` 取各 query 下的 max score 作為最終
score（即 "max-score wins"）；之後交給 §9 rerank 階段。

### 9. Reranker（v1.1 新增）

Multi-query 合併後的候選通常多於最終所需 `k`；v1.1 引入可選的 reranker，把候選壓回 top-k。

**契約**：

```python
def rerank(hits: list[DiscoveryHit], query: str, top_k: int) -> list[DiscoveryHit]:
    """If RERANKER_MODEL is unset, identity (return hits[:top_k]).
    Otherwise: call reranker to get relevance scores, return top_k by score."""
```

**實作選項**（spec 不綁定任一；實作者自由選擇）：

- **Cross-encoder**：`RERANKER_MODEL` 字串吻合 `bge-reranker-*` 族時，以 HTTP POST 打
  `{OPENAI_BASE_URL}/rerank`（TEI-compatible）或獨立 reranker 端點；body 含 `(query, documents[])`。
- **LLM-as-reranker**：小 chat model 對每筆 `(query, hit)` 打分並取 top-k。模型 id 可為任一
  chat model（例：`Qwen/Qwen2.5-7B-Instruct`）。

**環境變數**：

| 變數 | 預設 | 說明 |
|---|---|---|
| `RERANKER_MODEL` | `` (空) | 空=停用（identity）；非空則啟用。見 D0-3 §2.1。 |
| `RERANKER_CANDIDATES_MULTIPLIER` | `3` | rerank 前要先從 store 取回多少候選 = `k * multiplier`。 |

**整合後的 `search_discovery` pipeline**：

```
user_query
  → rewrite_query(user_query)                           # §8
  → for each q in rewrites:
        store.query(embed(q), n=k * RERANKER_CANDIDATES_MULTIPLIER)
  → merge by type, max-score wins                       # §8
  → rerank(candidates, user_query, top_k=k)             # §9
  → return list[DiscoveryHit] of length ≤ k
```

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
- [ ] `EMBED_PROMPT_PROFILE=bge` 搭配 `EMBED_MODEL=BAAI/bge-m3` 時，§6 原 4 條驗收查詢 + 新增 2 條（webhook→Slack、urgent 分支）皆能在 top-3 命中預期 type。
- [ ] `RERANKER_MODEL=""` 時 `rerank` 為 identity（回傳 `hits[:top_k]` 且順序不變）；`RERANKER_MODEL` 非空時，對 20 筆合成測試 input 能把相關者前移，Recall@3 不低於 identity baseline。
- [ ] `search_templates_by_types(['n8n-nodes-base.httpRequest','n8n-nodes-base.googleSheets'], 1)` 回傳的 template 其 `node_types` 實際同時包含此兩型。
- [ ] `filter_by_coverage` 不 drop 任何 hit，輸入與輸出 hit set 完全相同；僅重排順序。
- [ ] `QUERY_REWRITE_ENABLED=0` 時 `search_discovery` 走舊路徑（直接對原句 embed），不觸發任何 planner LLM 呼叫。
- [ ] 切換 `EMBED_PROMPT_PROFILE` 各 profile 值（含 `auto`、`bge`、`openai`、`embeddinggemma`、`none`）後 `scripts/bootstrap_rag.py --force` 皆能成功完成。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版 |
| v1.1.0 | 2026-04-21 | 新增 embedding prompt profiles、query rewrite、reranker、workflow_templates collection、has_detail-aware 降級路徑 |
