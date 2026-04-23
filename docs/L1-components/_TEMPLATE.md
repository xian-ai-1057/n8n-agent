# Spec Template — C1-4 Validator 條目寫作範本

> 本檔是 `spec-guardian` 起草新 spec 條目時的**寫作範本**。
> 複製此格式到實際的 `docs/L1-components/C1-*.md`,替換 placeholder。
> 好的 spec 條目能直接讓 Sonnet engineer 實作出正確代碼。

---

## 🟢 完整範例(V-PARAM-20)

以下是一個 spec-guardian 寫得好的 spec 條目完整樣貌:

---

### V-PARAM-20: 禁止空陣列參數

**Statement**

Validator 必須拒絕任何 `parameters` 欄位為空陣列 `[]` 的 node,回傳 `V-PARAM-20` 錯誤碼。此規則避免 builder 在後續組裝 n8n workflow 時產生無效 node(n8n 後端會吐 runtime error)。

**Rationale**

n8n 的 node parameters 即使是空內容,schema 上也該是 `{}`(物件)而非 `[]`(陣列)。使用者輸入或 LLM 生成過程有時會混淆兩者,validator 是最後一道防線。

**Affected files**

| 檔案 | 動作 |
|------|------|
| `backend/app/agent/validator_node.py` | 新增 `check_non_empty_parameters` function |
| `backend/app/models/validation.py` | 在 `ErrorCode` enum 加入 `V_PARAM_20` |
| `backend/tests/test_validator.py` | 新增 test cases(見下方 Test scenarios) |
| `tests/eval/prompts.yaml` | 新增 1 個 e2e eval case |

**Function signature**

```python
# C1-4:V-PARAM-20
def check_non_empty_parameters(node: dict) -> ValidationResult:
    """Reject nodes whose parameters field is an empty array.

    Args:
        node: A dict representing one n8n node, must contain 'id' key.

    Returns:
        ValidationResult with .ok=False and .code=V_PARAM_20 if parameters
        is an empty list []. Returns .ok=True otherwise. None is NOT handled
        here — see V-PARAM-21 for None semantics.

    Raises:
        KeyError: If node dict lacks 'id' key (caller responsibility).
    """
```

**Input / Output examples**

| Input | Expected Output | 說明 |
|-------|-----------------|------|
| `{"id": "node_1", "parameters": {"key": "v"}}` | `ValidationResult(ok=True)` | 正常有效 |
| `{"id": "node_1", "parameters": {}}` | `ValidationResult(ok=True)` | 空 dict 合法 |
| `{"id": "node_1", "parameters": []}` | `ValidationResult(ok=False, code=V_PARAM_20, node_id="node_1")` | ❌ 本規則目標 |
| `{"id": "node_1", "parameters": None}` | (N/A — 由 V-PARAM-21 處理) | None 不在本規則範圍 |
| `{"id": "node_1"}` (缺欄位) | (N/A — 由 V-PARAM-01 處理) | 缺欄位不在本規則範圍 |

**Error message format**

```
V-PARAM-20: Node '{node_id}' has empty array as parameters (expected object or non-empty)
```

**Test scenarios** (給 test-engineer)

| 測試名 | 情境 | 預期 |
|--------|------|------|
| `test_v_param_20_rejects_empty_array` | parameters = [] | fail with V_PARAM_20 |
| `test_v_param_20_passes_with_valid_dict` | parameters = {"k":"v"} | pass |
| `test_v_param_20_passes_with_empty_dict` | parameters = {} | pass |
| `test_v_param_20_ignores_none_params` | parameters = None | pass(本規則不 handle,交給 V-PARAM-21) |
| `test_v_param_20_e2e_rejects_in_full_pipeline` | 整條 pipeline 含違規 node | pipeline 在 validator 階段中止 |

**Security note** (C1-8)

N/A — 此規則是資料完整性,不涉及授權或注入。

**Related rules**

- `V-PARAM-21`: 處理 `parameters: None` 的情況
- `V-PARAM-01`: 處理 `parameters` 欄位完全缺失的情況

**Introduced in**: 2026-04-22
**Last modified**: 2026-04-22

---

## 🔴 反面範本(不該這樣寫)

以下是 spec-guardian 絕對不該寫出的條目:

```markdown
### V-PARAM-20: 檢查參數

驗證參數要正確。 ❌ Statement 太抽象

實作在 validator_node.py。 ❌ 沒給 function signature、examples、test scenarios
❌ 沒有 error message 格式定義
❌ 沒有邊界條件釐清(None vs [])
```

Sonnet engineer 拿到這種 spec 會:
1. 猜「正確」是什麼意思 → 實作出與預期不同的行為
2. 自創 error message 格式 → 與其他規則不一致
3. 沒意識到要跟 V-PARAM-21 區分 → 重疊邏輯

結果 spec-guardian 的最終 review 會駁回,重派修正,浪費 2-3 輪迭代。

---

## 欄位檢查清單

spec-guardian 起草完後自查:

- [ ] **Statement** 用一句話說清楚規則做什麼(不是抽象描述)
- [ ] **Rationale** 說明「為何需要此規則」(1-2 句即可)
- [ ] **Affected files** 列出具體 path,不是模糊描述
- [ ] **Function signature** 含完整 type hints 與 docstring 骨架
- [ ] **Input/Output examples** 至少 3 組(1 正 1 反 + 1 邊界)
- [ ] **Error message format** 明確定義(讓所有 error 格式一致)
- [ ] **Test scenarios** 至少 3 個(happy + edge + e2e)
- [ ] **Security note** 明確寫 N/A 或具體影響
- [ ] **Related rules** 列出會互動的其他規則
- [ ] **時間戳** 有 Introduced / Last modified

---

## 其他 component 的範本變體

### C1-2 Planner (Prefix: `P-`)
重點欄位: **State transitions**、**LLM prompt snippet**、**Expected JSON schema**

### C1-3 Builder (Prefix: `B-`)
重點欄位: **n8n node type mapping**、**parameter transformation rules**

### C1-5 API Contract (Prefix: `A-`)
重點欄位: **HTTP method/path**、**Request schema**、**Response schema**、**Error codes**

### C1-6 Frontend (Prefix: `U-`)
重點欄位: **Affected components**、**Session state changes**、**API interactions**、**User action → expected behavior**

### C1-7 RAG (Prefix: `R-`)
重點欄位: **Collection name**、**Chunk strategy**、**Embedding model**、**Retrieval params**

### C1-8 Security (Prefix: `S-`)
重點欄位: **Threat model**、**Mitigation**、**Test scenarios**(含 attack vectors)

---

## 存放位置

將此檔放在 `docs/L1-components/_TEMPLATE.md`,spec-guardian 每次起草新條目前先讀一次範本。不要實際被 traceability-audit 掃到(檔名以 `_` 開頭,audit 會跳過)。
