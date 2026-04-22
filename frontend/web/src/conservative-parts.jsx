// Conservative variant parts: sidebar, empty state, message, composer.
// Sidebar shows live backend health and exposes theme / density / backend URL.

const { useState, useEffect, useRef, useMemo } = React;

// ---------- Sidebar ----------
function Sidebar({
  density,
  theme,
  onThemeToggle,
  onDensityToggle,
  onNewChat,
  health,
  onRefreshHealth,
  history,
  activeHistoryId,
  onSelectHistory,
  deploys,
  backendUrl,
  onBackendUrlChange,
}) {
  const [editingUrl, setEditingUrl] = useState(false);
  const [urlDraft, setUrlDraft] = useState(backendUrl);
  useEffect(() => { setUrlDraft(backendUrl); }, [backendUrl]);

  return (
    <aside className="cv-sidebar">
      <div className="cv-sidebar__head">
        <div className="cv-brand">
          <span className="cv-brand__mark" aria-hidden>
            <svg viewBox="0 0 24 24" width="22" height="22">
              <defs>
                <linearGradient id="brand-g" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0%"  stopColor="var(--accent-light)"/>
                  <stop offset="100%" stopColor="var(--accent)"/>
                </linearGradient>
              </defs>
              <rect x="2" y="6" width="7" height="7" rx="2" fill="url(#brand-g)"/>
              <rect x="15" y="11" width="7" height="7" rx="2" fill="url(#brand-g)" opacity=".85"/>
              <path d="M9 9.5 15 14" stroke="var(--accent)" strokeWidth="1.8" strokeLinecap="round" fill="none"/>
            </svg>
          </span>
          <div className="cv-brand__text">
            <div className="cv-brand__title">Workflow Builder</div>
            <div className="cv-brand__sub">n8n · LangGraph Agent</div>
          </div>
          <button
            className="cv-sidebar__link cv-theme-toggle"
            onClick={onThemeToggle}
            title={theme === "dark" ? "切換為淺色" : "切換為深色"}
          >
            <Icon name={theme === "dark" ? "sun" : "moon"} size={16}/>
          </button>
        </div>
        <button className="btn btn--primary btn--full" onClick={onNewChat}>
          <Icon name="plus" size={16} />
          <span>新對話</span>
        </button>
      </div>

      <div className="cv-sidebar__section">
        <div className="cv-sidebar__section-head">
          <span>對話歷史</span>
          <span className="cv-sidebar__count">{history.length}</span>
        </div>
        {history.length === 0 ? (
          <div className="cv-sidebar__empty">還沒有對話</div>
        ) : (
          <ul className="cv-history">
            {history.map((h) => (
              <li
                key={h.id}
                className={"cv-history__item" + (h.id === activeHistoryId ? " cv-history__item--active" : "")}
                onClick={() => onSelectHistory?.(h.id)}
              >
                <span className={"cv-history__dot " + (h.ok ? "ok" : "fail")} />
                <div className="cv-history__body">
                  <div className="cv-history__title">{h.title}</div>
                  <div className="cv-history__time">{h.time}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="cv-sidebar__section">
        <div className="cv-sidebar__section-head">
          <span>最近部署</span>
          <button className="cv-sidebar__link"><Icon name="external" size={13}/></button>
        </div>
        {deploys.length === 0 ? (
          <div className="cv-sidebar__empty">部署後會出現在這裡</div>
        ) : (
          <ul className="cv-deploys">
            {deploys.map(d => (
              <li key={d.id} className="cv-deploys__item">
                <div className="cv-deploys__row">
                  <span className={"cv-deploys__status cv-deploys__status--" + (d.status || "active")} />
                  {d.url ? (
                    <a
                      href={d.url}
                      target="_blank"
                      rel="noreferrer"
                      className="cv-deploys__name cv-deploys__name--link"
                    >{d.name}</a>
                  ) : (
                    <span className="cv-deploys__name">{d.name}</span>
                  )}
                </div>
                <div className="cv-deploys__meta">
                  <span>{d.nodes} 節點</span>
                  <span className="cv-deploys__dot">·</span>
                  <span>{d.lastRun}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="cv-sidebar__foot">
        <div className="cv-sidebar__section-head cv-sidebar__section-head--foot">
          <span>後端健康</span>
          <button className="cv-sidebar__link" title="重新檢查" onClick={onRefreshHealth}>
            <Icon name="refresh" size={13}/>
          </button>
        </div>
        <HealthRow label="OpenAI"  status={health.openai} detail={health.openai.detail} />
        <HealthRow label="n8n"     status={health.n8n}    detail={health.n8n.detail} />
        <HealthRow label="Chroma"  status={health.chroma} detail={health.chroma.detail} />

        <div className="cv-sidebar__section-head cv-sidebar__section-head--foot" style={{ marginTop: 12 }}>
          <span>版面密度</span>
        </div>
        <div className="cv-seg">
          {[["comfortable","寬鬆"],["compact","緊湊"]].map(([v,l]) => (
            <button key={v}
              className={density === v ? "is-active" : ""}
              onClick={() => onDensityToggle(v)}>{l}</button>
          ))}
        </div>

        <div className="cv-sidebar__section-head cv-sidebar__section-head--foot" style={{ marginTop: 12 }}>
          <span>Backend URL</span>
          {!editingUrl && (
            <button className="cv-sidebar__link" title="編輯"
              onClick={() => setEditingUrl(true)}>編輯</button>
          )}
        </div>
        {editingUrl ? (
          <div className="cv-url-edit">
            <input
              value={urlDraft}
              onChange={e => setUrlDraft(e.target.value)}
              placeholder="http://localhost:8000"
            />
            <button className="btn btn--sm btn--primary" onClick={() => {
              onBackendUrlChange(urlDraft.trim() || backendUrl);
              setEditingUrl(false);
            }}>儲存</button>
          </div>
        ) : (
          <div className="cv-url-show mono" title={backendUrl}>{backendUrl}</div>
        )}
      </div>
    </aside>
  );
}

// ---------- Empty state ----------
function EmptyState({ onSelectPrompt, onSubmit }) {
  const [value, setValue] = useState("");
  return (
    <div className="cv-empty">
      <div className="cv-empty__hero">
        <span className="cv-empty__sparkle"><Icon name="sparkle" size={28} /></span>
        <h1>用一句話,生出 n8n 流程</h1>
        <p>描述你想要自動化的工作,Agent 會檢索 529 個節點、組裝、驗證、部署到你本機 n8n。</p>
      </div>

      <div className="cv-empty__composer">
        <textarea
          placeholder="例如:每個工作日早上 9 點抓 Notion 資料庫新增的頁面,用 OpenAI 摘要後發到 Slack 頻道"
          value={value}
          onChange={e => setValue(e.target.value)}
          rows={3}
          onKeyDown={e => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              if (value.trim()) { onSubmit(value); setValue(""); }
            }
          }}
        />
        <div className="cv-empty__composer-foot">
          <div className="cv-empty__hint">⌘ + Enter 送出</div>
          <button
            className="btn btn--primary"
            disabled={!value.trim()}
            onClick={() => { onSubmit(value); setValue(""); }}
          >
            <Icon name="send" size={14} />
            生成 workflow
          </button>
        </div>
      </div>

      <div className="cv-empty__samples">
        <div className="cv-empty__samples-head">或試試這些範例</div>
        <div className="cv-empty__samples-grid">
          {SAMPLE_PROMPTS.map((p, i) => (
            <button
              key={i}
              className="cv-sample"
              onClick={() => onSelectPrompt(p.title)}
            >
              <span className="cv-sample__icon"><Icon name={p.icon} size={18} /></span>
              <div className="cv-sample__body">
                <div className="cv-sample__title">{p.title}</div>
                <div className="cv-sample__tag">{p.tag}</div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------- Conversation ----------
function ConversationMessage({ msg }) {
  if (msg.role === "user") {
    return (
      <div className="cv-msg cv-msg--user">
        <div className="cv-msg__bubble">{msg.content}</div>
      </div>
    );
  }
  return (
    <div className="cv-msg cv-msg--assistant">
      <AssistantAvatar />
      <div className="cv-msg__body">
        {msg.pipelineBar && (
          <div className="cv-msg__pipeline">
            <PipelineTrack currentStage={msg.currentStage} stages={PIPELINE_STAGES} compact />
          </div>
        )}
        {msg.thinking
          ? <div className="cv-msg__thinking">{msg.thinkingLabel || "思考中"} <ThinkingDots/></div>
          : msg.content && <div className="cv-msg__text">{msg.content}</div>}
        {msg.plan && msg.plan.length > 0 && (
          <ol className="cv-plan">
            {msg.plan.map((step, i) => (
              <li key={i}><span className="cv-plan__num">{i + 1}</span>{step}</li>
            ))}
          </ol>
        )}
        {msg.workflowRef && (
          <div className="cv-result-card">
            <div className="cv-result-card__head">
              <span className="cv-result-card__title">
                <Icon name="check" size={14}/>
                {msg.workflowRef.deployed ? " 已部署到 n8n" : " Workflow 已生成"}
              </span>
              {msg.workflowRef.url ? (
                <a className="cv-result-card__link"
                  href={msg.workflowRef.url} target="_blank" rel="noreferrer">
                  <span>開啟 workflow</span> <Icon name="external" size={13}/>
                </a>
              ) : null}
            </div>
            <div className="cv-result-card__body">
              <div className="cv-result-card__row">
                <span className="cv-result-card__k">Workflow</span>
                <span className="cv-result-card__v">{msg.workflowRef.name}</span>
              </div>
              {msg.workflowRef.id && (
                <div className="cv-result-card__row">
                  <span className="cv-result-card__k">ID</span>
                  <span className="cv-result-card__v mono">{msg.workflowRef.id}</span>
                </div>
              )}
              <div className="cv-result-card__row">
                <span className="cv-result-card__k">節點</span>
                <span className="cv-result-card__v">{msg.workflowRef.nodeCount} 個</span>
              </div>
              {msg.workflowRef.elapsedS != null && (
                <div className="cv-result-card__row">
                  <span className="cv-result-card__k">耗時</span>
                  <span className="cv-result-card__v">
                    {msg.workflowRef.elapsedS.toFixed(1)}s
                    {msg.workflowRef.retryCount != null
                      ? ` · ${msg.workflowRef.retryCount} retry`
                      : ""}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
        {msg.errorBanner && (
          <div className="cv-error-banner">
            <Icon name="alert" size={14}/>
            <span>{msg.errorBanner}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function Composer({ onSubmit, disabled, onStop }) {
  const [value, setValue] = useState("");
  const taRef = useRef(null);
  useEffect(() => {
    if (!taRef.current) return;
    taRef.current.style.height = "auto";
    taRef.current.style.height = Math.min(200, taRef.current.scrollHeight) + "px";
  }, [value]);
  return (
    <div className="cv-composer">
      <textarea
        ref={taRef}
        placeholder={disabled ? "生成中,請稍候…" : "描述你想要的 workflow… 或追問調整"}
        value={value}
        onChange={e => setValue(e.target.value)}
        rows={1}
        disabled={disabled}
        onKeyDown={e => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            if (value.trim() && !disabled) { onSubmit(value); setValue(""); }
          }
        }}
      />
      {disabled && onStop ? (
        <button
          className="cv-composer__send cv-composer__send--stop"
          onClick={onStop}
          title="取消"
        >
          <Icon name="stop" size={14} />
        </button>
      ) : (
        <button
          className="cv-composer__send"
          disabled={!value.trim() || disabled}
          onClick={() => { onSubmit(value); setValue(""); }}
        >
          <Icon name="send" size={16} />
        </button>
      )}
    </div>
  );
}

window.ConservativeSidebar = Sidebar;
window.ConservativeEmpty = EmptyState;
window.ConservativeMessage = ConversationMessage;
window.ConservativeComposer = Composer;
