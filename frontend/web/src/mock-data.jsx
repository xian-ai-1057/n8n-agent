// Shared mock data for both prototype variants.
// Models the AgentState pipeline: Plan → Build → Assemble → Validate → Deploy.

const PIPELINE_STAGES = [
  { id: "plan",     label: "Plan",     zh: "規劃",   desc: "解析意圖、拆解步驟" },
  { id: "build",    label: "Build",    zh: "建置",   desc: "從 529 個節點檢索並組節點" },
  { id: "assemble", label: "Assemble", zh: "組裝",   desc: "連接節點、建立 edges" },
  { id: "validate", label: "Validate", zh: "驗證",   desc: "規則檢查、型別對齊" },
  { id: "deploy",   label: "Deploy",   zh: "部署",   desc: "POST 到 n8n、回傳 URL" },
];

// Prompt shortcuts (shown in empty state)
const SAMPLE_PROMPTS = [
  { icon: "schedule", title: "每小時抓 GitHub Zen API 存到 Google Sheet",
    tag: "Schedule · HTTP · Sheets" },
  { icon: "webhook",  title: "Webhook 收到訂單後寄 Email 通知",
    tag: "Webhook · Email" },
  { icon: "slack",    title: "每天早上 9 點把 Notion 資料庫新頁面發到 Slack",
    tag: "Cron · Notion · Slack" },
  { icon: "ai",       title: "收到 RSS 新文章時用 OpenAI 摘要再存進 Airtable",
    tag: "RSS · OpenAI · Airtable" },
];

// Fake generated workflow — matches the real n8n schema shape
// (nodes array + connections map).
const MOCK_WORKFLOW = {
  name: "GitHub Zen → Google Sheet (hourly)",
  nodes: [
    {
      id: "n1",
      name: "Schedule Trigger",
      type: "n8n-nodes-base.scheduleTrigger",
      typeVersion: 1.1,
      position: [240, 300],
      parameters: { rule: { interval: [{ field: "hours", hoursInterval: 1 }] } },
      kind: "trigger",
    },
    {
      id: "n2",
      name: "HTTP Request",
      type: "n8n-nodes-base.httpRequest",
      typeVersion: 4.1,
      position: [520, 300],
      parameters: {
        url: "https://api.github.com/zen",
        method: "GET",
        responseFormat: "text",
      },
      kind: "action",
    },
    {
      id: "n3",
      name: "Set fields",
      type: "n8n-nodes-base.set",
      typeVersion: 3.3,
      position: [800, 300],
      parameters: {
        assignments: {
          assignments: [
            { name: "quote",     value: "={{ $json.data }}",                type: "string" },
            { name: "timestamp", value: "={{ $now.toISO() }}",              type: "string" },
            { name: "source",    value: "github.com/zen",                    type: "string" },
          ],
        },
      },
      kind: "transform",
    },
    {
      id: "n4",
      name: "Google Sheets",
      type: "n8n-nodes-base.googleSheets",
      typeVersion: 4.4,
      position: [1080, 300],
      parameters: {
        operation: "append",
        documentId: "1aB...zenLog",
        sheetName: "Sheet1",
        columns: { mappingMode: "autoMapInputData" },
      },
      kind: "output",
    },
  ],
  connections: {
    "Schedule Trigger": { main: [[{ node: "HTTP Request",   type: "main", index: 0 }]] },
    "HTTP Request":     { main: [[{ node: "Set fields",     type: "main", index: 0 }]] },
    "Set fields":       { main: [[{ node: "Google Sheets",  type: "main", index: 0 }]] },
  },
};

// Simulated conversation history (previously generated workflows)
const CHAT_HISTORY = [
  { id: "h1", title: "GitHub Zen → Google Sheet",       time: "今天 14:32", ok: true  },
  { id: "h2", title: "Slack 新訊息轉 Notion",            time: "今天 11:05", ok: true  },
  { id: "h3", title: "Shopify 訂單同步 Airtable",       time: "昨天",       ok: true  },
  { id: "h4", title: "RSS 摘要發 Discord",              time: "昨天",       ok: false },
  { id: "h5", title: "Stripe webhook → HubSpot",        time: "3 天前",     ok: true  },
  { id: "h6", title: "每週把 Postgres 備份到 S3",       time: "上週",       ok: true  },
];

