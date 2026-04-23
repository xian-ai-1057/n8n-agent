# n8n-agent Claude Code Setup — 部署與使用指引

本套件包含 n8n-agent 專案的完整 Claude Code 開發配置:6 個 subagent + 1 份 CLAUDE.md + 2 個 skill。

---

## 套件內容

```
.claude/
├── agents/
│   ├── spec-guardian.md           ← Opus,起草+審查 spec
│   ├── backend-engineer.md        ← Sonnet,後端實作(預設)
│   ├── backend-engineer-opus.md   ← Opus,後端複雜實作
│   ├── frontend-engineer.md       ← Sonnet,前端實作
│   ├── test-engineer.md           ← Sonnet,測試與 eval
│   └── code-reviewer.md           ← Sonnet,程式碼品質審查
├── scripts/
│   └── git_sync.sh                ← 自動同步腳本
└── skills/
    ├── spec-driven-workflow/
    │   └── SKILL.md               ← 工作流程契約
    └── traceability-audit/
        ├── SKILL.md
        └── scripts/
            └── audit.py           ← 三方一致性稽核腳本

docs/L1-components/
└── _TEMPLATE.md                   ← Spec 條目寫作範本

docs/agent_teams_master_reference.md ← Subagent 設計總覽

CLAUDE.md                           ← 主 session orchestration 規則
README.md                           ← 本檔
```

---

## 部署步驟

### 1. 複製到 n8n-agent 專案

```bash
cd /home/user/n8n-agent

# 複製 agents 與 skills
cp -r /path/to/this-package/.claude/* .claude/

# 複製 CLAUDE.md(若已存在,看下方「合併既有 CLAUDE.md」)
cp /path/to/this-package/CLAUDE.md ./
```

### 2. 確認 .claude/ 結構

```bash
ls -la .claude/agents/
# 應該看到 6 個 .md 檔

ls -la .claude/skills/
# 應該看到 spec-driven-workflow 與 traceability-audit

ls docs/L1-components/_TEMPLATE.md
# 應該存在(spec-guardian 寫 spec 時的範本)
```

### 3. 驗證 Claude Code 識別 subagent

```bash
claude
# 在 Claude Code 中輸入:
> /agents
# 應該看到 5 個 subagent 列表
```

### 4. 賦予 audit.py 執行權限(可選)

```bash
chmod +x .claude/skills/traceability-audit/scripts/audit.py
```

---

## 合併既有 CLAUDE.md(若已有)

如果你的專案已經有 `CLAUDE.md`,**不要直接覆蓋**。改用合併方式:

1. 開啟現有 `CLAUDE.md`
2. 從本套件的 `CLAUDE.md` 複製以下區段加入:
   - `## Subagent 團隊與分工`
   - `## ⭐ Spec-Driven Workflow`
   - `## Model Escalation Rules`
   - `## 任務分類與 routing`
   - `## 統一回覆模板`
   - `## 禁止事項`
3. 既有的專案描述、環境指令保留

---

## 驗證測試

在 Claude Code 中跑這幾個測試,確認 setup 正確運作:

### 測試 A: spec + backend + test + review 完整流程
```
> 幫我在 validator 加一條規則 V-PARAM-22:禁止 parameters 為純空白字串。
```

**預期行為**:
1. 主 session 委派 `spec-guardian` 起草 → 在 `docs/L1-components/C1-4_Validator.md` 加 V-PARAM-22 條目(依 `_TEMPLATE.md` 格式)
2. 主 session 委派 `backend-engineer`(Sonnet)→ 改 `validator_node.py`,加 `# C1-4:V-PARAM-22` 註解
3. 主 session 委派 `test-engineer` → 加 `test_v_param_22_*` 測試,跑 pytest
4. 主 session 委派 `code-reviewer` → 檢查程式碼品質,回報 blocker/should-fix/nit
5. 主 session 再次委派 `spec-guardian` → review 三方對齊
6. 主 session 用統一模板回覆使用者

### 測試 B: 純 UI 變更
```
> Streamlit 的 plan editor 加一個重置按鈕。
```

