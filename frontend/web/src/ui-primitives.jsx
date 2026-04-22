// Shared UI primitives used by both variants.
// Keep these lean — just atomic helpers.

function Icon({ name, size = 18, className = "", style = {} }) {
  const html = ICONS[name] || ICONS.pipe;
  return (
    <span
      className={"icon " + className}
      style={{ width: size, height: size, display: "inline-flex", ...style }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

// Tiny pill showing pipeline stage status
function StagePill({ stage, state /* idle | active | done | fail */ }) {
  return (
    <span className={`stage-pill stage-pill--${state}`}>
      <span className="stage-pill__dot" />
      <span className="stage-pill__label">{stage.zh}</span>
    </span>
  );
}

// Horizontal pipeline track
function PipelineTrack({ currentStage, stages, compact = false }) {
  const idx = stages.findIndex(s => s.id === currentStage);
  return (
    <div className={"pipeline-track" + (compact ? " pipeline-track--compact" : "")}>
      {stages.map((s, i) => {
        const state =
          currentStage === "done" ? "done"
          : i < idx ? "done"
          : i === idx ? "active"
          : "idle";
        return (
          <React.Fragment key={s.id}>
            <StagePill stage={s} state={state} />
            {i < stages.length - 1 && (
              <span className={`pipeline-track__link pipeline-track__link--${i < idx ? "done" : "idle"}`} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// Node card used in the workflow diagram
function NodeCard({ node, onClick, highlighted, errorCount = 0, compact = false }) {
  const meta = NODE_KIND_META[node.kind] || NODE_KIND_META.action;
  const iconKey = iconForNode(node);
  return (
    <button
      className={
        "node-card node-card--" + meta.accent +
        (highlighted ? " node-card--highlighted" : "") +
        (compact ? " node-card--compact" : "")
      }
      onClick={onClick}
      type="button"
    >
      <div className="node-card__head">
        <span className="node-card__icon">
          <Icon name={iconKey} size={18} />
        </span>
        <span className="node-card__kind">{meta.label}</span>
        {errorCount > 0 && (
          <span className="node-card__badge" title={`${errorCount} 個問題`}>
            {errorCount}
          </span>
        )}
      </div>
      <div className="node-card__name">{node.name}</div>
      <div className="node-card__type">{node.type.replace("n8n-nodes-base.", "")}</div>
    </button>
  );
}

// SVG edge between two nodes (cubic bezier)
function NodeEdge({ from, to, animated = false }) {
  const x1 = from.x + from.w, y1 = from.y + from.h / 2;
  const x2 = to.x,           y2 = to.y + to.h / 2;
  const dx = Math.max(40, (x2 - x1) * 0.5);
  const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  return (
    <g className={"node-edge" + (animated ? " node-edge--animated" : "")}>
      <path d={d} className="node-edge__line" />
      {animated && <path d={d} className="node-edge__pulse" />}
      <circle cx={x2} cy={y2} r={3} className="node-edge__cap" />
    </g>
  );
}

// Avatar blob for assistant
function AssistantAvatar({ size = 28 }) {
  return (
    <span className="assistant-avatar" style={{ width: size, height: size }}>
      <svg viewBox="0 0 20 20">
        <defs>
          <radialGradient id="aa-grad" cx="30%" cy="30%">
            <stop offset="0%" stopColor="var(--accent-light)" />
            <stop offset="100%" stopColor="var(--accent)" />
          </radialGradient>
        </defs>
        <circle cx="10" cy="10" r="9" fill="url(#aa-grad)" />
        <circle cx="7"  cy="9" r="1.6" fill="white" opacity="0.85" />
        <circle cx="13" cy="9" r="1.6" fill="white" opacity="0.85" />
        <path d="M6.5 12.5c1 1 2.2 1.5 3.5 1.5s2.5-.5 3.5-1.5"
              stroke="white" strokeWidth="1.2" strokeLinecap="round" fill="none" opacity="0.85"/>
      </svg>
    </span>
  );
}

// Thinking shimmer dots
function ThinkingDots() {
  return (
    <span className="thinking-dots">
      <span /><span /><span />
    </span>
  );
}

// Health row
function HealthRow({ label, status, detail }) {
  return (
    <div className={"health-row health-row--" + (status.ok ? "ok" : "fail")}>
      <span className="health-row__dot" />
      <span className="health-row__label">{label}</span>
      <span className="health-row__detail">{detail}</span>
    </div>
  );
}

Object.assign(window, {
  Icon, StagePill, PipelineTrack, NodeCard, NodeEdge,
  AssistantAvatar, ThinkingDots, HealthRow,
});
