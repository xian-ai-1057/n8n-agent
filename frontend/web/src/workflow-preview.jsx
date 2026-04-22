// Right-side workflow preview — diagram / JSON / errors tabs.

const { useState: usePS, useMemo: useMP } = React;

function WorkflowPreview({
  workflow,
  view,           // "diagram" | "json" | "errors"
  onViewChange,
  errors = [],
  phase,          // "empty" | "generating" | "done" | "error"
  buildingUpTo,   // null | number
  highlightedNode,
  setHighlightedNode,
  deployedUrl,    // string | null
}) {
  const errorsByNode = useMP(() => {
    const out = {};
    for (const e of errors) {
      (out[e.node_name] = out[e.node_name] || []).push(e);
    }
    return out;
  }, [errors]);

  const jsonText = useMP(() => {
    if (!workflow) return "";
    return JSON.stringify(
      { name: workflow.name, nodes: workflow.nodes.map(stripFields), connections: workflow.connections },
      null, 2
    );
  }, [workflow]);

  return (
    <section className="preview">
      <header className="preview__head">
        <div className="preview__title">
          <div className="preview__title-row">
            <Icon name="graph" size={16} />
            <span>Workflow 預覽</span>
            {phase === "generating" && (
              <span className="preview__badge preview__badge--gen">
                <span className="preview__badge-dot" /> 生成中
              </span>
            )}
            {phase === "done" && (
              <span className="preview__badge preview__badge--ok">
                <Icon name="check" size={12}/> 已部署
              </span>
            )}
            {phase === "error" && (
              <span className="preview__badge preview__badge--err">
                <Icon name="alert" size={12}/> 驗證失敗
              </span>
            )}
          </div>
          {workflow && phase !== "empty" && (
            <div className="preview__subtitle">{workflow.name}</div>
          )}
        </div>

        <div className="preview__tabs">
          <button
            className={"preview__tab" + (view === "diagram" ? " is-active" : "")}
            onClick={() => onViewChange("diagram")}
          ><Icon name="graph" size={13}/> 節點圖</button>
          <button
            className={"preview__tab" + (view === "json" ? " is-active" : "")}
            onClick={() => onViewChange("json")}
          ><Icon name="code" size={13}/> JSON</button>
          <button
            className={"preview__tab" + (view === "errors" ? " is-active" : "")}
            onClick={() => onViewChange("errors")}
          >
            <Icon name="alert" size={13}/> 問題
            {errors.length > 0 && <span className="preview__tab-badge">{errors.length}</span>}
          </button>
        </div>
      </header>

      <div className="preview__body">
        {phase === "empty" && <PreviewEmpty />}

        {phase === "generating" && !workflow && <PreviewGenerating />}

        {phase !== "empty" && workflow && view === "diagram" && (
          <div className="preview__scroll">
            <WorkflowDiagram
              workflow={workflow}
              highlightedNode={highlightedNode}
              onNodeClick={n => setHighlightedNode(n === highlightedNode ? null : n.name)}
              errorsByNode={errorsByNode}
              buildingUpTo={buildingUpTo}
            />
            {highlightedNode && (() => {
              const n = workflow.nodes.find(x => x.name === highlightedNode);
              if (!n) return null;
              const kind = (NODE_KIND_META[n.kind] || NODE_KIND_META.action).label;
              return (
                <aside className="inspector">
                  <div className="inspector__head">
                    <div>
                      <div className="inspector__kind">{kind}</div>
                      <div className="inspector__name">{n.name}</div>
                      <div className="inspector__type mono">{n.type}</div>
                    </div>
                    <button className="inspector__close" onClick={() => setHighlightedNode(null)}>×</button>
                  </div>
                  <div className="inspector__sect">
                    <div className="inspector__sect-label">參數</div>
                    <pre className="inspector__params"><code>{JSON.stringify(n.parameters, null, 2)}</code></pre>
                  </div>
                  {errorsByNode[n.name]?.length > 0 && (
                    <div className="inspector__sect">
                      <div className="inspector__sect-label">問題（{errorsByNode[n.name].length}）</div>
                      {errorsByNode[n.name].map((e, i) => (
                        <div key={i} className="inspector__err">
                          <span className="mono">{e.rule_id}</span>
                          <span>{e.message}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </aside>
              );
            })()}
          </div>
        )}

        {phase !== "empty" && workflow && view === "json" && (
          <div className="preview__json">
            <div className="preview__json-head">
              <span>{workflow.nodes.length} 節點 · {jsonText.length} 字元</span>
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => {
                  try { navigator.clipboard.writeText(jsonText); } catch (e) {}
                }}
              >
                <Icon name="copy" size={13}/> 複製
              </button>
            </div>
            <pre><code>{jsonText}</code></pre>
          </div>
        )}

        {phase !== "empty" && view === "errors" && (
          <ErrorsPane errors={errors} onJumpTo={n => {
            setHighlightedNode(n);
            onViewChange("diagram");
          }}/>
        )}
      </div>

      {phase !== "empty" && workflow && view === "diagram" && (
        <footer className="preview__foot">
          <div className="preview__legend">
            {Object.entries(NODE_KIND_META).map(([k, m]) => (
              <span key={k} className={"legend legend--" + m.accent}>
                <span className="legend__swatch" />
                {m.label}
              </span>
            ))}
          </div>
          <div className="preview__actions">
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => {
                try { navigator.clipboard.writeText(jsonText); } catch (e) {}
              }}
            >
              <Icon name="copy" size={13}/> 匯出 JSON
            </button>
            {deployedUrl ? (
              <a
                className="btn btn--primary btn--sm"
                href={deployedUrl}
                target="_blank"
                rel="noreferrer"
              >
                <Icon name="external" size={13}/> 在 n8n 開啟
              </a>
            ) : (
              <button className="btn btn--primary btn--sm" disabled>
                <Icon name="external" size={13}/> 在 n8n 開啟
              </button>
            )}
          </div>
        </footer>
      )}
    </section>
  );
}

function PreviewGenerating() {
  return (
    <div className="preview-generating">
      <div className="preview-generating__pulse">
        <svg viewBox="0 0 80 80" width="80" height="80">
          <circle cx="40" cy="40" r="28" fill="none" stroke="var(--accent-border)" strokeWidth="1.5"/>
          <circle cx="40" cy="40" r="28" fill="none" stroke="var(--accent)" strokeWidth="2"
                  strokeDasharray="44 132" strokeLinecap="round" transform="rotate(-90 40 40)">
            <animateTransform attributeName="transform" type="rotate"
              from="0 40 40" to="360 40 40" dur="1.4s" repeatCount="indefinite"/>
          </circle>
        </svg>
      </div>
      <div className="preview-generating__title">生成中…</div>
      <div className="preview-generating__sub">Agent 正在執行 plan → build → assemble → validate → deploy 階段。完成後會在這裡顯示節點圖、JSON 與 validator 結果。</div>
    </div>
  );
}

function stripFields(n) {
  const { kind, ...rest } = n;
  return rest;
}

function PreviewEmpty() {
  return (
    <div className="preview-empty">
      <div className="preview-empty__illus">
        <svg viewBox="0 0 220 120" width="220" height="120">
          <defs>
            <linearGradient id="pe-a" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor="var(--accent-light)"/>
              <stop offset="1" stopColor="var(--accent)"/>
            </linearGradient>
          </defs>
          <rect x="10"  y="40" width="52" height="40" rx="10" fill="var(--surface-2)" stroke="var(--border)"/>
          <rect x="84"  y="40" width="52" height="40" rx="10" fill="var(--surface-2)" stroke="var(--border)"/>
          <rect x="158" y="40" width="52" height="40" rx="10" fill="url(#pe-a)" opacity="0.85"/>
          <path d="M62 60 H84 M136 60 H158" stroke="var(--border-strong)" strokeWidth="1.5" strokeDasharray="3 3" fill="none"/>
          <circle cx="36"  cy="60" r="4" fill="var(--border-strong)"/>
          <circle cx="110" cy="60" r="4" fill="var(--border-strong)"/>
          <circle cx="184" cy="60" r="4" fill="white"/>
        </svg>
      </div>
      <div className="preview-empty__title">還沒有 workflow</div>
      <div className="preview-empty__sub">
        在左邊用中文描述你要的自動化，Agent 會在這裡繪出節點圖、JSON 與 validator 結果。
      </div>
    </div>
  );
}

function ErrorsPane({ errors, onJumpTo }) {
  if (errors.length === 0) {
    return (
      <div className="errors-empty">
        <Icon name="check" size={22}/>
        <div className="errors-empty__title">沒有偵測到問題</div>
        <div className="errors-empty__sub">Validator 全部通過。</div>
      </div>
    );
  }
  return (
    <div className="errors-list">
      {errors.map((e, i) => {
        const isWarn = e.rule_id.startsWith("W-");
        return (
          <article key={i} className={"error-card error-card--" + (isWarn ? "warn" : "err")}>
            <div className="error-card__head">
              <span className="error-card__badge">{isWarn ? "WARN" : "ERROR"}</span>
              <span className="error-card__rule mono">{e.rule_id}</span>
              <button className="error-card__jump" onClick={() => onJumpTo(e.node_name)}>
                跳到節點 <Icon name="arrow" size={13}/>
              </button>
            </div>
            <div className="error-card__node">
              <Icon name="pipe" size={13}/>
              <span>{e.node_name}</span>
              <span className="error-card__path mono">{e.path}</span>
            </div>
            <div className="error-card__msg">{e.message}</div>
          </article>
        );
      })}
    </div>
  );
}

window.WorkflowPreview = WorkflowPreview;
