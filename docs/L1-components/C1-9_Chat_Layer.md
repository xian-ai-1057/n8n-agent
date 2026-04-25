# C1-9：Chat Layer（Chat-first + Tool-triggered Pipeline）

> **版本**: v1.0.1 ｜ **狀態**: Draft ｜ **前置**: C1-1 v2.0.4(HITL), C1-5 v2.0.3, C1-8 v1.0.0 ｜ **Prompts**: 新增 `prompts/chat_system.md`

## Purpose

把現有「每則訊息直跑 7-node LangGraph」改為 **chat-first + tool-triggered**：使用者大部分時間在自然對話,LLM 自行判斷何時呼叫 `build_workflow` / `confirm_plan` 兩個 tool。Chat layer **位於 API 層**(不在 LangGraph 內),AgentState 與 graph.py 結構不動;chat session 與 LangGraph thread 共用 `session_id`。

本層解決三個問題:
1. **避免 LLM 在 ambiguous 輸入時亂跑 graph**(成本 + 體驗)。
2. **與 C1-1 v2.0.2 HITL `await_plan_approval` 協作**:plan 浮出後讓使用者用自然語言 confirm / edit / reject,由 chat LLM 翻譯成 `confirm_plan` tool call。
3. **保留未來 SSE / Redis swap 空間**(MVP 先 one-shot JSON + in-memory dict)。

注意:本層**前提**是 C1-1 v2.0.2 與 C1-5 v2.0.0 的 HITL 已 ship(見對應檔的 `HITL-SHIP-*` 條目)。chat layer 的 `confirm_plan` tool 直接打 `POST /chat/{session_id}/confirm-plan`。

## Inputs

- `ChatRequest`(C1-5;新增 `session_id` 欄位語意,見 CHAT-API-01)
- 環境變數(D0-3 待新增):`CHAT_MODEL` / `CHAT_TEMPERATURE` / `CHAT_MAX_HISTORY` / `CHAT_SESSION_TTL_S` / `CHAT_MAX_SESSIONS`(見 CHAT-CFG-01)
- Keyword YAML config:`backend/app/chat/keywords.yaml`(見 CHAT-KW-01)
- LangGraph `MemorySaver` checkpointer(C1-1 HITL-SHIP-01)— chat layer 透過 tool dispatch 間接讀寫

## Outputs

- `ChatResponse`(C1-5)的 envelope 擴充:新增 `assistant_text`、`tool_calls`、`status` 欄位(見 CHAT-API-01)。
- 一條 chat history(in-memory,per session_id)。
- 結構化 log(見 CHAT-OBS-01)。

## Contracts

### 1. 模組結構

```
backend/app/
└── chat/
    ├── __init__.py
    ├── session_store.py      # CHAT-SESS-01/02/03
    ├── keywords.py           # CHAT-KW-01/02
    ├── keywords.yaml         # CHAT-KW-01 (config)
    ├── tools.py              # CHAT-TOOL-01/02
    ├── dispatcher.py         # CHAT-DISP-01/02/03
    └── prompts/
        └── chat_system.md    # CHAT-DISP-02 base system prompt
```

`backend/app/api/routes.py` 改寫 `POST /chat` 進入點:走 chat dispatcher,而非直接 `run_cli`(CHAT-API-01)。

### 2. Two-gate HITL

| Gate | 位置 | 用途 |
|---|---|---|
| **Gate-1**: pre-planner chat mode | API 層(本 spec) | 自然語言澄清,LLM 判斷何時 call `build_workflow` |
| **Gate-2**: post-planner `await_plan_approval` | LangGraph 內(C1-1) | 結構化 plan review;chat LLM 看 `awaiting_plan_approval` 狀態後翻譯 user 訊息成 `confirm_plan` tool call |

兩 gate 獨立但共用 `session_id`。Chat session(`SessionState`)儲存 chat history + 上次 graph 狀態快照;LangGraph 端的 plan / built_nodes 仍由 `MemorySaver` 持有(以 `session_id` 為 thread_id)。

### 3. Failure modes 總覽

詳見每條 traceability 的 Errors 子節 + §Errors 章。摘要:

| 場景 | 行為 |
|---|---|
| Chat LLM endpoint 掛 | 500,body 含 cause(不 retry) |
| Tool call 參數非法(Pydantic 驗證失敗) | 回傳 tool error,讓 chat LLM 重試或退回澄清 |
| `build_workflow` 進到 graph timeout(180s) | 沿用 C1-1 B-TIMEOUT-01;tool 收到 error dict,chat LLM friendly 告知 |
| `confirm_plan` 打過期 session | 404(由 C1-5 HITL-SHIP-01 處理),tool 翻譯為 friendly 訊息 + 建議重啟 session |
| Keyword false-positive(例:"不要建立") | system prompt 已含 hint,LLM 自行判斷;eval suite 必抓 0 誤觸發 |
| 同 turn 連續 ≥2 次 tool call | 只 honor 第 1 次(CHAT-DISP-01) |
| Session 達 `CHAT_MAX_SESSIONS=500` | log warn 但仍建立(MVP 無 hard eviction) |

### 4. v2+ deferred(明寫不在 scope)

- SSE streaming for chat turns(目前 one-shot JSON;但 SSE event taxonomy 預留 `assistant_delta` / `tool_call_started` / `tool_call_finished` placeholder,見 C1-5 §2 後續更新)
- Redis session backend
- Chat history summarization(v1 硬 drop oldest)
- 多語 keyword(只 zh + en)
- Per-session rate limit(沿用 C1-8 全域)
- 第三顆以上 tool

---

## Traceability entries

### CHAT-SESS-01: SessionState 結構與 lifecycle

**Statement**

定義 in-memory `SessionState` dataclass / Pydantic model 與 `SessionStore` API(`create / get / update / delete`)。`session_id` 既是 chat history key,也作為 LangGraph `MemorySaver` 的 thread_id;兩端**必須**用同一個 id,否則 HITL resume 會找不到 graph 狀態。

`SessionStore` 為 process-local singleton(MVP 單 worker);`get_session_store()` 工廠回傳 lazy-initialised instance。

**Rationale**

把「chat history」與「graph state」用同一 key 串起來是最低成本的整合方式 — chat dispatcher 收訊息 → 認 session_id → 從 SessionStore 拿 history → 呼叫 chat LLM → 若 tool call → 用同 session_id 進 graph(MemorySaver 已存在的 thread_id)。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/session_store.py` | 新增 |
| `backend/app/chat/__init__.py` | export `get_session_store`, `SessionState` |
| `backend/tests/test_chat_session_store.py` | 新增 |

**Function signature**

```python
# backend/app/chat/session_store.py
# C1-9:CHAT-SESS-01
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class SessionState:
    session_id: str                         # ^[A-Za-z0-9_-]{8,64}$ (C1-5 既有)
    history: list[dict[str, str]] = field(default_factory=list)
    # 每筆 {"role": "user"|"assistant"|"tool", "content": str, "tool_call_id"?: str}
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 標記目前 graph 是否在等待 plan approval(供 system prompt 切換 hint)
    awaiting_plan_approval: bool = False
    # Gate-2 觸發後快取的 plan 摘要(讓 chat LLM 不需重打 graph 拿 plan 文字)
    pending_plan_summary: str | None = None

