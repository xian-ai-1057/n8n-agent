# CLAUDE.md — n8n-agent 專案開發指引

> 本檔由 Claude Code 主 session 自動讀取,用以決定如何委派 subagent 與回覆使用者。
> 修改本檔會直接影響所有後續 Claude Code 對話的行為。

---

## 專案概要

`n8n-agent` 是一個 conversational n8n workflow builder。技術棧:

- **Backend**: FastAPI + LangGraph 1.1+ + ChromaDB(3 層 RAG)
- **Frontend**: Streamlit
- **核心 pipeline**: 7 個 LangGraph node — planner → builder → connections_linker → assembler → validator → critic → deployer
- **開發方法**: spec-driven development(spec 是 SSOT)

---

## Subagent 團隊與分工

| Subagent | Model | 職責 | 何時呼叫 |
|----------|-------|------|----------|
| `spec-guardian` | Opus | 起草/審查 spec、分配 traceability ID | 每次變更的開頭 + 結尾 |
| `backend-engineer` | Sonnet | 後端代碼實作(預設) | 後端變更,範圍清晰時 |
| `backend-engineer-opus` | Opus | 後端複雜實作 | 多檔重構、模糊需求、escalation |
| `frontend-engineer` | Sonnet | 前端代碼實作 | UI / Streamlit 變更 |
| `test-engineer` | Sonnet | pytest + eval harness | 代碼變更後 |
| `code-reviewer` | Sonnet | 程式碼品質審查(非 spec 面) | 測試完成後、spec-guardian 最終 review 前 |

---

## ⭐ Spec-Driven Workflow(每個變更都遵循)

對任何代碼變更請求,主 session **必須**按此順序委派 subagent,**禁止跳步**:

```
使用者需求
    ↓
[1] spec-guardian (起草階段)
    → 在 docs/L1-components/C1-*.md 加/改條目
    → 分配 traceability ID
    → 給 engineer 具體指引
    ↓
[2] backend-engineer / frontend-engineer (可平行,若 scope 不重疊)
    → 依 spec 實作,加 # C1-x:ID 註解
    → 補基本 unit test
    ↓
[3] test-engineer
    → 補 spec 中列的 test scenarios
    → 跑 pytest 與 eval harness
    ↓
[4] code-reviewer
    → 程式碼品質審查 (style、dead code、error handling、security smells)
    → 發現 blocker → 主 session 重派 engineer 修正,再回 [3]
    → 通過 → 繼續
    ↓
[5] spec-guardian (review 階段)
    → git diff + spec 三方對齊檢查
    → 給結論: ✅ / ⚠️ / ❌
    ↓
[6] 主 session 統一彙整 → 回覆使用者(用下方模板)
```

### 例外處理規則

- **任一步失敗** → 主 session 重派該 agent 修正,**不要把失敗細節直接拋給使用者**,先閉環再回報結論
- **engineer 回報 escalation** → 主 session 用 `backend-engineer-opus` 重派同一任務
- **engineer 回報 missing spec** → 主 session 重派 spec-guardian 補強,再恢復 engineer
- **engineer 回報需要改 API contract** → 主 session 暫停 frontend,先讓 spec-guardian + backend-engineer 處理 API,再恢復 frontend

---

## Model Escalation Rules

`backend-engineer` (Sonnet) 預設使用。**主動升級為 `backend-engineer-opus`** 的條件(任一成立即升級):

1. spec-guardian 在起草時標記任務為「複雜」或「跨多檔」
2. 任務涉及 ≥ 4 個檔案的同步修改
3. 任務描述含「重構」「重新設計」「不確定怎麼做」等關鍵字
4. backend-engineer 主動回報 escalation
5. 同一任務 backend-engineer 已嘗試 2 輪仍未通過 spec-guardian review

**不要為了保險就用 Opus**。Sonnet 對 well-scoped 實作的成功率與 Opus 接近(SWE-bench 差距 ~1-2%),但成本與速度有顯著優勢。Opus 留給真正需要 reasoning depth 的工作。

---

## 任務分類與 routing

### 純代碼問題(沒有 spec 影響)
**例如**: 「這個 import error 怎麼修」「為什麼這個 test 跑不起來」
→ 主 session 直接處理,不委派 subagent。

### 純 spec 諮詢(不改代碼)
**例如**: 「V-PARAM-20 是什麼意思」「C1-4 還有哪些規則沒實作」
→ 主 session 直接讀 docs/L1-components/ 回答,不委派。

### 代碼變更(任何規模)
→ 走完整 spec-driven workflow(上方流程)。
→ **即使是「加一行 log」這種小改動,也要走流程** —— 否則 traceability 會破。
→ 例外:純 typo 修正、純註解修正、純 import reorder 可跳過 spec-guardian。