// Recent deployed workflows (sidebar section 3)
const RECENT_DEPLOYS = [
  { id: "wf-8a91", name: "GitHub Zen → Google Sheet",  nodes: 4, status: "active",   lastRun: "2 分鐘前" },
  { id: "wf-7c13", name: "Slack 新訊息轉 Notion",       nodes: 5, status: "active",   lastRun: "17 分鐘前" },
  { id: "wf-6e04", name: "Shopify → Airtable",          nodes: 6, status: "inactive", lastRun: "昨天" },
  { id: "wf-5b22", name: "Stripe → HubSpot",            nodes: 7, status: "active",   lastRun: "3 小時前" },
];

// Health check state
const HEALTH = {
  openai: { ok: true,  latency: 184, model: "Qwen2.5-7B-Instruct" },
  n8n:    { ok: true,  latency:  22, version: "1.123.4" },
  chroma: { ok: true,  latency:  11, discovery: 529, detailed: 30 },
};

// Validator errors (used in "error" state)
const MOCK_ERRORS = [
  { rule_id: "E-REQ-PARAM",  node_name: "Google Sheets", path: "$.parameters.documentId",
    message: "documentId 為必填欄位，但未提供。" },
  { rule_id: "E-BAD-EXPR",   node_name: "Set fields",    path: "$.parameters.assignments[0].value",
    message: "表達式 ={{ $json.data }} 找不到對應欄位，建議改為 ={{ $json.body }}。" },
  { rule_id: "W-TYPE-VER",   node_name: "HTTP Request",  path: "$.typeVersion",
    message: "typeVersion 4.1 已過時，建議升級至 4.2。" },
];

// Scripted conversation for the "generating" demo
const DEMO_CONVERSATION = [
  {
    role: "user",
    content: "幫我做一個每小時抓 https://api.github.com/zen 然後存到 Google Sheet 的 workflow",
  },
  {
    role: "assistant",
    stage: "plan",
    content: "我來規劃這個 workflow。我理解你要的是：",
    plan: [
      "用 Schedule Trigger 每小時觸發一次",
      "透過 HTTP Request 抓 GitHub Zen 純文字 API",
      "用 Set 節點整理成 quote / timestamp / source 三欄",
      "最後 append 到你指定的 Google Sheet",
    ],
  },
  {
    role: "user",
    content: "可以，不過 timestamp 用 ISO 8601 格式就好",
  },
  {
    role: "assistant",
    content: "好的，我會在 Set 節點用 `{{$now.toISO()}}` 生成 ISO 8601 格式，含時區偏移。",
  },
  {
    role: "user",
    content: "那如果 API 掛了呢？會不會炸？",
  },
  {
    role: "assistant",
    content: "我會在 HTTP Request 節點打開 retry on fail（3 次、每次間隔 5 秒），再接一個 IF 節點檢查 response.statusCode 是否為 200。若非 200，分支到 Slack 通知而不寫入 Sheet。",
  },
  {
    role: "user",
    content: "不用 Slack 通知，失敗就跳過那一輪。",
  },
  {
    role: "assistant",
    content: "了解，那就用 Continue on Fail + NoOp 分支處理掉。準備好了嗎？我可以直接產出 JSON。",
  },
];

// Node visual meta — kind → color token + icon label
const NODE_KIND_META = {
  trigger:   { label: "觸發",   accent: "amber"   },
  action:    { label: "動作",   accent: "blue"    },
  transform: { label: "轉換",   accent: "violet"  },
  output:    { label: "輸出",   accent: "green"   },
  condition: { label: "條件",   accent: "rose"    },
};