**預期**: 走 spec-guardian → frontend-engineer → test-engineer → spec-guardian。

### 測試 C: 升級 Opus
```
> 重新設計 ValidationResult 類別以支援多錯誤聚合,並更新所有相關 node。
```

**預期**: 主 session 識別「重新設計」+「跨多檔」→ 直接派給 `backend-engineer-opus`。

### 測試 D: Spec drift 偵測
故意在 `validator_node.py` 加一個沒有 spec 的註解 `# C1-4:V-FAKE-99`,然後跑:
```bash
python .claude/skills/traceability-audit/scripts/audit.py
```

**預期**: 看到 V-FAKE-99 列在 orphan annotations。

---

## 常見問題

### Q1: 為什麼沒有 head-leader / orchestrator subagent?

**A**: Claude Code 主 session 本身就是 orchestrator。再加一層 leader subagent 會多一層 round-trip 與 token 開銷,且 subagent 不被設計來再委派其他 subagent。本套件用 `CLAUDE.md` 中的 routing rules 取代 leader 角色,效果一樣但更省。

### Q2: engineer 都用 Sonnet 真的夠嗎?

**A**: Sonnet 4.6 在 SWE-bench Verified 是 79.6%,Opus 是 80.8%,差距 ~1.2%。對於「依清楚 spec 實作」這種 well-scoped 工作 Sonnet 完全夠用,且成本低 5 倍、速度快 2-3 倍。複雜任務由 `backend-engineer-opus` 接手(由主 session 依 escalation rule 自動切換)。

### Q3: 如何新增/修改 subagent?

**A**: 直接編輯 `.claude/agents/*.md`。修改後**重新啟動 Claude Code session** 才會生效(subagent 定義在啟動時載入)。

### Q4: 主 session 跳過 spec-guardian 直接讓 engineer 寫,怎麼辦?

**A**: 檢查 `CLAUDE.md` 是否被正確讀取(`> /context` 在 Claude Code 內可看)。若 CLAUDE.md 過長,主 session 可能略過部分內容,把「⭐ Spec-Driven Workflow」段落往前移。

### Q5: 我想要 frontend 也有 Opus 升級版?

**A**: 複製 `backend-engineer-opus.md` 為 `frontend-engineer-opus.md`,改 frontmatter 中的 `name`、`description`、檔案範圍即可。

### Q6: traceability-audit 報告 orphan IDs 太多怎麼辦?

**A**: orphan IDs 代表「spec 寫了但代碼還沒實作」 — 這是正常的 backlog。在新增 spec 條目時故意先寫,再分批實作,本來就會出現。重點是別讓 **orphan annotations**(代碼有但 spec 沒)出現,那才是 drift 警訊。

---

## 進階:整合到 CI

在 `.github/workflows/audit.yml`(或 GitLab CI 等同):

```yaml
name: Traceability Audit
on: [pull_request]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Run traceability audit
        run: python .claude/skills/traceability-audit/scripts/audit.py --strict
```

`--strict` 模式下任何 orphan / missing test 會 exit 1,擋下 PR。

---

## 後續建議的擴充

1. **加上 `langgraph-node-pattern` skill**:當你有 3 個以上重複的 node 加新功能後,把共通模式抽出來
2. **整合 `version-control` skill**:在 spec-guardian 完成 review 後,自動觸發版本控制紀錄
3. **加上 `frontend-engineer-opus`**:若前端開始出現複雜重構需求(目前只有後端有 Opus 版)
4. **建立 `.claude/settings.json`**:用 permissions 條目 enforce 每個 subagent 的工具限制,而非只靠 prompt 約束

---

## 修改與維護

- 所有 agent 行為都由 `.md` 檔的 system prompt 控制 — 想調整行為就改 prompt
- 新增 traceability ID prefix:同步更新 `spec-guardian.md`、`spec-driven-workflow/SKILL.md` 兩處
- audit.py 的 ID 正則在 `ID_PATTERN` 與 `ANNOTATION_PATTERN`,需要支援更多格式時改這兩個

---

設定完成後,直接在 Claude Code 中提需求即可。主 session 會依 CLAUDE.md 的規則自動編排整個流程。