### Debug / 探索性追蹤
**例如**: 「validator 對某個 input 為什麼回傳錯的結果」
→ 主 session 自己用 Explore subagent 或 grep/read 工具追蹤,釐清根因後再決定是否走完整流程。

---

## 統一回覆模板(主 session 給使用者的最終回覆)

每次完成 spec-driven workflow 後,**用此模板回覆使用者**,不要把各 subagent 的零散輸出原文丟出來:

```markdown
## 需求理解
<一句話複述使用者需求>

## Spec 變動
- 影響 component: C1-x、C1-y
- 新增/修改 traceability ID: <list>
- 變動摘要: <brief>

## 代碼變更
- backend: <files,若有>
- frontend: <files,若有>
- (若無變更則寫「無」)

## 測試結果
- 新增測試: <files>
- 執行結果: ✅ N passed / ❌ <failures>
- Eval harness: <若有>

## Review 結論
code-reviewer: ✅ Clean / ⚠️ <N> should-fix / ❌ <N> blockers
spec-guardian: ✅ Pass / ⚠️ Minor / ❌ Rework needed
<簡短摘要>

## 後續
<若有遺留項或需使用者決策的選項;否則寫「可 commit」>
```

---

## 禁止事項(主 session 自己也要遵守)

- ❌ 不要在沒有 spec 的情況下讓 engineer 開始寫代碼
- ❌ 不要跳過 spec-guardian 的最終 review
- ❌ 不要把 subagent 的失敗報告直接丟給使用者(先閉環)
- ❌ 不要自己改 docs/L1-components/(那是 spec-guardian 的工作)
- ❌ 不要執行 `git commit` 或 `git push`(由使用者最終決定)

---

## 常用環境指令

```bash
# 啟動後端
uv run uvicorn backend.app.main:app --reload --port 8000

# 啟動前端
uv run streamlit run frontend/app.py --server.port 8501

# Docker 全棧
docker compose up -d

# 全套測試
uv run pytest backend/tests/ -v

# Eval harness
uv run python tests/eval/run.py --all

# Lint + type check
uv run ruff check .
uv run mypy backend/app/
```

---

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" → invoke /plan-ceo-review
- Architecture, "does this design make sense" → invoke /plan-eng-review
- Design system, brand, "how should this look" → invoke /design-consultation
- Design review of a plan → invoke /plan-design-review
- Developer experience of a plan → invoke /plan-devex-review
- "Review everything", full review pipeline → invoke /autoplan
- Bugs, errors, "why is this broken", "wtf", "this doesn't work" → invoke /investigate
- Test the site, find bugs, "does this work" → invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" → invoke /review
- Visual polish, design audit, "this looks off" → invoke /design-review
- Developer experience audit, try onboarding → invoke /devex-review
- Ship, deploy, create a PR, "send it" → invoke /ship
- Merge + deploy + verify → invoke /land-and-deploy
- Configure deployment → invoke /setup-deploy
- Post-deploy monitoring → invoke /canary
- Update docs after shipping → invoke /document-release
- Weekly retro, "how'd we do" → invoke /retro
- Second opinion, codex review → invoke /codex
- Safety mode, careful mode, lock it down → invoke /careful or /guard
- Restrict edits to a directory → invoke /freeze or /unfreeze
- Upgrade gstack → invoke /gstack-upgrade
- Save progress, "save my work" → invoke /context-save
- Resume, restore, "where was I" → invoke /context-restore
- Security audit, OWASP, "is this secure" → invoke /cso
- Make a PDF, document, publication → invoke /make-pdf
- Launch real browser for QA → invoke /open-gstack-browser
- Import cookies for authenticated testing → invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks → invoke /benchmark
- Review what gstack has learned → invoke /learn
- Tune question sensitivity → invoke /plan-tune
- Code quality dashboard → invoke /health

Available gstack skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/setup-gbrain`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`

---

## 關鍵檔案路徑速查

```
backend/app/agent/        ← 7 個 LangGraph nodes
backend/app/api/          ← FastAPI routes
backend/app/main.py       ← FastAPI entry
backend/app/rag/          ← 3 層 ChromaDB RAG
backend/app/models/       ← 前後端共享 Pydantic
frontend/app.py           ← Streamlit 主入口
frontend/web/             ← 自訂 web assets
docs/L1-components/       ← spec SSOT (C1-1 到 C1-8)
tests/eval/prompts.yaml   ← evaluation harness
pyproject.toml            ← Python 依賴
docker-compose.yml        ← 全棧編排
```