class SessionStore:
    def create(self, session_id: str | None = None) -> SessionState: ...
    def get(self, session_id: str) -> SessionState | None: ...
    def update(self, session: SessionState) -> None: ...        # 更新 updated_at
    def delete(self, session_id: str) -> None: ...
    def gc_expired(self, *, now: datetime | None = None) -> int: ...  # 見 CHAT-SESS-02

def get_session_store() -> SessionStore: ...   # process-local singleton
```

**Examples**

- ✅ `store.create()` → 自動產生 `session_id = uuid4().hex[:16]`,符合 C1-5 pattern
- ✅ `store.create("custom_session_01")` → 直接使用,但需先驗 pattern(invalid 拋 `ValueError`)
- ✅ `store.get("nonexistent")` → `None`(不拋 KeyError;呼叫者判斷)
- ❌ `store.create("ab")` → `ValueError`(< 8 chars)

**Test scenarios** (test-engineer)

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_session_create_with_uuid` | 不帶 id | 回 SessionState,id 16 chars,符合 pattern |
| `test_session_create_with_explicit_id` | 帶合法 id | 回對應 SessionState |
| `test_session_create_invalid_id` | 帶 "ab" | ValueError |
| `test_session_get_returns_none_when_missing` | get 不存在 id | None |
| `test_session_update_advances_updated_at` | update 後 | updated_at > 原值 |
| `test_session_delete_idempotent` | delete 兩次 | 不拋 |

**Security note** — N/A 直接(history 內容 redaction 由 CHAT-SEC-01 處理)。

**Introduced in**: 2026-04-25

---

### CHAT-SESS-02: TTL / GC / max_sessions 行為

**Statement**

SessionStore 採 **lazy TTL GC**:每次 `get` 與 `create` 前,若距上次 GC ≥ 60s,呼叫 `gc_expired()`,刪除所有 `now - updated_at > CHAT_SESSION_TTL_S`(預設 1800s)的 session。**不**啟動背景 thread(避免 worker reload 行為複雜化)。

當 `len(store) >= CHAT_MAX_SESSIONS`(預設 500)且 `create` 被呼叫時:**仍允許建立**,但 `logger.warning("session_store_over_capacity", count=...)`。MVP 不做 LRU eviction;若實際運維需要,留待 v2(此時應換 Redis)。

**Rationale**

- Lazy GC = simplicity(無 thread / scheduler);O(N) 掃過 500 entries < 1ms,可接受。
- `over capacity log warn 仍建立`是設計取捨:hard eviction 容易意外趕跑 active session(LRU/FIFO 都需追蹤額外狀態);MVP 優先「不誤殺」。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/session_store.py` | gc_expired + over-capacity log |
| `backend/app/config.py` | 補 5 個新 env(見 CHAT-CFG-01) |
| `backend/tests/test_chat_session_store.py` | 加 TTL / capacity 測試 |

**Function signature**

```python
# C1-9:CHAT-SESS-02
class SessionStore:
    _last_gc_at: datetime
    _gc_interval_s: float = 60.0

    def _maybe_gc(self) -> None:
        if (datetime.now(timezone.utc) - self._last_gc_at).total_seconds() >= self._gc_interval_s:
            self.gc_expired()

    def gc_expired(self, *, now: datetime | None = None) -> int:
        """Delete sessions whose updated_at older than TTL. Returns deleted count."""
```

**Examples**

- ✅ 建立 session → sleep 1801s(test 用 frozen clock)→ 任一 `get`/`create` 觸發 lazy gc,前者 session 消失
- ✅ 建立第 501 個 session,沒人到期 → log warn 但仍回 SessionState
- ✅ TTL 內持續 update → updated_at 重新 advance,不被 gc

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_session_ttl_expiry` | freeze_time + 1801s | get 回 None |
| `test_session_lazy_gc_throttled` | gc 觸發後 5s 內再呼叫 | 不重跑 gc(spy 確認) |
| `test_session_over_capacity_logs_warn` | 建立第 501 個 | log warn 觸發 + session 仍建立 |
| `test_session_active_not_gced` | TTL 內 update | 不被 gc |
| `test_session_gc_count_returned` | gc 3 個過期 | gc_expired() 回 3 |

**Security note** — N/A

---

### CHAT-SESS-03: Thread safety(RLock)

**Statement**

`SessionStore` 使用 `threading.RLock` 保護內部 `dict`(`create / get / update / delete / gc_expired` 全部加鎖)。RLock 而非 Lock 是因為 `_maybe_gc` 會在持鎖時呼叫 `gc_expired`,需可重入。

**Rationale**

FastAPI uvicorn 即使單 worker 也有 multi-thread executor(`asyncio.to_thread`)會讓多請求平行進 SessionStore。Lock 開銷 < 1µs,不是熱點。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/session_store.py` | RLock + with self._lock |
| `backend/tests/test_chat_session_store.py` | concurrent create test |

**Function signature**

```python
# C1-9:CHAT-SESS-03
import threading
class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self._last_gc_at = datetime.now(timezone.utc)
```

**Examples**

- ✅ 100 thread 同時 `create()` → 100 unique session_id,無 KeyError / race
- ✅ Thread A 在 gc_expired 中、Thread B 呼叫 get → B 阻塞直到 A 結束(<1ms)

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_session_concurrent_create` | 50 thread × 2 create each | 100 unique id, 無例外 |
| `test_session_concurrent_get_during_gc` | gc 中 get | 無 deadlock(timeout 1s 內完成) |

**Security note** — N/A

---

### CHAT-KW-01: Keyword YAML schema 與 loader

**Statement**

Keyword 列表存 `backend/app/chat/keywords.yaml`,schema:

```yaml
# C1-9:CHAT-KW-01
build_keywords:
  zh:
    - 建立 workflow
    - 自動化
    - 排程
    - 抓取
    - 同步
    - ...
  en:
    - build workflow
    - automate
    - schedule
    - sync
    - ...
confirm_keywords:
  zh: ["確認", "對", "好", "可以", "就這樣"]
  en: ["confirm", "yes", "ok", "approve"]
reject_keywords:
  zh: ["不要", "取消", "算了"]
  en: ["cancel", "abort", "nevermind"]
```

Loader 在 process 啟動時 load 一次,cache 到 module 級變數(`_KEYWORDS`)。檔案缺失 → log error + 退到內建 fallback list(每類 1-2 個常見字)以避免 startup crash。

**Rationale**

Substring + case-insensitive matching 在 zh 語境最便宜。把列表獨立成 YAML 讓 ops/PM 可調整,不必動 code。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/keywords.yaml` | 新增 |
| `backend/app/chat/keywords.py` | loader + match function |
| `backend/tests/test_chat_keywords.py` | 新增 |

**Function signature**

```python
# C1-9:CHAT-KW-01
from pathlib import Path
import yaml

_KEYWORDS: dict[str, dict[str, list[str]]] | None = None

