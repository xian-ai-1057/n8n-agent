# D0-2：Data Model（SSOT）

> **版本**: v1.1.0 ｜ **狀態**: Draft ｜ **前置**: D0-1

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
  - `models/template.py` — WorkflowTemplate（v1.1 新增）
  - `models/workflow.py` — BuiltNode, Connection, WorkflowDraft
  - `models/validation.py` — ValidationIssue, ValidationReport
  - `models/critic.py` — CriticConcern, CriticReport（v1.1 新增）
  - `models/agent.py` — AgentState
  - `models/api.py` — ChatRequest, ChatResponse, BuilderStepOutput, ConnectionsOutput, BuilderOutput(deprecated)

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
    has_detail: bool = Field(
        default=False,
        description="Whether a detailed NodeDefinition exists for this type; see R2-2 v1.1.",
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
    schema_hint: Literal[
        "url", "cron", "node_id", "expression", "credential_ref",
        "email", "datetime", "secret", "resource_locator",
    ] | None = Field(
        default=None,
        description="Semantic hint for the Builder / Critic; allowlist authoritative in R2-2 v1.1.",
    )


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

### 4a. Workflow Template（RAG 來源；R2-4）

```python
# models/template.py
from __future__ import annotations
from pydantic import BaseModel


class WorkflowTemplate(BaseModel):
    """A reusable workflow example ingested from R2-4 sources (e.g. n8n.io templates)."""

    template_id: str
    name: str
    description: str
    category: str
    use_case: str | None = None
    node_types: list[str]
    workflow_json: dict    # full n8n workflow, R2-1 shape
```

參考：R2-4 描述 ingest 與 provenance；本模型僅規範記憶體形式。

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
from typing import Literal
from pydantic import BaseModel, Field
from .enums import ValidationSeverity


class ValidationIssue(BaseModel):
    rule_id: str = Field(..., description="See C1-4 rule table, e.g. 'V-NODE-001'.")
    rule_class: Literal[
        "structural", "catalog", "topology", "parameter", "security"
    ] = Field(..., description="Rule family; see C1-4 v1.1. Required — breaking change vs v1.0.")
    severity: ValidationSeverity
    message: str
    node_name: str | None = None
    path: str | None = Field(default=None, description="Dotted path into the draft, e.g. 'nodes[3].parameters.url'.")
    suggested_fix: str | None = Field(
        default=None, description="Optional human-readable remediation hint (C1-4 v1.1)."
    )


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

### 6a. Critic（C1-7）

```python
# models/critic.py
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class CriticConcern(BaseModel):
    """One concern raised by the Critic over a draft. See C1-7."""

    severity: Literal["block", "warn"]
    node_name: str | None = None
    field: str | None = None
    rule: Literal[
        "empty_required_param", "placeholder_value", "intent_mismatch",
        "unbound_credential", "unreachable_node", "implausible_schedule",
        "wrong_http_method", "missing_auth_on_external_call",
    ]
    message: str
    suggested_fix: str


class CriticReport(BaseModel):
    """Aggregate Critic verdict for one validation round. See C1-7."""

    ok: bool = Field(alias="pass")    # pass is a Python keyword; expose via alias
    concerns: list[CriticConcern] = Field(default_factory=list)
    latency_ms: int = 0

    model_config = {"populate_by_name": True}
```

### 7. Agent State（LangGraph）

```python
# models/agent.py
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
from .planning import StepPlan
from .node import NodeCandidate
from .template import WorkflowTemplate
from .workflow import BuiltNode, Connection, WorkflowDraft
from .validation import ValidationReport
from .critic import CriticReport


class AgentState(BaseModel):
    """LangGraph shared state. Each node reads/writes a subset — see C1-1 v2.0."""

    # input
    user_message: str

    # planner
    plan: list[StepPlan] = Field(default_factory=list)
    discovery_hits: list[dict[str, Any]] = Field(default_factory=list)

    # templates (C1-2 v1.1 + R2-4)
    templates: list[WorkflowTemplate] = Field(default_factory=list)

    # HITL (C1-1 v2.0 §5)
    plan_approved: bool = False
    session_id: str | None = None

    # builder
    candidates: list[NodeCandidate] = Field(default_factory=list)
    built_nodes: list[BuiltNode] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    # per-step builder loop (C1-1 v2.0 §2.3)
    current_step_idx: int = 0

    # assembler
    draft: WorkflowDraft | None = None

    # validator
    validation: ValidationReport | None = None
    retry_count: int = 0

    # critic (C1-7)
    critic: CriticReport | None = None

    # retry routing (C1-1 v2.0 §2.7)
    fix_target: Literal["planner", "builder"] | None = None

    # deployer
    workflow_id: str | None = None
    workflow_url: str | None = None

    # diagnostics
    messages: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Role/content tuples. role ∈ "
            "{'user','planner','builder','validator','critic',"
            "'router','hitl','deployer','system'}. "
            "Validator errors are appended here before retry."
        ),
    )
    error: str | None = None

    model_config = {"extra": "ignore"}
```

> `messages[*].role` 的合法值集合：`"user" | "planner" | "builder" | "validator" | "critic" | "router" | "hitl" | "deployer" | "system"`。新角色（critic / router / hitl）由 C1-1 v2.0 與 C1-7 引入。

### 8. API / LLM output wrappers

```python
# models/api.py
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from .validation import ValidationIssue
from .workflow import BuiltNode, Connection


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


# --- LLM structured-output wrappers (C1-1 v2.0 per-step loop) ---

class BuilderStepOutput(BaseModel):
    """Single-node Builder call output. Replaces BuilderOutput in new graph."""

    node: BuiltNode


class ConnectionsOutput(BaseModel):
    """Connections-only output from the Assembler / connection phase."""

    connections: list[Connection]


class BuilderOutput(BaseModel):
    """DEPRECATED (v1.0). Batch output of all nodes + connections.

    Retained for backwards-compat reference only; new graph uses
    BuilderStepOutput + ConnectionsOutput. See C1-1 v2.0 §2.3.
    """

    nodes: list[BuiltNode]
    connections: list[Connection] = Field(default_factory=list)
```

## Errors

- Pydantic v2 的 `ValidationError` 會在 LLM 結構化輸出或 API 請求入口被捕捉；
  - LLM 失敗 → 由 C1-1 節點重試或回傳 `PlanningError` / `BuildingError`。
  - API 入口失敗 → FastAPI 回 422。
- `BuiltNode.position` 若非 `[x, y]` 會 raise，屬於 Builder 內部 bug（C1-1 retry 不救）。

### v1 → v1.1 相容性

- `AgentState` 新欄位（`plan_approved`, `session_id`, `current_step_idx`, `templates`, `critic`, `fix_target`）皆有 default；v1 構造器 `AgentState(user_message="x")` 繼續可用。
- `ValidationIssue.rule_class` 為**必填**，屬**破壞性變更**；所有舊 caller 必須在構造時提供 `rule_class`。`suggested_fix` 為可選。
- `BuilderOutput { nodes, connections }` 保留定義以資參考，但新 graph 不再使用；改以 `BuilderStepOutput` + `ConnectionsOutput` 對應 C1-1 v2.0 的 per-step loop。
- Pydantic schema serialization 對未知欄位採 `extra="ignore"`（v1 未明訂，v1.1 加入於 `AgentState.model_config`）。
- `NodeCatalogEntry.has_detail` 與 `NodeParameter.schema_hint` 皆 default 為 `False` / `None`，對舊 ingest 輸出相容；權威 allowlist 見 R2-2 v1.1。

## Acceptance Criteria

- [ ] 每個 code block 可在 `python -c "from pydantic import BaseModel; exec(open(...).read())"` 環境 import 成功。
- [ ] Phase 1-B 能逐檔複製到 `backend/app/models/`，只需補 `__init__.py`。
- [ ] 所有欄位名稱與 R2-1 的 n8n JSON 欄位（或 alias）一致。
- [ ] StrEnum 值與 C1-4 rule 訊息模板使用的字串一致。
- [ ] `WorkflowDraft.settings` 預設 `{"executionOrder": "v1"}`，對齊 plan。
- [ ] `AgentState(user_message="x")` 可建構，所有新欄位取 default（`plan_approved=False`、`current_step_idx=0`、`templates=[]`、`critic=None`、`fix_target=None`、`session_id=None`）。
- [ ] `ValidationIssue(rule_id="V-TOP-001", rule_class="structural", severity=ValidationSeverity.ERROR, message="...")` 建構成功；省略 `rule_class` 會 raise。
- [ ] `CriticReport.model_validate({"pass": True, "concerns": []})` 成功，且 `report.ok is True`。
- [ ] `WorkflowTemplate` 做 `model_dump()` → `model_validate()` 的 JSON roundtrip 結果一致。

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-20 | 初版 SSOT |
| v1.1.0 | 2026-04-21 | AgentState 加 plan_approved/session_id/current_step_idx/templates/critic/fix_target；ValidationIssue 加 rule_class（破壞性）+ suggested_fix；新增 WorkflowTemplate、CriticConcern、CriticReport、BuilderStepOutput、ConnectionsOutput；NodeCatalogEntry/NodeParameter 擴 R2-2 v1.1 欄位 |
