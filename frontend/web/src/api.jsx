// Backend API wrapper — talks to the FastAPI backend at BACKEND_URL.
//
// Endpoints used:
//   POST /chat   { message } -> ChatResponse (ok, workflow_url, workflow_json, errors, ...)
//   GET  /health            -> { ok, openai, n8n, chroma, checks:{...} }

const DEFAULT_BACKEND_URL =
  (typeof window !== "undefined" && window.__BACKEND_URL__) ||
  "http://localhost:8000";

function getBackendUrl() {
  try {
    const stored = localStorage.getItem("n8n_builder_backend_url");
    if (stored) return stored;
  } catch (e) {}
  return DEFAULT_BACKEND_URL;
}
function setBackendUrl(url) {
  try { localStorage.setItem("n8n_builder_backend_url", url); } catch (e) {}
}

async function postChat(message, { signal, timeoutMs = 200_000 } = {}) {
  const url = getBackendUrl().replace(/\/$/, "") + "/chat";
  const ctrl = new AbortController();
  if (signal) signal.addEventListener("abort", () => ctrl.abort());
  const tid = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
      signal: ctrl.signal,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) {
      throw new Error(`無法解析後端回應：${text.slice(0, 200)}`);
    }
    if (!res.ok && (!data || typeof data !== "object")) {
      throw new Error(`後端錯誤 HTTP ${res.status}`);
    }
    return { status: res.status, data: data || {} };
  } finally {
    clearTimeout(tid);
  }
}

async function getHealth({ timeoutMs = 5_000 } = {}) {
  const url = getBackendUrl().replace(/\/$/, "") + "/health";
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { method: "GET", signal: ctrl.signal });
    const data = await res.json();
    return data;
  } finally {
    clearTimeout(tid);
  }
}

// Convert backend /health payload into the row-friendly shape the sidebar uses.
function toHealthRows(payload) {
  const checks = payload?.checks || {};
  const openai = checks.openai || { ok: !!payload?.openai };
  const n8n = checks.n8n || { ok: !!payload?.n8n };
  const chroma = checks.chroma || { ok: !!payload?.chroma };

  const fmtMs = (c) => (c.latency_ms != null ? `${c.latency_ms}ms` : "—");

  // chroma.detail looks like "discovery=529,detailed=30"
  let chromaDetail = fmtMs(chroma);
  if (chroma.detail) {
    const m = /discovery=(\d+)/.exec(chroma.detail);
    if (m) chromaDetail = `${m[1]} 節點 · ${fmtMs(chroma)}`;
  }

  return {
    openai: { ok: !!openai.ok, detail: openai.error || fmtMs(openai) },
    n8n:    { ok: !!n8n.ok,    detail: n8n.error    || fmtMs(n8n) },
    chroma: { ok: !!chroma.ok, detail: chroma.error || chromaDetail },
    ok: !!payload?.ok,
  };
}

// Infer a "kind" for a raw node so the diagram colors it correctly.
// Mirrors the prototype's NODE_KIND_META buckets.
function inferKind(node) {
  const t = (node?.type || "").toLowerCase();
  if (t.includes("trigger") || t.includes("webhook") || t.includes("cron")) return "trigger";
  if (t.includes("if") || t.includes("switch") || t.includes("filter")) return "condition";
  if (t.includes("set") || t.includes("function") || t.includes("code") || t.includes("merge")) return "transform";
  if (
    t.includes("googlesheets") || t.includes("slack") || t.includes("notion") ||
    t.includes("email") || t.includes("gmail") || t.includes("airtable") ||
    t.includes("discord") || t.includes("sendgrid") || t.includes("telegram")
  ) return "output";
  return "action";
}

// Turn the backend workflow_json into the diagram-friendly shape.
// Backend nodes are `{id, name, type, typeVersion, position, parameters}`;
// we add `kind` and make sure `position` exists so the diagram layout works.
function normalizeWorkflow(raw) {
  if (!raw) return null;
  const nodes = (raw.nodes || []).map((n, i) => ({
    id: n.id || String(i),
    name: n.name,
    type: n.type,
    typeVersion: n.typeVersion,
    position: n.position || [240 + i * 280, 300],
    parameters: n.parameters || {},
    kind: n.kind || inferKind(n),
  }));
  return {
    name: raw.name || "未命名 workflow",
    nodes,
    connections: raw.connections || {},
  };
}

Object.assign(window, {
  getBackendUrl, setBackendUrl, postChat, getHealth, toHealthRows, normalizeWorkflow,
});
