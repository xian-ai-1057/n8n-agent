// Workflow diagram — SVG layout of nodes + connections.
// Handles: measuring node positions, drawing edges, animated "building" state.

// Build a layout given a workflow and layout params.
// Returns { nodes: [{...node, x, y, w, h}], edges: [{from, to}], width, height }
function layoutWorkflow(workflow, { gapX = 56, nodeW = 200, nodeH = 96, padding = 24 } = {}) {
  const nodes = workflow.nodes.map((n, i) => ({
    ...n,
    x: padding + i * (nodeW + gapX),
    y: padding,
    w: nodeW,
    h: nodeH,
  }));
  const byName = Object.fromEntries(nodes.map(n => [n.name, n]));
  const edges = [];
  for (const [fromName, conns] of Object.entries(workflow.connections || {})) {
    const mains = conns.main || [];
    for (const group of mains) {
      for (const target of group) {
        if (byName[fromName] && byName[target.node]) {
          edges.push({ from: byName[fromName], to: byName[target.node] });
        }
      }
    }
  }
  const width = padding * 2 + nodes.length * nodeW + (nodes.length - 1) * gapX;
  const height = padding * 2 + nodeH;
  return { nodes, edges, width, height };
}

function WorkflowDiagram({
  workflow,
  highlightedNode,
  onNodeClick,
  errorsByNode = {},
  buildingUpTo = null, // node index currently being "built" (animated reveal)
  compact = false,
}) {
  const layout = React.useMemo(
    () => layoutWorkflow(workflow, compact
      ? { gapX: 36, nodeW: 168, nodeH: 82, padding: 18 }
      : {}),
    [workflow, compact]
  );

  // Pan state — drag on empty space translates the diagram
  const [pan, setPan] = React.useState({ x: 0, y: 0 });
  const dragRef = React.useRef(null);

  const onMouseDown = (e) => {
    // only pan if clicking on the root or svg (not on a node)
    if (e.target.closest(".workflow-diagram__node")) return;
    dragRef.current = { startX: e.clientX, startY: e.clientY, origX: pan.x, origY: pan.y };
    e.preventDefault();
  };
  React.useEffect(() => {
    const onMove = (e) => {
      const d = dragRef.current;
      if (!d) return;
      setPan({ x: d.origX + (e.clientX - d.startX), y: d.origY + (e.clientY - d.startY) });
    };
    const onUp = () => { dragRef.current = null; };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  const resetPan = () => setPan({ x: 0, y: 0 });

  return (
    <div
      className={"workflow-diagram" + (dragRef.current ? " is-dragging" : "")}
      onMouseDown={onMouseDown}
      onDoubleClick={resetPan}
      title="拖曳空白處可移動 · 雙擊重設"
    >
      <div className="workflow-diagram__pan" style={{ transform: `translate(${pan.x}px, ${pan.y}px)` }}>
      <svg
        className="workflow-diagram__svg"
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
      >
        <defs>
          <filter id="soft-shadow" x="-50%" y="-50%" width="200%" height="200%">
            <feDropShadow dx="0" dy="4" stdDeviation="6" floodOpacity="0.08" />
          </filter>
        </defs>
        {layout.edges.map((e, i) => {
          const fromIdx = layout.nodes.indexOf(e.from);
          const toIdx = layout.nodes.indexOf(e.to);
          const animated = buildingUpTo !== null && toIdx === buildingUpTo;
          const visible = buildingUpTo === null || toIdx <= buildingUpTo;
          if (!visible) return null;
          return <NodeEdge key={i} from={e.from} to={e.to} animated={animated} />;
        })}
      </svg>
      <div className="workflow-diagram__nodes" style={{ width: layout.width, height: layout.height }}>
        {layout.nodes.map((n, i) => {
          const visible = buildingUpTo === null || i <= buildingUpTo;
          const pulsing = buildingUpTo !== null && i === buildingUpTo;
          return (
            <div
              key={n.id}
              className={
                "workflow-diagram__node" +
                (visible ? "" : " workflow-diagram__node--hidden") +
                (pulsing ? " workflow-diagram__node--pulsing" : "")
              }
              style={{ left: n.x, top: n.y, width: n.w, height: n.h }}
            >
              <NodeCard
                node={n}
                onClick={() => onNodeClick?.(n)}
                highlighted={highlightedNode === n.name}
                errorCount={(errorsByNode[n.name] || []).length}
                compact={compact}
              />
            </div>
          );
        })}
      </div>
      </div>
    </div>
  );
}

window.WorkflowDiagram = WorkflowDiagram;
window.layoutWorkflow = layoutWorkflow;