def load_keywords(path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """Load keywords.yaml; cache; fallback to builtin on error."""

def get_keywords() -> dict[str, dict[str, list[str]]]:
    """Cached accessor."""
```

**Examples**

- ✅ keywords.yaml 存在且 valid → 回 nested dict
- ✅ keywords.yaml 缺失 → log error + 回 fallback `{"build_keywords": {"zh": ["建立"], "en": ["build"]}, ...}`
- ✅ keywords.yaml 內容非 dict / 缺 key → log error + 回 fallback

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_keywords_load_valid_yaml` | 正常 yaml | 回 nested dict |
| `test_keywords_load_missing_file` | path 不存在 | 回 fallback,log error |
| `test_keywords_load_malformed_yaml` | yaml syntax 錯 | 回 fallback |
| `test_keywords_load_missing_top_key` | 缺 build_keywords | 回 fallback |
| `test_keywords_loader_cached` | load 兩次 | 第二次不讀檔(spy) |

**Security note** — keywords.yaml 由 repo 控管,不接受 user 上傳;path 不從 user input 來。

---

### CHAT-KW-02: Keyword matching 規則

**Statement**

`match_keywords(text: str) -> KeywordHits`:對 user message 做 lowercase + substring 比對,回傳命中的三類關鍵字(build / confirm / reject)分別有哪些 hits。**重要:hit 的結果僅作為 system prompt hint 注入(CHAT-DISP-02),不直接觸發 tool call**;最終 tool call 與否由 chat LLM 決定。

**Rationale**

純規則容易誤觸發("我不想建立 X" 會 hit "建立");把判斷權交回 LLM + few-shot 是 sweet spot。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/keywords.py` | match function |
| `backend/tests/test_chat_keywords.py` | 加測試 |

**Function signature**

```python
# C1-9:CHAT-KW-02
from dataclasses import dataclass

@dataclass
class KeywordHits:
    build: list[str]      # 命中的 build keywords(原樣保留)
    confirm: list[str]
    reject: list[str]

    def has_build(self) -> bool: return bool(self.build)
    def has_confirm(self) -> bool: return bool(self.confirm)
    def has_reject(self) -> bool: return bool(self.reject)

def match_keywords(text: str) -> KeywordHits:
    """Substring + case-insensitive match across zh+en lists."""
```

**Input / Output examples**

| Input | Expected | 說明 |
|---|---|---|
| "幫我建立 workflow 每小時抓 github" | `build=["建立 workflow", "抓"]`, confirm=[], reject=[] | hit |
| "Build a workflow that syncs Slack" | `build=["build workflow", "sync"]` | en hit |
| "我不要建立 workflow" | `build=["建立 workflow"]`, reject=["不要"] | 兩邊都 hit;由 LLM 自行判斷 |
| "今天天氣不錯" | 全空 | no hit |
| "好,確認" | confirm=["好", "確認"] | confirm hit |

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_kw_match_zh_build` | 含 "建立 workflow" | build hit |
| `test_kw_match_en_build` | "build workflow" | build hit |
| `test_kw_match_case_insensitive` | "BUILD WORKFLOW" | build hit |
| `test_kw_match_negation_still_hits` | "不要建立 X" | build + reject 都 hit(LLM 處理) |
| `test_kw_match_empty_string` | "" | 全空 |
| `test_kw_match_no_hit` | "今天天氣" | 全空 |

**Security note** — N/A(僅讀取,不執行)

---

### CHAT-TOOL-01: `build_workflow` tool schema + docstring + error handling

**Statement**

定義 `build_workflow(user_request: str, clarifications: dict[str, str] | None = None) -> dict`,作為 chat LLM 的 tool。內部呼叫 `run_graph_until_interrupt(session_id, user_message)`(打 LangGraph,跑到 `await_plan_approval` 中斷)。Docstring 必須明寫 **"Do NOT call this tool if the user request is ambiguous or you have unresolved questions."**

工具回傳 dict(非 raise),格式:

```python
# success(graph 跑到 plan_approval 中斷)
{"ok": True, "status": "awaiting_plan_approval", "plan_summary": "1. ... 2. ...", "session_id": "..."}
# 直接 deploy(plan_approved=True 在 graph 內 — HITL_ENABLED=0 才會走到)
{"ok": True, "status": "deployed", "workflow_url": "...", "workflow_id": "..."}
# graph 內部錯誤
{"ok": False, "status": "error", "error_category": "building_timeout", "error_message": "..."}
```

`user_request` 不可為空字串(Pydantic `min_length=1`);違反 → tool 直接回 `{"ok": False, "status": "invalid_argument", "error_message": "user_request must not be empty"}`(**不**進 graph)。

**Rationale**

把 graph invocation 包成 tool,讓 LLM 用 OpenAI tool calling 機制呼叫,語意明確。Docstring 是 LLM 真正讀的契約,所以必須白話寫清楚邊界。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/tools.py` | 新增 |
| `backend/app/agent/graph.py` | export `run_graph_until_interrupt(session_id, message)` helper(銜接 HITL-SHIP-01) |
| `backend/tests/test_chat_tools.py` | 新增 |

**Function signature**

```python
# backend/app/chat/tools.py
# C1-9:CHAT-TOOL-01
from pydantic import BaseModel, Field

class BuildWorkflowArgs(BaseModel):
    user_request: str = Field(..., min_length=1, max_length=8000,
                              description="The user's full automation request, restated clearly.")
    clarifications: dict[str, str] | None = Field(
        default=None,
        description="Key-value clarifications gathered during chat (e.g. {'frequency': 'hourly'}).",
    )

BUILD_WORKFLOW_DOCSTRING = """\
Build an n8n workflow from a user request.

Use this ONLY when:
1. The user clearly wants to automate something (build / schedule / sync / fetch / ...).
2. You have enough information: trigger, source, destination, frequency (if any).

Do NOT call this tool if:
- The user is just chatting (greetings, weather, off-topic).
- The request is ambiguous (you still have unresolved questions — ask them first).
- The user has rejected a previous plan and is exploring alternatives without committing.

After this tool returns 'awaiting_plan_approval', present the plan_summary to the user
and wait for their confirmation. Then call confirm_plan with their decision.
"""

def build_workflow_tool(args: BuildWorkflowArgs, *, session_id: str) -> dict: ...
```

**Examples**

- ✅ `BuildWorkflowArgs(user_request="每小時抓 github 存 sheet")` → `{ok:True, status:"awaiting_plan_approval", plan_summary:"...", session_id:"..."}`
- ✅ `BuildWorkflowArgs(user_request="")` → Pydantic ValidationError → tool catches,回 `{ok:False, status:"invalid_argument", ...}`
- ✅ Graph timeout → `{ok:False, status:"error", error_category:"building_timeout", ...}`(B-TIMEOUT-02 prefix 沿用)

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_build_tool_happy_path` | 正常 request,mock graph 中斷在 plan_approval | ok=True, status="awaiting_plan_approval" |
| `test_build_tool_empty_request` | user_request="" | ok=False, status="invalid_argument";graph **未**被呼叫 |
| `test_build_tool_graph_timeout` | mock graph raise BuilderTimeoutError | ok=False, error_category="building_timeout" |
| `test_build_tool_security_blocked` | mock state.error="give_up: ..." V-SEC-001 | ok=False, error_category="give_up" |
| `test_build_tool_session_id_passed_through` | 帶 session_id="abc" | run_graph_until_interrupt 收到同 id |

**Security note** — `user_request` 與 chat history 都會走 C1-8 sanitize_user_message + secret-mask。tool 不繞過 security gate;security gate 在 dispatcher 入口已套用,tool 拿到的已是 sanitized 版本。

---

### CHAT-TOOL-02: `confirm_plan` tool schema + docstring + error handling

**Statement**

定義 `confirm_plan(approved: bool, edits: list[dict] | None, feedback: str | None) -> dict`。內部 HTTP 打 `POST /chat/{session_id}/confirm-plan`(C1-5 v2.0.0 §4 規範,由 HITL-SHIP-01 落地)。Tool 將 `edits`(如 `[{"step_id": "step_2", "candidate_node_types": ["n8n-nodes-base.slack"]}]`)轉成 `edited_plan` payload(merge 原 plan + edits)。

Docstring 必須明寫 **"Call this only when the user has explicitly responded to a plan presented by build_workflow."**

回傳 dict:

```python
# approved=true → graph 跑完,deploy 成功
{"ok": True, "status": "deployed", "workflow_url": "...", "workflow_id": "..."}
# approved=false
{"ok": True, "status": "rejected", "message": "plan rejected; you can refine the request and try again."}
# session 過期
{"ok": False, "status": "session_expired", "error_message": "session ... expired (> 30min); please start over."}
# graph 不在 await_plan_approval(409)
{"ok": False, "status": "stage_mismatch", "current_stage": "..."}
# edited_plan schema 不合(422)
{"ok": False, "status": "invalid_argument", "error_message": "..."}
# 部署失敗(502)
{"ok": False, "status": "deploy_failed", "error_message": "..."}
```

**Rationale**

把 confirm/edit/reject 三條路統一成單一 tool,LLM 不需理解 HTTP 細節。Tool 把 edits 轉成 server 可解析的 `edited_plan` 是因為 LLM 用 partial update 比 full replan 更自然。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/tools.py` | 新增 confirm_plan_tool |
| `backend/app/api/routes.py` | `POST /chat/{sid}/confirm-plan` 落地(見 C1-5 HITL-SHIP-01) |
| `backend/tests/test_chat_tools.py` | 新增 |

**Function signature**

```python
# C1-9:CHAT-TOOL-02
class StepEdit(BaseModel):
    step_id: str
    description: str | None = None
    intent: str | None = None
    candidate_node_types: list[str] | None = None

class ConfirmPlanArgs(BaseModel):
    approved: bool
    edits: list[StepEdit] | None = None  # 僅 approved=True 時生效
    feedback: str | None = Field(default=None, max_length=500)

CONFIRM_PLAN_DOCSTRING = """\
Confirm, edit, or reject a plan that build_workflow returned.

Call this only when:
- The previous tool call was build_workflow returning awaiting_plan_approval.
- The user has explicitly responded ("yes / 確認 / 改 step 2 / 取消 / ...").

Do NOT call this tool:
- Before any plan exists.
- If the user has not given a clear yes/no/edit decision.

Set approved=true for confirmation (with optional edits) or approved=false to reject.
"""

def confirm_plan_tool(args: ConfirmPlanArgs, *, session_id: str) -> dict: ...
```

**Examples**

- ✅ approved=True, edits=None → POST {approved:true} → server 跑完 graph → `{ok:True, status:"deployed", workflow_url:...}`
- ✅ approved=True, edits=[{step_id:"step_2", candidate_node_types:["n8n-nodes-base.slack"]}] → tool 從 SessionStore 拿 cached plan,merge edits,送出 `edited_plan` payload
- ✅ approved=False → `{ok:True, status:"rejected", ...}`
- ❌ session 不存在(C1-5 404) → `{ok:False, status:"session_expired", ...}`
- ❌ stage mismatch(C1-5 409) → `{ok:False, status:"stage_mismatch", current_stage:"build_step_loop"}`

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_confirm_approved_no_edits` | approved=True | server 收到 {approved:true, edited_plan:None} |
| `test_confirm_with_edits_merges_plan` | edits=[...] | server 收到 merged edited_plan |
| `test_confirm_rejected` | approved=False | server 收到 {approved:false},tool 回 status=rejected |
| `test_confirm_session_expired` | server 回 404 | tool 回 status=session_expired |
| `test_confirm_stage_mismatch` | server 回 409 | tool 回 status=stage_mismatch + current_stage |
| `test_confirm_invalid_edits` | server 回 422 | tool 回 status=invalid_argument |
| `test_confirm_edits_with_unknown_step_id` | edits step_id 不在 plan | tool layer reject(回 invalid_argument)前不打 server |

**Security note** — `feedback` 字串會 append 到 chat history,須走 sanitize(C1-8)。`edits` 內 `candidate_node_types` 若含 V-SEC-001 blocklist type,後續 validator 會擋(沿用既有路徑)。

**v1 reconcile note: dual-mode `_merge_edits_into_plan`** (2026-04-25)

`SessionState` v1 只快取 `pending_plan_summary: str`,並未保留結構化 plan。dispatcher v1 也未從 LangGraph checkpointer 撈活躍 plan(見 CHAT-API-01 補充說明)。因此 `confirm_plan_tool._merge_edits_into_plan` 採 **dual mode**:

1. **Merge mode**(`pending_plan` 不為 None,由 caller 帶入):每筆 `StepEdit` 只 patch 對應 `step_id` 既有欄位,未提的欄位保留原 plan 值。其他未編輯的 step 原樣保留。
2. **Standalone mode**(`pending_plan is None`):每筆 edit 必須是「完整 step」(含 `description`、`intent`、`candidate_node_types` 三個必填欄位),否則 tool 回 `{ok:False, status:"invalid_argument"}`。

dispatcher v1 一律以 `pending_plan=None` 呼叫(走 standalone mode),這是 **shipped acceptance**。Follow-up 工作項見 CHAT-API-01 §pending_plan injection,實作後 merge mode 自動啟用。

**Plan 序列化形式**:`build_workflow_tool` 在 `awaiting_plan_approval` 回傳的 `plan` 欄位以 **dict-form**(`p.model_dump()` 的結果)而非 raw `StepPlan` instance,理由:tool result 會被 LLM 序列化成 JSON 後 fed back,dict 比 Pydantic instance 在跨 process / log boundary 更穩。`plan_summary` 仍為 plain string。

---

### CHAT-DISP-01: Dispatcher 主流程

**Statement**

`dispatch_chat_turn(message: str, session_id: str | None) -> ChatTurnResult` 為 chat layer 唯一入口。流程:

1. **Sanitize**: C1-8 `sanitize_user_message(message)`(routes.py 已有,提取共用)。
2. **Resolve session**: 若 `session_id` 帶入,從 SessionStore.get;否則 create 新 session。404 if 帶了 id 但不存在。
3. **Keyword match**: `kws = match_keywords(sanitized)`(CHAT-KW-02)。
4. **Append user msg to history**: `session.history.append({"role":"user","content":sanitized})`;若超 `CHAT_MAX_HISTORY=40` 條,drop oldest 至剛好 40(CHAT-DISP-03)。
5. **Build system prompt**: `system = build_system_prompt(session, kws)`(CHAT-DISP-02)。
6. **Call chat LLM**: 用 `tools=[build_workflow_schema, confirm_plan_schema]`,model=`CHAT_MODEL`,temperature=`CHAT_TEMPERATURE`。
7. **Parse LLM response**:
   - 若無 tool call → 純 assistant 文字回應,append history,回 `ChatTurnResult(assistant_text=..., tool_calls=[])`。
   - 若 ≥1 tool call → **只 honor 第 1 個**(其他 log warn 後丟棄);dispatch 對應 tool function,把 tool result 用 `{"role":"tool", "content": json.dumps(result), "tool_call_id":...}` append 到 history;**再呼叫一次 chat LLM**(讓它把 tool result 整理成 user-facing 文字);把第二次 LLM 回應的 assistant_text 作為最終回應。
8. **Update session.updated_at, awaiting_plan_approval flag, pending_plan_summary**(若 tool 回傳 awaiting_plan_approval)。
9. Return result。

**Rationale**

「LLM call → tool dispatch → LLM call again 整理結果」是標準 OpenAI tool calling pattern。第二次 LLM call 確保使用者收到的是自然語言,不是 raw tool dict。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/dispatcher.py` | 新增主流程 |
| `backend/app/api/routes.py` | `POST /chat` 改打 dispatch_chat_turn |
| `backend/tests/test_chat_dispatcher.py` | 新增 |

**Function signature**

```python
# C1-9:CHAT-DISP-01
from dataclasses import dataclass

@dataclass
class ChatTurnResult:
    session_id: str
    assistant_text: str
    tool_calls: list[dict]   # [{name, args, result_status}, ...] for observability
    status: str              # "chat" | "awaiting_plan_approval" | "deployed" | "rejected" | "error"
    workflow_url: str | None = None
    workflow_id: str | None = None
    error_message: str | None = None

def dispatch_chat_turn(message: str, session_id: str | None) -> ChatTurnResult: ...
```

**Examples**

- ✅ 純閒聊 "Hi" → LLM 不 call tool,assistant_text="Hi! ...",status="chat"
- ✅ "建立 workflow ..." → LLM call build_workflow → tool 回 awaiting_plan_approval → 第二次 LLM "好的,這是計畫:1. ... 你要繼續嗎?",status="awaiting_plan_approval"
- ✅ session.awaiting_plan_approval=True,user 說 "好" → LLM call confirm_plan(approved=True) → tool 回 deployed → 第二次 LLM "已部署!URL: ...",status="deployed",workflow_url 填入
- ❌ LLM 在同 turn 回兩個 tool_call → 只跑第一個,log warn

**Test scenarios**

對應 test plan 的 E2E:

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_dispatch_pure_chat` | "今天天氣" | 不 call tool, status="chat" |
| `test_dispatch_clear_request_calls_build` | "建立 workflow 每小時抓 github 存 sheet" | call build_workflow 一次,status="awaiting_plan_approval" |
| `test_dispatch_ambiguous_no_tool_call` | "我想做點自動化" | LLM 應澄清,不 call tool |
| `test_dispatch_double_tool_call_first_wins` | mock LLM 同 turn 回 2 個 tool_call | 只跑第 1 個 |
| `test_dispatch_confirm_path` | session.awaiting_plan_approval=True + "確認" | call confirm_plan(approved=True) |
| `test_dispatch_reject_path` | "不要了" | call confirm_plan(approved=False),status="rejected" |
| `test_dispatch_invalid_session_id` | 帶不存在的 id | raise 404 |

**Security note** — sanitize 在 step 1 已套用;tool 收到的是 sanitized 版本;chat LLM 不會看到原始未 sanitize 訊息。

---

### CHAT-DISP-02: System prompt 組合

**Statement**

`build_system_prompt(session, kws)` 由三部分組成:

1. **Base prompt**(從 `backend/app/chat/prompts/chat_system.md` 讀取,startup cache):描述 agent 身分、兩個 tool 的存在、何時該用、何時不該用、語氣偏好。
2. **State hint**:
   - 若 `session.awaiting_plan_approval=True`:注入 `<plan_pending>{session.pending_plan_summary}</plan_pending>` + 一段 "The user has been shown the above plan. If their next message is a yes/no/edit, call confirm_plan."
   - 否則:無此區塊。
3. **Keyword hint**(僅當 kws 有 hit):
   - `kws.has_build()` 且 `not session.awaiting_plan_approval` → 注入 `<keyword_hint>The user's message contains build-intent keywords ({hits}). They MAY want to build a workflow, but verify the request is unambiguous before calling build_workflow.</keyword_hint>`
   - `kws.has_confirm()` 且 `session.awaiting_plan_approval` → 注入 `<keyword_hint>The user's message contains confirm keywords ({hits}). If they're confirming the pending plan, call confirm_plan(approved=true).</keyword_hint>`
   - `kws.has_reject()` → 注入對應 hint(reject + awaiting_plan → confirm(false);reject + chat → 純對話)

**重要**:keyword hit **不**跳過 LLM,只是 hint。最終決策權在 LLM。

**Rationale**

把 keyword 當「軟提示」而非「硬路由」可避免誤觸發,同時讓 LLM 在歧義輸入時偏向正確判斷(eval 顯示 precision/recall 都會明顯改善)。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/dispatcher.py` | build_system_prompt |
| `backend/app/chat/prompts/chat_system.md` | 新增 base prompt |
| `backend/tests/test_chat_dispatcher.py` | 加 prompt assembly 測試 |

**Function signature**

```python
# C1-9:CHAT-DISP-02
def build_system_prompt(session: SessionState, kws: KeywordHits) -> str: ...
```

**Examples**

- ✅ `awaiting=False, kws=empty` → 純 base prompt
- ✅ `awaiting=False, kws.build=["建立"]` → base + build hint
- ✅ `awaiting=True, kws.confirm=["好"]` → base + plan_pending block + confirm hint
- ✅ `awaiting=True, kws.reject=["不要"]` → base + plan_pending + reject hint(suggest confirm(false))

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_prompt_base_only` | no awaiting, no kw | 不含任何 `<...>` block |
| `test_prompt_build_hint` | kw.build hit | 含 `<keyword_hint>` 描述 build keywords |
| `test_prompt_plan_pending_block` | awaiting_plan_approval=True | 含 `<plan_pending>...summary...</plan_pending>` |
| `test_prompt_confirm_hint_only_when_awaiting` | kw.confirm hit but not awaiting | confirm hint **不**注入(避免亂觸發 confirm_plan) |
| `test_prompt_reject_with_awaiting_suggests_confirm_false` | kw.reject + awaiting | hint 提示 confirm_plan(approved=false) |

**Security note** — base prompt 內容由 repo 控管;`pending_plan_summary` 來自 graph state,已 sanitize。`<plan_pending>` 包裹避免 plan 內容被當成 system 指令(沿用 C1-8 §1 同思路)。

---

### CHAT-DISP-03: Chat history 截斷

**Statement**

每次 dispatcher append user/assistant/tool message 後,若 `len(session.history) > CHAT_MAX_HISTORY`(預設 40),drop **最舊**的 messages 直到剩 `CHAT_MAX_HISTORY` 條。System message **不**算在 history 內(每次重新 build,不 store)。

特殊處理:**不可在 tool_call 與其對應 tool_result 中間切斷**。若被切點落在這對之間,把這對視為原子單位一起保留或一起丟棄(整對保留優先,即多保留 1 條)。

**Rationale**

40 條足夠覆蓋 ~20 turn 對話(user + assistant 各算 1 條),v1 不做 summarization 簡化實作。tool_call/tool_result pair 切斷會讓 LLM 看到 orphan tool message,部分 provider 直接報 400。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/dispatcher.py` | _truncate_history 函式 |
| `backend/tests/test_chat_dispatcher.py` | 加 truncation 測試 |

**Function signature**

```python
# C1-9:CHAT-DISP-03
def _truncate_history(history: list[dict], *, max_len: int) -> list[dict]:
    """Drop oldest until len <= max_len, preserving tool_call/tool_result pairs."""
```

**Examples**

- ✅ history=42 條,max=40 → drop 最舊 2 條;若第 3 舊是 tool_result 對應到第 2 舊的 assistant tool_call → 整對保留,結果 41 條(微超量,可接受)
- ✅ history=40 條 → no-op

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_truncate_under_limit_noop` | 39 條, max=40 | unchanged |
| `test_truncate_drops_oldest` | 50 條, max=40 | 剩 40 條,前 10 條被 drop |
| `test_truncate_preserves_tool_pair` | 切點在 tool_call/tool_result 之間 | 整對保留(可超量 1 條) |
| `test_truncate_handles_orphan_tool` | history 開頭就是 orphan tool_result(理論上不應發生) | 直接 drop,不拋例外 |

**Security note** — N/A

---

### CHAT-API-01: `POST /chat` 新 request/response schema

**Statement**

`/chat` 在 chat layer 落地後行為改變:

**Request 改動**(C1-5 ChatRequest 擴充):
- 既有 `message`、`hitl`、`session_id`、`deploy` 不變
- **新增**(可選)`session_id`:不再只是 LangGraph thread id,也是 chat session id;client 不帶 → server 產 uuid;client 帶 → 必須符合 C1-5 既有 pattern,不存在則 404(原 v1 是新建,v2.0.0 已是 404 — 沿用)

**Response 改動**(ChatResponse 擴充;**向前相容,v1 client extra="ignore"**):

```python
# 在 C1-5 既有 ChatResponse 上擴充
class ChatResponse(BaseModel):
    # ... 既有欄位 (workflow_url, workflow_id, workflow_json, plan, errors, retry_count, error_message, critic_concerns) ...
    # ↓ NEW (CHAT-API-01)
    session_id: str                       # 永遠回填(client 可取以接續)
    assistant_text: str = ""              # chat LLM 自然語言回應(主要顯示給使用者)
    status: str = "chat"                  # "chat" | "awaiting_plan_approval" | "deployed" | "rejected" | "error"
    tool_calls: list[dict] = Field(       # 觀察用(frontend debug);內容 {name, args_summary, result_status}
        default_factory=list,
    )
```

HTTP status 規則(取代 C1-5 §5 部分):
- 純 chat、awaiting_plan_approval、rejected、deployed 都回 **200**(因為 turn 本身成功)
- 422 仍用於 V-SEC-001 命中 / Pydantic schema 錯
- 404 用於 session_id 帶入但不存在
- 503 / 502 / 429 / 400 沿用 C1-5

`workflow_json` / `workflow_url` 只在 status="deployed" 時有值,其他情況為 None。

**Rationale**

新增的三個欄位是 chat-first 流程的核心;envelope 仍是 ChatResponse 確保 v1 client 可降級(只看 workflow_url 等舊欄位)。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/models/api.py` | ChatResponse 擴充 |
| `backend/app/api/routes.py` | `POST /chat` 改打 dispatcher,組 ChatResponse |
| `backend/tests/test_routes.py` 或 `test_api.py` | 新增/修改 |

**Function signature** — 見上方 schema delta。

**Examples**

- ✅ 純 chat → 200 `{ok:true, status:"chat", assistant_text:"Hi!", session_id:"...", workflow_url:null, ...}`
- ✅ awaiting_plan_approval → 200 `{ok:true, status:"awaiting_plan_approval", assistant_text:"這是計畫...", plan:[...], session_id:"..."}`
- ✅ deployed → 200 `{ok:true, status:"deployed", assistant_text:"已部署!", workflow_url:"https://...", workflow_id:"...", session_id:"..."}`
- ❌ session_id 不存在 → 404 `{error:"session_not_found"}`

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_chat_response_chat_status` | 純閒聊 | 200, status="chat", assistant_text non-empty, workflow_url=None |
| `test_chat_response_awaiting_status` | clear request | 200, status="awaiting_plan_approval", plan non-empty |
| `test_chat_response_deployed_status` | full E2E confirm | 200, status="deployed", workflow_url 填 |
| `test_chat_response_rejected_status` | confirm(false) | 200, status="rejected", error_message="plan_rejected" |
| `test_chat_response_v1_compat` | v1 client 不認新欄位 | 不會 reject(extra fields 由 client 端 ignore) |
| `test_chat_unknown_session_id_404` | 帶不存在 id | 404 |

**Security note** — assistant_text 含 LLM 輸出,套用 C1-8 §4 redaction 規則(REDACT_TRACE 只影響 messages,不影響 assistant_text — assistant_text 是 user-facing,不是 internal trace)。

**v1 reconcile note: pending_plan injection** (2026-04-25)

dispatcher CHAT-DISP-01 §step 6 描述 `make_chat_tools(...)` 接受 `pending_plan` 參數,讓 `confirm_plan` tool 在 merge 模式下保留未編輯 step。**v1 acceptance**:dispatcher 一律以 `pending_plan=None` 呼叫,亦即不從 LangGraph `MemorySaver` checkpointer 重新 hydrate 結構化 plan。理由:

1. 從 checkpointer 讀 plan 需要在 dispatcher 內 import graph internals(`get_state(thread_id)`),增加耦合。
2. v1 未要求 partial edit 必須 reuse 既有 step;LLM 在 chat history 內已看過 plan_summary,可以重新生成完整 step list 走 standalone mode。
3. SessionState 加 `pending_plan: list[StepPlan] | None` 欄位是技術上可行的 follow-up,但需與 graph state 同步策略思考(避免 chat session 與 graph state drift)。

**Follow-up**(非 v1 blocker):若 partial-edit UX 不足,從 LangGraph checkpointer 撈最新 plan 注入 tool factory(可加 `dispatcher._fetch_pending_plan(session_id)`,走 `compiled_graph.get_state({"thread_id": session_id}).values["plan"]`),並把 `pending_plan` 傳入 `make_chat_tools`。

**v1 reconcile note: `tool_choice="none"` provider compatibility** (2026-04-25)

dispatcher §step 7b 第二次 LLM invocation 用 `tool_choice="none"` 防止 tool loop。實作以 `try/except (TypeError, ValueError)` 包裹,若 provider(典型:本地 Ollama / 部分自託管 LLM)不支援 `tool_choice`,自動 fallback 至 `llm.invoke(second_messages)`(無 tools binding)。即使 fallback 場景下 LLM 回應誤帶 tool_calls,dispatcher 第二次 invocation 後不再 dispatch tool(只取 `content` 文字),語意安全。

此 fallback 行為為**設計上必要**(不是缺陷):MVP 必須跑得起本地模型。

---

### CHAT-API-02: `POST /chat/{sid}/confirm-plan` 與 tool 的銜接

**Statement**

C1-5 v2.0.0 §4 既有 spec 的 `POST /chat/{session_id}/confirm-plan` endpoint 由 C1-5:HITL-SHIP-01 負責落地。chat layer 的 `confirm_plan_tool`(CHAT-TOOL-02)透過內部 HTTP client(httpx)打這個 endpoint。**不**直接呼叫 graph.resume()(避免 tool 內 import graph internals,維持邊界清晰)。

由於 chat layer 與 endpoint 同 process,httpx 走 `http://localhost:{port}` 太繞;改用 `fastapi.testclient.TestClient` 不適合 prod。**規範:在 dispatcher 注入一個 `confirm_plan_callable`(callable that takes session_id + ConfirmPlanRequest, returns ChatResponse-shape dict)**,prod 由 routes.py 提供 in-process 函式,test 由 mock 提供。Tool 內呼叫此 callable,不繞 HTTP。

**Rationale**

避免 in-process self HTTP call 開銷,同時保留 endpoint 對外可用(供 SSE client / 其他外部呼叫者使用)。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/tools.py` | confirm_plan_tool 改透過注入 callable |
| `backend/app/api/routes.py` | wire `confirm_plan_callable` 並 expose endpoint |
| `backend/app/agent/graph.py` | `resume_graph_with_confirmation(session_id, approved, edited_plan)` helper(由 HITL-SHIP-01 提供) |
| `backend/tests/test_chat_tools.py` | 加 callable injection test |

**Function signature**

```python
# C1-9:CHAT-API-02
ConfirmPlanCallable = Callable[[str, ConfirmPlanRequest], dict]

def confirm_plan_tool(
    args: ConfirmPlanArgs,
    *,
    session_id: str,
    confirm_callable: ConfirmPlanCallable,
) -> dict: ...
```

**Examples**

- ✅ Prod: routes.py 提供 `lambda sid, req: _do_confirm_plan(sid, req)` 直接走 in-process
- ✅ Test: mock callable 回 `{"ok":True, "status":"deployed", ...}`

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_confirm_tool_uses_injected_callable` | mock callable 計次 | callable 被呼叫一次,參數正確 |
| `test_confirm_endpoint_and_tool_share_logic` | 同一 session_id 從 endpoint 與 tool 入口都能 confirm | 行為一致(integration test) |

**Security note** — 既然不繞 HTTP,C1-8 rate limit 只會在外部 endpoint 路徑套用;內部 tool 呼叫不限速。但因 chat LLM 本身受 chat-level rate limit 保護,使用者沒有額外攻擊面。

---

### CHAT-OBS-01: 結構化 log 欄位

**Statement**

每個 chat turn 在 dispatcher 進出各 log 一筆 JSON 結構,欄位:

| 欄位 | 來源 |
|---|---|
| `event` | `"chat_turn_start"` / `"chat_turn_end"` |
| `chat_turn_id` | uuid4 hex(16 chars);per turn 唯一 |
| `session_id` | 來自 request |
| `request_id` | 來自 `request_id_var`(既有 contextvar) |
| `keyword_hits` | `{build: int, confirm: int, reject: int}`(每類 hit 數量,**不**記字串內容,避免敏感資料外洩) |
| `tool_calls` | `[{name, status, latency_ms}, ...]` |
| `latency_ms` | turn 總時間 |
| `tokens_prompt` / `tokens_completion` | LLM 提供時填,缺則 omit |
| `chat_history_len_before` / `_after` | history 長度 |
| `awaiting_plan_approval` | bool(turn 結束時的 session 狀態) |

`logger.info("chat_turn_end", extra={...})`。

**Rationale**

eval harness(D0-5)會吃這些欄位算 precision/recall;ops 用來看 turn 平均 latency。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/chat/dispatcher.py` | log emit |
| `backend/tests/test_chat_dispatcher.py` | 用 caplog 驗欄位 |

**Function signature** — 無新函式,既有 logger。

**Examples**

```json
{"event":"chat_turn_end","chat_turn_id":"abc...","session_id":"...","keyword_hits":{"build":1,"confirm":0,"reject":0},"tool_calls":[{"name":"build_workflow","status":"awaiting_plan_approval","latency_ms":12345}],"latency_ms":12500,"awaiting_plan_approval":true}
```

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_obs_log_chat_turn_end_emitted` | 任一 turn | log 有 `event=chat_turn_end` |
| `test_obs_log_keyword_hits_count_only` | kw hit | log 中 keyword_hits 是 int dict,不含 keyword strings |
| `test_obs_log_tool_call_latency_present` | tool call turn | tool_calls[0].latency_ms is int |

**Security note** — 不 log keyword strings 與 message content(避免 secrets 外洩到 log)。chat history 若需 debug,由 server-side 透過 session_id 查 SessionStore(不進 log file)。

---

### CHAT-SEC-01: Chat history REDACT_TRACE 與 session_id pattern

**Statement**

兩條規則:

1. **REDACT_TRACE 套用到 chat history**:當 `REDACT_TRACE=1`,`/chat` response 不回傳 `tool_calls.args_summary` 與 `assistant_text` 中可能洩漏內部 trace 的部分(具體:tool_calls 完全清空為 `[]`;assistant_text 保留;chat history **不**在 response 裡,本來就不外洩)。預設 `REDACT_TRACE=0`。

2. **session_id pattern validation**:延用 C1-5 既有 `^[A-Za-z0-9_-]{8,64}$` pattern。chat layer 的 SessionStore.create(explicit_id) 與 dispatcher 的 session_id resolve 都先驗 pattern,不合 → 400 `{error:"invalid_session_id"}`(prod 路徑)或 ValueError(內部呼叫)。

**Rationale**

(1) tool_calls 含 args_summary,可能不小心洩 user 訊息 fragments;REDACT 模式下整個清空最安全。(2) session_id 是 LangGraph thread_id,若放任 user 帶 `../../../etc/passwd` 之類,雖然 MemorySaver 用 dict 不會直接路徑遍歷,仍應限制為 safe charset。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/api/routes.py` | response build 時套 REDACT_TRACE |
| `backend/app/chat/session_store.py` | create() 驗 pattern |
| `backend/app/chat/dispatcher.py` | resolve session_id 時驗 pattern |
| `backend/tests/test_chat_security.py` | 新增 |

**Function signature**

```python
# C1-9:CHAT-SEC-01
import re
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

def _validate_session_id(sid: str) -> None:
    if not _SESSION_ID_RE.match(sid):
        raise ValueError(f"invalid session_id: {sid!r}")
```

**Examples**

- ✅ `_validate_session_id("abc12345")` → ok
- ❌ `_validate_session_id("../../etc")` → ValueError
- ❌ `_validate_session_id("ab")` → ValueError(< 8)
- ✅ REDACT_TRACE=1 → response.tool_calls=[]

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_session_id_traversal_rejected` | "../../etc/passwd" | 400 / ValueError |
| `test_session_id_too_short_rejected` | "abc" | reject |
| `test_session_id_special_chars_rejected` | "abc!@#$" | reject |
| `test_redact_trace_clears_tool_calls` | REDACT_TRACE=1 + tool turn | response.tool_calls == [] |
| `test_redact_trace_default_off` | 不設 env | tool_calls 仍正常 |

**Security note** — 此條目本身就是 C1-8 延伸;與 C1-8 §1(injection)、§4(REDACT)同源。chat history 內容若含 secrets,由 C1-8 secret-mask 在入口處遮罩,本層不重複。

---

### CHAT-CFG-01: 5 個新 env vars 規範

**Statement**

新增以下 settings 欄位(`backend/app/config.py`),由 D0-3 environment 規範統一管理:

| Env | Type | Default | 說明 |
|---|---|---|---|
| `CHAT_MODEL` | str | `<PLANNER_MODEL>` | chat LLM model id;未設則 fallback 至 `PLANNER_MODEL` |
| `CHAT_TEMPERATURE` | float | `0.3` | chat LLM temperature(略高於 planner 的 0,允許自然對話) |
| `CHAT_MAX_HISTORY` | int | `40` | dispatcher 截斷上限(see CHAT-DISP-03) |
| `CHAT_SESSION_TTL_S` | int | `1800` | SessionStore TTL 秒(see CHAT-SESS-02) |
| `CHAT_MAX_SESSIONS` | int | `500` | SessionStore over-capacity log 閾值 |

Chat LLM 共用 `OPENAI_BASE_URL` 與 `OPENAI_API_KEY`(沿用既有 LLM factory);**只**換 model id 與 temperature。

**Rationale**

獨立 chat model 讓 ops 可選便宜模型(如 gpt-4o-mini)做 chat 路由,planner / builder 用較強 model。temperature 0.3 是 chat 標準起手值。

**Affected files**

| 檔案 | 動作 |
|---|---|
| `backend/app/config.py` | Settings 加 5 欄位 + `effective_chat_model` property |
| `.env.example` | 補 5 個 env 範例(若有此檔) |
| `backend/tests/test_config.py` | 新增 default 與 override 測試 |
| `docs/L0-decisions/D0-3*.md`(若 spec-guardian 後續維護) | 補表 |

**Function signature**

```python
# C1-9:CHAT-CFG-01
class Settings(BaseSettings):
    # ... 既有欄位 ...
    chat_model: str | None = None
    chat_temperature: float = 0.3
    chat_max_history: int = 40
    chat_session_ttl_s: int = 1800
    chat_max_sessions: int = 500

    @property
    def effective_chat_model(self) -> str:
        return self.chat_model or self.planner_model
```

**Examples**

- ✅ env 不設 → `effective_chat_model == settings.planner_model`
- ✅ env `CHAT_MODEL=gpt-4o-mini` → `effective_chat_model == "gpt-4o-mini"`
- ✅ `CHAT_TEMPERATURE=0.7` → `settings.chat_temperature == 0.7`

**Test scenarios**

| 測試名 | 情境 | 預期 |
|---|---|---|
| `test_chat_model_defaults_to_planner` | 不設 env | effective_chat_model == planner_model |
| `test_chat_model_override` | env 設定 | effective 用 env 值 |
| `test_chat_temperature_default` | 不設 | 0.3 |
| `test_chat_max_history_int` | 設 "20" | int 20 |
| `test_chat_session_ttl_int` | 設 "60" | int 60 |

**Security note** — 5 個 env 都不含 secret;不需 mask in log。

---

## Errors(總表)

| 場景 | 行為 | 對應 ID |
|---|---|---|
| Chat LLM endpoint 掛 | 500,body `{error:"chat_llm_unavailable", detail:"..."}` | CHAT-DISP-01 |
| Tool args Pydantic 失敗 | tool 回 `{ok:False, status:"invalid_argument"}`;chat LLM 收到後重試或澄清 | CHAT-TOOL-01/02 |
| build_workflow 進 graph timeout | sourced from C1-1 B-TIMEOUT-01;tool 回 `{ok:False, error_category:"building_timeout"}` | CHAT-TOOL-01 |
| confirm_plan 打過期 session | C1-5 404;tool 回 `{ok:False, status:"session_expired"}` | CHAT-TOOL-02 |
| session_id 帶入 invalid pattern | 400 `{error:"invalid_session_id"}` | CHAT-SEC-01 |
| Keyword false-positive | 不報錯;由 LLM 自決;eval suite 偵測 | CHAT-KW-02 |
| 同 turn ≥2 tool call | 只 honor 第 1 次,log warn | CHAT-DISP-01 |
| Session over capacity (≥500) | log warn,仍建立 | CHAT-SESS-02 |
| Keyword YAML 缺失/格式錯 | log error,fallback 內建 | CHAT-KW-01 |

---

## Acceptance Criteria

- [ ] **CHAT-SESS-***: SessionStore 可建立/取/更新/刪除 session;TTL 1800s 過期;500 上限 log warn 但仍建立;100-thread concurrent create 無 race。
- [ ] **CHAT-KW-***: keywords.yaml 載入成功;substring + case-insensitive match 正確;match 結果僅 hint,不繞過 LLM。
- [ ] **CHAT-TOOL-***: build_workflow 與 confirm_plan 兩個 tool 的 docstring 含 "Do NOT call ..." 邊界描述;tool 回 dict 不 raise(invalid args / graph error / session expired 都有對應 status)。
- [ ] **CHAT-DISP-***: dispatcher pure-chat / build / confirm / reject 四條路徑全通;同 turn ≥2 tool call 只 honor 第 1 個;history 截斷尊重 tool_call/result pair。
- [ ] **CHAT-API-***: `POST /chat` response 含 `session_id`、`assistant_text`、`status`、`tool_calls` 4 個新欄位;v1 client 不被新欄位 reject;status 有 4 種值。
- [ ] **CHAT-OBS-01**: chat_turn_end log 含必要欄位且 keyword_hits 為 count(非 string)。
- [ ] **CHAT-SEC-01**: session_id pattern 守住;REDACT_TRACE 模式 tool_calls 清空。
- [ ] **CHAT-CFG-01**: 5 個 env 載入正確;chat_model fallback 至 planner_model。
- [ ] **HITL 整合**:E2E-1 ~ E2E-6(test plan)全通;eval 4 套件達標。
- [ ] **Regression**:既有 `python -m app.agent "<prompt>"` CLI 模式不受影響(沿用 hitl_enabled=False 路徑)。

---

## E2E Test Coverage(對應 test plan)

下列 6 條 E2E flow + 4 條 eval suite 在 spec acceptance 中為硬性要求:

**E2E flows** (`backend/tests/test_chat_e2e.py`):
- E2E-1 閒聊→澄清→build→HITL confirm→deploy(golden path)
- E2E-2 clear request→build→HITL→edit→resume→deploy
- E2E-3 build→HITL→reject→session 可續聊
- E2E-4 keyword fast-path:1 turn 直接 build→HITL→deploy
- E2E-5 ambiguous 3 輪不收斂→LLM 不強 call tool
- E2E-6 build 失敗(validator 用盡)→chat LLM friendly 解釋,session 保留

**Eval suites**(`tests/eval/prompts.yaml`):
- `eval_chat_tool_decision_precision`(20 prompts):precision ≥ 0.85, recall ≥ 0.80
- `eval_chat_plan_convergence`(10 ambiguous):平均 ≤ 2 turns 收斂
- `eval_chat_keyword_false_positive`(10 negation):0 誤觸發
- `eval_chat_hitl_confirm_interp`(10 confirm 表達):recall ≥ 0.90

---

## 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0.0 | 2026-04-25 | 初版:chat-first + tool-triggered pipeline,Two-gate HITL。新增模組 backend/app/chat/。Traceability:CHAT-SESS-01..03 / CHAT-KW-01..02 / CHAT-TOOL-01..02 / CHAT-DISP-01..03 / CHAT-API-01..02 / CHAT-OBS-01 / CHAT-SEC-01 / CHAT-CFG-01。前提:C1-1 v2.0.2 與 C1-5 v2.0.0 HITL 必須先 ship(見對應檔的 HITL-SHIP-* 條目)。 |
| v1.0.1 | 2026-04-25 | reconcile review:CHAT-TOOL-02 補「dual-mode `_merge_edits_into_plan`」說明(merge mode + standalone mode)+ 「Plan 序列化形式」(dict-form via `model_dump()`);CHAT-API-01 補「pending_plan injection」follow-up(v1 一律 standalone mode、checkpointer hydration 留待 follow-up)+ 「`tool_choice='none'` provider compatibility」(本地 Ollama 等不支援時 fallback 至無 tools llm.invoke)。前置版號同步至 C1-1 v2.0.4 / C1-5 v2.0.3。 |
