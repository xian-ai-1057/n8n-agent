# D0-2：Data Model（SSOT）

> **版本**: v1.0.0 ｜ **狀態**: Draft ｜ **前置**: D0-1

## Purpose

本 spec 為 MVP 全部 Pydantic 模型的 Single Source of Truth。Phase 1-B 會把下列程式碼區塊**原樣複製**到 `backend/app/models/`，因此每個區塊都必須是 **語法有效的 Python 3.11 + Pydantic v2**。後續 spec 引用模型時一律以本檔為準。

## Inputs

- D0-1 已切分之元件與資料流
- R2-1 n8n workflow schema（反向約束 BuiltNode / Connection 的欄位）
- archive `99_Archive/n8n_Agent/src/models/*.py`（欄位取捨參考）

## Outputs

- 一組可匯入的 Pydantic 模型（planning / build / workflow / validation / agent / api）。
- 欄位型別、預設值、validator 與簡短 docstring。

## Contracts

### 1. 設計原則

- Pydantic v2 BaseModel；欄位 snake_case。
- 可選欄位以 `| None = None` 明示。
- ID / 列舉一律用 `StrEnum` / `str`（UUID v4 字串）。
- 所有 docstring 以英文撰寫（避免 JSON schema 傳給 LLM 時混用多語干擾）。
- 檔案切分建議（Phase 1-B 實作時）：
  - `models/enums.py` — StrEnum
  - `models/planning.py` — StepPlan
  - `models/node.py` — NodeCandidate, NodeCatalogEntry, NodeDefinition, NodeParameter
  - `models/workflow.py` — BuiltNode, Connection, WorkflowDraft
  - `models/validation.py` — ValidationIssue, ValidationReport
  - `models/agent.py` — AgentState
  - `models/api.py` — ChatRequest, ChatResponse

### 2. Enums

```python
# models/enums.py
from enum import StrEnum


class StepIntent(StrEnum):
    TRIGGER = "trigger"
    ACTION = "action"
    CONDITION = "condition"
    TRANSFORM = "transform"
    OUTPUT = "output"


class ConnectionType(StrEnum):
    MAIN = "main"
    AI_LANGUAGE_MODEL = "ai_languageModel"
    AI_MEMORY = "ai_memory"
    AI_TOOL = "ai_tool"


class ValidationSeverity(StrEnum):
    ERROR = "error"      # blocks deploy
    WARNING = "warning"  # non-blocking
```

### 3. Planning（Planner 輸出）

```python
# models/planning.py
from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from .enums import StepIntent


class StepPlan(BaseModel):
    """A single step produced by the Planner from user intent + discovery RAG hits."""

    step_id: str = Field(..., description="Stable id within the plan, e.g. 'step_1'.")
    description: str = Field(..., max_length=200, description="Natural-language step description.")
    intent: StepIntent = Field(..., description="Coarse intent classification.")
    candidate_node_types: list[str] = Field(
        ...,
        min_length=1,
        max_length=5,
        description="Ranked n8n node types the Builder may choose from (e.g. 'n8n-nodes-base.httpRequest').",
    )
    reason: str = Field(..., max_length=300, description="Why these candidates match.")

    @field_validator("candidate_node_types")
    @classmethod
    def _types_nonempty(cls, v: list[str]) -> list[str]:
        if any(not t.strip() for t in v):
            raise ValueError("candidate_node_types must not contain empty strings")
        return v
```

### 4. Node Catalog（RAG 資料來源）

```python
# models/node.py
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field


class NodeCatalogEntry(BaseModel):
    """Discovery-level entry — one row from xlsx, no parameter schema."""

    type: str = Field(..., description="Canonical n8n node type, e.g. 'n8n-nodes-base.slack'.")
    display_name: str
    category: str = Field(..., description="e.g. 'Core Nodes', 'Communication'.")
    description: str
    default_type_version: float | None = Field(
        default=None, description="Latest known typeVersion; may be filled during ingest."
    )


class NodeParameter(BaseModel):
    """One parameter of a detailed NodeDefinition."""

    name: str
    display_name: str | None = None
    type: Literal[
        "string", "number", "boolean", "options", "multiOptions",
        "collection", "fixedCollection", "json", "color", "dateTime",
    ]
    required: bool = False
    default: Any = None
    description: str | None = None
    options: list[dict[str, Any]] | None = None


class NodeDefinition(BaseModel):
    """Detailed node schema — source for Builder parameter filling."""

    type: str
    display_name: str
    description: str
    category: str
    type_version: float
    parameters: list[NodeParameter] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list, description="Credential type names, empty in MVP.")
    inputs: list[str] = Field(default_factory=lambda: ["main"])
    outputs: list[str] = Field(default_factory=lambda: ["main"])


class NodeCandidate(BaseModel):
    """Builder-time bundle: a StepPlan combined with the chosen NodeDefinition."""

    step_id: str
    chosen_type: str
    definition: NodeDefinition | None = Field(
        default=None, description="None means type was found in discovery only; Builder may emit an empty shell."
    )
```