// Icons as inline SVG strings (small, monochrome, 20px viewBox)
const ICONS = {
  schedule: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.5 2"/></svg>`,
  http:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="7"/><path d="M3 10h14M10 3c2 2 3 4.5 3 7s-1 5-3 7c-2-2-3-4.5-3-7s1-5 3-7z"/></svg>`,
  transform:`<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h9l-2-2M16 13H7l2 2"/></svg>`,
  sheet:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="13" height="13" rx="1.5"/><path d="M3.5 8h13M3.5 12.5h13M8 3.5v13M13 3.5v13"/></svg>`,
  webhook:  `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.5"/><circle cx="14" cy="14" r="2.5"/><circle cx="14" cy="6" r="1.5"/><path d="M7.5 7.5l5 5"/></svg>`,
  slack:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="6" height="2" rx="1"/><rect x="11" y="10" width="6" height="2" rx="1"/><rect x="8" y="3" width="2" height="6" rx="1"/><rect x="10" y="11" width="2" height="6" rx="1"/></svg>`,
  ai:       `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3l1.8 4.2L16 9l-4.2 1.8L10 15l-1.8-4.2L4 9l4.2-1.8z"/></svg>`,
  send:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10l14-6-6 14-2-6-6-2z"/></svg>`,
  sparkle:  `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v3M10 14v3M3 10h3M14 10h3M5 5l2 2M13 13l2 2M15 5l-2 2M7 13l-2 2"/></svg>`,
  check:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10l4 4 8-8"/></svg>`,
  copy:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M13 7V4.5A1.5 1.5 0 0011.5 3h-7A1.5 1.5 0 003 4.5v7A1.5 1.5 0 004.5 13H7"/></svg>`,
  external: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4h5v5M16 4l-7 7M13 11v4.5A1.5 1.5 0 0111.5 17h-7A1.5 1.5 0 013 15.5v-7A1.5 1.5 0 014.5 7H9"/></svg>`,
  alert:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3l8 14H2z"/><path d="M10 8v4M10 15v.01"/></svg>`,
  code:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 6l-4 4 4 4M13 6l4 4-4 4"/></svg>`,
  graph:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="10" r="2"/><circle cx="15" cy="5" r="2"/><circle cx="15" cy="15" r="2"/><path d="M7 10l6-4M7 10l6 4"/></svg>`,
  plus:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M10 4v12M4 10h12"/></svg>`,
  sun:      `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="3.5"/><path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.5 4.5l1.4 1.4M14.1 14.1l1.4 1.4M4.5 15.5l1.4-1.4M14.1 5.9l1.4-1.4"/></svg>`,
  moon:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M16 11.5A7 7 0 018.5 4a7 7 0 107.5 7.5z"/></svg>`,
  clock:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.5 2"/></svg>`,
  trash:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h12M8 6V4h4v2M6 6l1 11h6l1-11"/></svg>`,
  refresh:  `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10a6 6 0 0110-4.5L16 7M16 4v3h-3M16 10a6 6 0 01-10 4.5L4 13M4 16v-3h3"/></svg>`,
  stop:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/></svg>`,
  menu:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 6h12M4 10h12M4 14h12"/></svg>`,
  arrow:    `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10h12M12 6l4 4-4 4"/></svg>`,
  pipe:     `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="4" cy="10" r="1.5"/><circle cx="10" cy="10" r="1.5"/><circle cx="16" cy="10" r="1.5"/><path d="M5.5 10h3M11.5 10h3"/></svg>`,
};

// Map node.type → icon key
function iconForNode(node) {
  const t = node.type || "";
  if (t.includes("schedule")) return "schedule";
  if (t.includes("cron"))     return "clock";
  if (t.includes("webhook"))  return "webhook";
  if (t.includes("http"))     return "http";
  if (t.includes("set") || t.includes("function")) return "transform";
  if (t.includes("googleSheets")) return "sheet";
  if (t.includes("slack"))    return "slack";
  if (t.includes("openAi") || t.includes("ai")) return "ai";
  return "pipe";
}

// Expose globally
Object.assign(window, {
  PIPELINE_STAGES, SAMPLE_PROMPTS, MOCK_WORKFLOW, CHAT_HISTORY,
  RECENT_DEPLOYS, HEALTH, MOCK_ERRORS, DEMO_CONVERSATION,
  NODE_KIND_META, ICONS, iconForNode,
});