### 5. Workflow（Builder / Assembler 輸出）

```python
# models/workflow.py
from __future__ import annotations
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field, field_validator
from .enums import ConnectionType


class BuiltNode(BaseModel):
    """A node ready to be assembled into n8n workflow JSON. Aligns with R2-1."""

    id: str = Field(default_factory=lambda: str(uuid4()), description="UUID v4.")
    name: str = Field(..., description="Unique within the workflow; used as connections key.")
    type: str = Field(..., description="e.g. 'n8n-nodes-base.httpRequest'.")
    type_version: float = Field(..., alias="typeVersion")
    position: list[float] = Field(..., description="[x, y]; see C1-1 for layout rule.")
    parameters: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] | None = None
    disabled: bool | None = None
    on_error: str | None = Field(default=None, alias="onError", description="Replaces deprecated continueOnFail.")
    execute_once: bool | None = Field(default=None, alias="executeOnce")
    retry_on_fail: bool | None = Field(default=None, alias="retryOnFail")
    notes: str | None = None
    notes_in_flow: bool | None = Field(default=None, alias="notesInFlow")

    model_config = {"populate_by_name": True}

    @field_validator("position")
    @classmethod
    def _position_is_xy(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("position must be [x, y]")
        return v


class Connection(BaseModel):
    """One directed edge in n8n connections map. Source is by node NAME (not id)."""

    source_name: str
    source_output_index: int = 0
    target_name: str
    target_input_index: int = 0
    type: ConnectionType = ConnectionType.MAIN


class WorkflowDraft(BaseModel):
    """Assembler output — pre-deploy representation. Serialised to n8n JSON by the client."""

    name: str = Field(..., max_length=128)
    nodes: list[BuiltNode]
    connections: list[Connection] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=lambda: {"executionOrder": "v1"})
```

### 6. Validation

```python
# models/validation.py
from __future__ import annotations
from pydantic import BaseModel, Field
from .enums import ValidationSeverity


class ValidationIssue(BaseModel):
    rule_id: str = Field(..., description="See C1-4 rule table, e.g. 'V-NODE-001'.")
    severity: ValidationSeverity
    message: str
    node_name: str | None = None
    path: str | None = Field(default=None, description="Dotted path into the draft, e.g. 'nodes[3].parameters.url'.")


class ValidationReport(BaseModel):
    ok: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> "ValidationReport":
        errs = [i for i in issues if i.severity == ValidationSeverity.ERROR]
        warns = [i for i in issues if i.severity == ValidationSeverity.WARNING]
        return cls(ok=len(errs) == 0, errors=errs, warnings=warns)
```

### 7. Agent State（LangGraph）

```python
# models/agent.py
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from .planning import StepPlan
from .node import NodeCandidate
from .workflow import BuiltNode, Connection, WorkflowDraft
from .validation import ValidationReport


class AgentState(BaseModel):
    """LangGraph shared state. Each node reads/writes a subset — see C1-1."""

    # input
    user_message: str

    # planner
    plan: list[StepPlan] = Field(default_factory=list)
    discovery_hits: list[dict[str, Any]] = Field(default_factory=list)

    # builder
    candidates: list[NodeCandidate] = Field(default_factory=list)
    built_nodes: list[BuiltNode] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)

    # assembler
    draft: WorkflowDraft | None = None

    # validator
    validation: ValidationReport | None = None
    retry_count: int = 0

    # deployer
    workflow_id: str | None = None
    workflow_url: str | None = None

    # diagnostics
    messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Role/content tuples. Validator errors are appended here before retry.",
    )
    error: str | None = None
```

### 8. API models

```python
# models/api.py
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from .validation import ValidationIssue


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    ok: bool
    workflow_url: str | None = None
    workflow_id: str | None = None
    workflow_json: dict[str, Any] | None = None
    retry_count: int = 0
    errors: list[ValidationIssue] = Field(default_factory=list)
    error_message: str | None = None
```

## Errors

- Pydantic v2 的 `ValidationError` 會在 LLM 結構化輸出或 API 請求入口被捕捉；
  - LLM 失敗 → 由 C1-1 節點重試或回傳 `PlanningError` / `BuildingError`。
  - API 入口失敗 → FastAPI 回 422。
- `BuiltNode.position` 若非 `[x, y]` 會 raise，屬於 Builder 內部 bug（C1-1 retry 不救）。

## Acceptance Criteria

- [ ] 每個 code block 可在 `python -c "from pydantic import BaseModel; exec(open(...).read())"` 環境 import 成功。
- [ ] Phase 1-B 能逐檔複製到 `backend/app/models/`，只需補 `__init__.py`。
- [ ] 所有欄位名稱與 R2-1 的 n8n JSON 欄位（或 alias）一致。
- [ ] StrEnum 值與 C1-4 rule 訊息模板使用的字串一致。
- [ ] `WorkflowDraft.settings` 預設 `{"executionOrder": "v1"}`，對齊 plan。
