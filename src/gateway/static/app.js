const NAV_META = {
  chat: { title: "Chat", subtitle: "Gateway WebSocket chat" },
  dashboard: { title: "Dashboard", subtitle: "Task objects, approvals, and runtime status" },
  audit: { title: "Audit", subtitle: "Audit log explorer for actors, sessions, and approvals" },
  sessions: { title: "Sessions", subtitle: "Current browser sessions" },
  channels: { title: "Channels", subtitle: "Channel status overview" },
  skills: { title: "Skills", subtitle: "OpenClaw-style skill catalog and run" },
  memory: { title: "Memory", subtitle: "SQLite vector memory browser" },
  cron: { title: "Cron Jobs", subtitle: "Scheduled tasks (platform)" },
  logs: { title: "Live Logs", subtitle: "Raw client-side event mirror" },
  settings: { title: "Settings", subtitle: "Gateway connection options" }
};

const DEFAULTS = {
  gatewayUrl: `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`,
  userId: "web_user",
  token: ""
};

const STORAGE_KEY = "boiled_claw_ui_settings_v1";

const navButtons = Array.from(document.querySelectorAll(".nav-item"));
const tabs = Array.from(document.querySelectorAll(".tab"));
const tabTitle = document.getElementById("tabTitle");
const tabSubtitle = document.getElementById("tabSubtitle");
const messagesEl = document.getElementById("messages");
const eventLogEl = document.getElementById("eventLog");
const eventCountBadgeEl = document.getElementById("eventCountBadge");
const rawLogEl = document.getElementById("rawLog");
const sessionListEl = document.getElementById("sessionList");
const refreshDashboardBtn = document.getElementById("refreshDashboardBtn");
const clearDashboardFiltersBtn = document.getElementById("clearDashboardFiltersBtn");
const dashboardSearchInputEl = document.getElementById("dashboardSearchInput");
const dashboardSessionBackendEl = document.getElementById("dashboardSessionBackend");
const dashboardSessionNamespaceEl = document.getElementById("dashboardSessionNamespace");
const dashboardPendingApprovalsEl = document.getElementById("dashboardPendingApprovals");
const dashboardOpenTasksEl = document.getElementById("dashboardOpenTasks");
const dashboardApprovalsListEl = document.getElementById("dashboardApprovalsList");
const dashboardTasksListEl = document.getElementById("dashboardTasksList");
const dashboardApprovalsCaptionEl = document.getElementById("dashboardApprovalsCaption");
const dashboardTasksCaptionEl = document.getElementById("dashboardTasksCaption");
const dashboardApprovalsPrevBtn = document.getElementById("dashboardApprovalsPrevBtn");
const dashboardApprovalsNextBtn = document.getElementById("dashboardApprovalsNextBtn");
const dashboardTasksPrevBtn = document.getElementById("dashboardTasksPrevBtn");
const dashboardTasksNextBtn = document.getElementById("dashboardTasksNextBtn");
const dashboardDetailPanelEl = document.getElementById("dashboardDetailPanel");
const dashboardDetailBadgeEl = document.getElementById("dashboardDetailBadge");
const analyticsContentEl = document.getElementById("analyticsContent");
const refreshAnalyticsBtn = document.getElementById("refreshAnalyticsBtn");
const refreshAuditBtn = document.getElementById("refreshAuditBtn");
const clearAuditFiltersBtn = document.getElementById("clearAuditFiltersBtn");
const auditSearchInputEl = document.getElementById("auditSearchInput");
const auditActorInputEl = document.getElementById("auditActorInput");
const auditSessionInputEl = document.getElementById("auditSessionInput");
const auditToolInputEl = document.getElementById("auditToolInput");
const auditSourceInputEl = document.getElementById("auditSourceInput");
const auditResultInputEl = document.getElementById("auditResultInput");
const auditCurrentSessionEl = document.getElementById("auditCurrentSession");
const auditMatchCountEl = document.getElementById("auditMatchCount");
const auditCaptionEl = document.getElementById("auditCaption");
const auditPrevBtn = document.getElementById("auditPrevBtn");
const auditNextBtn = document.getElementById("auditNextBtn");
const auditListEl = document.getElementById("auditList");
const auditDetailPanelEl = document.getElementById("auditDetailPanel");
const auditDetailBadgeEl = document.getElementById("auditDetailBadge");
const inspectorSessionBackendEl = document.getElementById("inspectorSessionBackend");
const inspectorCurrentSessionEl = document.getElementById("inspectorCurrentSession");
const inspectorPendingApprovalsEl = document.getElementById("inspectorPendingApprovals");
const inspectorOpenTasksEl = document.getElementById("inspectorOpenTasks");
const inspectorApprovalsListEl = document.getElementById("inspectorApprovalsList");
const inspectorTasksListEl = document.getElementById("inspectorTasksList");
const inspectorApprovalCountBadgeEl = document.getElementById("inspectorApprovalCountBadge");
const inspectorTaskCountBadgeEl = document.getElementById("inspectorTaskCountBadge");
const inspectorSelectionDetailEl = document.getElementById("inspectorSelectionDetail");
const inspectorSelectionBadgeEl = document.getElementById("inspectorSelectionBadge");
const statusDotEl = document.getElementById("statusDot");
const statusTextEl = document.getElementById("statusText");
const sessionBadgeEl = document.getElementById("sessionBadge");
const gatewayHostLabelEl = document.getElementById("gatewayHostLabel");
const heartbeatDotEl = document.getElementById("heartbeatDot");
const dashboardFilterChips = Array.from(document.querySelectorAll(".status-chip"));

const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const abortBtn = document.getElementById("abortBtn");
const chatForm = document.getElementById("chatForm");
const messageInputEl = document.getElementById("messageInput");

const gatewayUrlEl = document.getElementById("gatewayUrl");
const tokenEl = document.getElementById("token");
const userIdEl = document.getElementById("userId");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const resetSettingsBtn = document.getElementById("resetSettingsBtn");
const refreshSkillsBtn = document.getElementById("refreshSkillsBtn");
const refreshMemoryBtn = document.getElementById("refreshMemoryBtn");
const searchMemoryBtn = document.getElementById("searchMemoryBtn");
const memoryQueryInputEl = document.getElementById("memoryQueryInput");
const memoryTagsInputEl = document.getElementById("memoryTagsInput");
const memoryListEl = document.getElementById("memoryList");
const memoryStatsEl = document.getElementById("memoryStats");
const skillsListEl = document.getElementById("skillsList");
const skillNameInputEl = document.getElementById("skillNameInput");
const skillParamsInputEl = document.getElementById("skillParamsInput");
const runSkillBtn = document.getElementById("runSkillBtn");
const skillResultEl = document.getElementById("skillResult");

// cron elements
const cronListEl = document.getElementById("cronList");
const cronNameEl = document.getElementById("cronName");
const cronExprEl = document.getElementById("cronExpr");
const cronTaskEl = document.getElementById("cronTask");
const cronAgentEl = document.getElementById("cronAgent");
const cronDeliveryEl = document.getElementById("cronDelivery");
const cronRetriesEl = document.getElementById("cronRetries");
const cronSysEventEl = document.getElementById("cronSysEvent");
const addCronBtn = document.getElementById("addCronBtn");
const refreshCronBtn = document.getElementById("refreshCronBtn");
const cronResultEl = document.getElementById("cronResult");

let socket = null;
let waitingIndicator = null;
const sessions = [];
let pendingMessage = null;
const messageHistory = [];
let currentSessionId = null;
const inlineApprovals = new Map();
const MAX_EVENT_ROWS = 200;

// --- streaming state ---
let _streamingBubble = null;
let _streamingText = "";
let _runInProgress = false;
let _messageInputComposing = false;
let _dashboardRefreshHandle = null;
let _dashboardRefreshPromise = null;
let _auditRefreshHandle = null;
let _auditRefreshPromise = null;
const dashboardState = {
  sessionBackend: "-",
  sessionNamespace: "",
  pendingApprovals: [],
  pendingApprovalsTotal: 0,
  dashboardApprovals: [],
  approvalPage: 1,
  approvalPageSize: 12,
  approvalTotal: 0,
  approvalHasMore: false,
  recentTasks: [],
  recentTasksTotal: 0,
  dashboardTasks: [],
  taskPage: 1,
  taskPageSize: 12,
  taskTotal: 0,
  taskHasMore: false,
  openTaskCount: 0,
  searchQuery: "",
  taskStatusFilter: "all",
  approvalStateFilter: "all",
  selectedKind: null,
  selectedId: null,
  selectedTask: null,
  selectedApproval: null,
  relatedTasks: [],
  relatedApprovals: [],
  childTasks: [],
  subagentRun: null,
  taskTimeline: [],
  taskTimelinePagination: null,
  selectedApprovalSuggestions: [],
  taskComparison: null,
};
const auditState = {
  entries: [],
  page: 1,
  pageSize: 20,
  total: 0,
  hasMore: false,
  searchQuery: "",
  actorFilter: "",
  sessionFilter: "",
  toolFilter: "",
  sourceFilter: "",
  resultFilter: "",
  selectedEntryId: null,
  selectedEntry: null,
  autoSelectFirst: false,
  focus: null,
};
const KNOWN_STATUS_TAGS = new Set([
  "accepted",
  "approved",
  "approving",
  "cancelled",
  "completed",
  "denied",
  "denying",
  "expired",
  "failed",
  "idle",
  "pending",
  "propagated",
  "resolved",
  "running"
]);

// -----------------------------------------------------------------------
// Settings
// -----------------------------------------------------------------------

function parseStoredSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch (_) { return {}; }
}

function parseUrlSettings() {
  const params = new URLSearchParams(window.location.search);
  const partial = {};
  if (params.get("gatewayUrl")) partial.gatewayUrl = params.get("gatewayUrl");
  if (params.get("token")) partial.token = params.get("token");
  if (params.get("userId")) partial.userId = params.get("userId");
  return partial;
}

function currentSettings() {
  return {
    gatewayUrl: (gatewayUrlEl.value || "").trim(),
    token: (tokenEl.value || "").trim(),
    userId: (userIdEl.value || "").trim() || "web_user"
  };
}

function applySettings(settings) {
  const merged = { ...DEFAULTS, ...settings };
  gatewayUrlEl.value = merged.gatewayUrl;
  tokenEl.value = merged.token;
  userIdEl.value = merged.userId;
  gatewayHostLabelEl.textContent = merged.gatewayUrl || DEFAULTS.gatewayUrl;
}

function persistSettings() {
  const settings = currentSettings();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  gatewayHostLabelEl.textContent = settings.gatewayUrl;
  addSystemMessage("saved settings");
  logEvent("settings.saved", settings);
}

function resetSettings() {
  localStorage.removeItem(STORAGE_KEY);
  applySettings(DEFAULTS);
  addSystemMessage("reset settings");
  logEvent("settings.reset", DEFAULTS);
}

function isTabActive(tabKey) {
  return document.getElementById(`tab-${tabKey}`)?.classList.contains("active");
}

// -----------------------------------------------------------------------
// URL helpers
// -----------------------------------------------------------------------

function toWebSocketUrl(settings, sessionId = null) {
  let base = settings.gatewayUrl || DEFAULTS.gatewayUrl;
  if (base.startsWith("http://")) base = "ws://" + base.slice(7);
  if (base.startsWith("https://")) base = "wss://" + base.slice(8);
  if (!base.startsWith("ws://") && !base.startsWith("wss://")) {
    base = `${window.location.protocol === "https:" ? "wss" : "ws"}://${base}`;
  }
  base = base.replace(/\/+$/, "");

  const parsed = new URL(base);
  const userPath = `/ws/${encodeURIComponent(settings.userId)}`;

  if (["/chat", "/chat/", "/ws", "/ws/"].includes(parsed.pathname) ||
      /^\/ws\/[^/]+\/?$/.test(parsed.pathname) ||
      parsed.pathname === "/" || parsed.pathname === "") {
    parsed.pathname = userPath;
  } else if (!parsed.pathname.startsWith("/ws/")) {
    parsed.pathname = userPath;
  }

  const wsUrl = new URL(parsed.toString());
  if (settings.token) wsUrl.searchParams.set("token", settings.token);
  if (sessionId) wsUrl.searchParams.set("session_id", sessionId);
  return wsUrl.toString();
}

function toHttpBaseUrl(settings) {
  let base = settings.gatewayUrl || DEFAULTS.gatewayUrl;
  if (base.startsWith("ws://")) base = "http://" + base.slice(5);
  if (base.startsWith("wss://")) base = "https://" + base.slice(6);
  if (!base.startsWith("http://") && !base.startsWith("https://")) {
    base = `${window.location.protocol}//${base}`;
  }
  base = base.replace(/\/+$/, "");
  const parsed = new URL(base);
  if (["/chat", "/chat/", "/ws", "/ws/"].includes(parsed.pathname) ||
      /^\/ws\/[^/]+\/?$/.test(parsed.pathname) || parsed.pathname === "/") {
    parsed.pathname = "";
  }
  return parsed.toString().replace(/\/+$/, "");
}

// -----------------------------------------------------------------------
// UI helpers
// -----------------------------------------------------------------------

function setStatus(online, text) {
  statusDotEl.classList.toggle("online", online);
  statusDotEl.classList.toggle("offline", !online);
  statusTextEl.textContent = text;
  connectBtn.disabled = online;
  disconnectBtn.disabled = !online;
}

function setRunInProgress(inProgress) {
  _runInProgress = inProgress;
  abortBtn.disabled = !inProgress;
  messageInputEl.disabled = inProgress;
}

function appendBubble(kind, text, { persist = true } = {}) {
  if (persist) {
    messageHistory.push({ kind, text });
  }
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind}`;
  bubble.textContent = text;
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function addSystemMessage(text) {
  return appendBubble("system", text);
}

function approvalStateLabel(status) {
  switch (status) {
    case "approving": return "approving";
    case "denying": return "denying";
    case "approved": return "approved";
    case "denied": return "denied";
    case "expiring": return "expiring";
    case "expired": return "expired";
    default: return "pending";
  }
}

function approvalBubbleClass(model) {
  let className = "bubble approval";
  if (model.status === "approved") className += " approval-resolved";
  if (model.status === "denied" || model.status === "denying") className += " approval-denied";
  if (model.status === "expiring") className += " approval-expiring";
  if (model.status === "expired") className += " approval-denied";
  return className;
}

function approvalCountdownHtml(model) {
  if (!model.expiresAt || !["pending", "expiring"].includes(model.status)) return "";
  const nowSec = Date.now() / 1000;
  const remaining = Math.max(0, Math.round(model.expiresAt - nowSec));
  if (remaining <= 0) return `<span class="approval-countdown expired">expired</span>`;
  const mins = Math.floor(remaining / 60);
  const secs = remaining % 60;
  const label = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  const urgencyClass = remaining <= 30 ? " urgent" : remaining <= 60 ? " warning" : "";
  return `<span class="approval-countdown${urgencyClass}" data-expires-at="${model.expiresAt}">${label}</span>`;
}

function approvalEscalationHtml(model) {
  if (model.status !== "expiring") return "";
  const suggestions = Array.isArray(model.escalationSuggestions) ? model.escalationSuggestions : [];
  if (!suggestions.length) return "";
  const buttons = suggestions.map((s) =>
    `<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="${escapeAttr(s.strategy || "session_exact")}">${escapeHtml(s.label || "Upgrade")}</button>`
  );
  return `<div class="approval-escalation"><div class="approval-escalation-label">Upgrade scope to avoid timeout:</div><div class="approval-actions">${buttons.join("")}</div></div>`;
}

function approvalBodyHtml(model) {
  const argsHtml = model.argsPreview
    ? `<div class="approval-args">args: ${escapeHtml(model.argsPreview)}</div>`
    : "";
  const noteHtml = model.note
    ? `<div class="approval-note">${escapeHtml(model.note)}</div>`
    : "";
  const countdownHtml = approvalCountdownHtml(model);
  const escalationHtml = approvalEscalationHtml(model);
  const familyPattern = approvalFamilyPattern(model.toolName || "");
  const strategyButtons = [];
  if (model.status === "pending" || model.status === "expiring") {
    strategyButtons.push(`<button class="btn btn-sm approve-btn" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="single">Approve</button>`);
    if (model.kind === "tool" && model.toolName) {
      strategyButtons.push(`<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="session_exact">Session</button>`);
      if (familyPattern && familyPattern !== model.toolName) {
        strategyButtons.push(`<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="family_session">Family</button>`);
      }
      if (isDesktopApprovalTool(model.toolName)) {
        strategyButtons.push(`<button class="btn btn-sm" data-id="${escapeAttr(model.requestId)}" data-approved="true" data-strategy="desktop_session_pack">Desktop Pack</button>`);
      }
    }
    strategyButtons.push(`<button class="btn btn-sm deny-btn" data-id="${escapeAttr(model.requestId)}" data-approved="false" data-strategy="single">Deny</button>`);
  }
  const actionsHtml = strategyButtons.length
    ? `<div class="approval-actions">${strategyButtons.join("")}</div>`
    : "";

  return [
    `<div class="approval-card">`,
    `<div class="approval-header">`,
    `<div class="approval-title">${escapeHtml(model.title)}</div>`,
    countdownHtml,
    `<span class="tag approval-status">${escapeHtml(approvalStateLabel(model.status))}</span>`,
    `</div>`,
    model.subtitle ? `<div class="approval-meta">${escapeHtml(model.subtitle)}</div>` : "",
    model.reason ? `<div class="approval-reason">${escapeHtml(model.reason)}</div>` : "",
    argsHtml,
    noteHtml,
    escalationHtml,
    actionsHtml,
    `</div>`
  ].join("");
}

function wireApprovalButtons(bubble, model) {
  bubble.querySelectorAll("[data-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const approved = button.dataset.approved === "true";
      const strategy = button.dataset.strategy || "single";
      sendApprovalAction(
        model.requestId,
        approved,
        strategy,
        model.sessionId || currentSessionId || "",
      );
    });
  });
}

function renderInlineApproval(model) {
  const bubble = document.createElement("div");
  bubble.className = approvalBubbleClass(model);
  bubble.dataset.requestId = model.requestId;
  bubble.innerHTML = approvalBodyHtml(model);
  wireApprovalButtons(bubble, model);
  model.element = bubble;
  return bubble;
}

function updateInlineApprovalElement(model) {
  const bubble = model.element;
  if (!bubble || !bubble.isConnected) return;
  bubble.className = approvalBubbleClass(model);
  bubble.innerHTML = approvalBodyHtml(model);
  wireApprovalButtons(bubble, model);
}

function upsertInlineApproval(model) {
  const existing = inlineApprovals.get(model.requestId);
  const next = {
    createdAt: existing?.createdAt || Date.now(),
    status: existing?.status || "pending",
    note: existing?.note || "",
    ...existing,
    ...model
  };
  inlineApprovals.set(next.requestId, next);

  if (next.element && next.element.isConnected) {
    updateInlineApprovalElement(next);
    return next;
  }

  const bubble = renderInlineApproval(next);
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return next;
}

function updateInlineApprovalStatus(requestId, status, note = "") {
  const existing = inlineApprovals.get(requestId);
  if (!existing) return;
  existing.status = status;
  existing.note = note;
  updateInlineApprovalElement(existing);
}

function isDesktopApprovalTool(toolName) {
  return String(toolName || "").startsWith("desktop_");
}

function approvalFamilyPattern(toolName) {
  const normalized = String(toolName || "");
  if (normalized.startsWith("desktop_ax_")) return "desktop_ax_*";
  if (normalized.startsWith("desktop_view_")) return "desktop_view_*";
  if (normalized.startsWith("desktop_wait_")) return "desktop_wait_*";
  if (normalized.startsWith("desktop_control_")) return "desktop_control_*";
  return normalized;
}

function getPendingInlineApprovalIds() {
  return Array.from(inlineApprovals.values())
    .filter((model) => model.status === "pending")
    .sort((a, b) => a.createdAt - b.createdAt)
    .map((model) => model.requestId);
}

function parseApprovalResolutionMessage(message) {
  const match = /^Approval\s+([a-z0-9]+):\s+(approved|denied)$/i.exec(message || "");
  if (!match) return null;
  return {
    requestId: match[1],
    status: match[2].toLowerCase() === "approved" ? "approved" : "denied"
  };
}

function restoreMessages() {
  messagesEl.innerHTML = "";
  inlineApprovals.forEach((model) => {
    model.element = null;
  });
  messageHistory.forEach(({ kind, text }) => {
    const b = document.createElement("div");
    b.className = `bubble ${kind}`;
    b.textContent = text;
    messagesEl.appendChild(b);
  });
  Array.from(inlineApprovals.values())
    .sort((a, b) => a.createdAt - b.createdAt)
    .forEach((model) => {
      messagesEl.appendChild(renderInlineApproval(model));
    });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function clearWaiting() {
  if (waitingIndicator) {
    waitingIndicator.remove();
    waitingIndicator = null;
  }
}

function logEvent(name, payload) {
  const ts = new Date().toISOString();
  const row = document.createElement("div");
  row.className = "event-row";
  row.textContent = `[${ts}] ${name}${payload ? ` ${JSON.stringify(payload)}` : ""}`;
  eventLogEl.prepend(row);
  while (eventLogEl.childElementCount > MAX_EVENT_ROWS) {
    eventLogEl.removeChild(eventLogEl.lastElementChild);
  }
  if (eventCountBadgeEl) {
    eventCountBadgeEl.textContent = String(eventLogEl.childElementCount);
  }
  const line = `[${ts}] ${name}${payload ? ` ${JSON.stringify(payload)}` : ""}`;
  rawLogEl.textContent = `${line}\n${rawLogEl.textContent}`.slice(0, 12000);
}

function apiFetch(url, init = {}) {
  const settings = currentSettings();
  const headers = new Headers(init.headers || {});
  if (settings.token) headers.set("Authorization", `Bearer ${settings.token}`);
  return fetch(url, { ...init, headers });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(str) {
  return escapeHtml(str).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function formatTimestamp(ts) {
  if (!ts) return "-";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (_) {
    return "-";
  }
}

function statusTag(status) {
  const normalized = String(status || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  const safe = escapeHtml(status || "unknown");
  const fallbackClass = KNOWN_STATUS_TAGS.has(normalized) ? "" : " status-fallback";
  const cls = `tag status-tag status-${normalized}${fallbackClass}`;
  return `<span class="${cls}">${safe}</span>`;
}

function formatJsonBlock(value) {
  try {
    return escapeHtml(JSON.stringify(value ?? {}, null, 2));
  } catch (_) {
    return escapeHtml(String(value ?? ""));
  }
}

function compactText(value, limit = 180) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1)}…`;
}

function isOpenTaskStatus(status) {
  const normalized = String(status || "").toLowerCase();
  return !["completed", "failed", "cancelled", "expired"].includes(normalized);
}

function dashboardSearchQuery() {
  return String(dashboardState.searchQuery || "").trim();
}

function approvalListIncludesExpired(filter) {
  return filter === "all" || filter === "expired";
}

function paginationLabel(total, page, pageSize, currentCount) {
  if (!total) return "0 shown";
  if (!currentCount) return `0 / ${total}`;
  const start = (Math.max(1, page) - 1) * pageSize + 1;
  const end = start + currentCount - 1;
  return `${start}-${Math.max(start, end)} / ${total}`;
}

function updatePagerButtons(prevEl, nextEl, page, hasMore) {
  if (prevEl) prevEl.disabled = page <= 1;
  if (nextEl) nextEl.disabled = !hasMore;
}

function resetDashboardPages() {
  dashboardState.approvalPage = 1;
  dashboardState.taskPage = 1;
}

function updateDashboardFilterButtons() {
  dashboardFilterChips.forEach((chip) => {
    const kind = chip.dataset.filterKind;
    const value = chip.dataset.filterValue || "all";
    const active = (
      (kind === "task-status" && dashboardState.taskStatusFilter === value)
      || (kind === "approval-state" && dashboardState.approvalStateFilter === value)
    );
    chip.classList.toggle("active", active);
  });
}

function buildDashboardApprovalParams() {
  const params = new URLSearchParams({
    state: dashboardState.approvalStateFilter || "all",
    page: String(dashboardState.approvalPage || 1),
    page_size: String(dashboardState.approvalPageSize || 12),
  });
  if (approvalListIncludesExpired(dashboardState.approvalStateFilter)) {
    params.set("include_expired", "true");
  }
  const query = dashboardSearchQuery();
  if (query) params.set("q", query);
  if (currentSessionId) params.set("session_id", currentSessionId);
  return params;
}

function buildDashboardTaskParams() {
  const params = new URLSearchParams({
    page: String(dashboardState.taskPage || 1),
    page_size: String(dashboardState.taskPageSize || 12),
  });
  if (dashboardState.taskStatusFilter && dashboardState.taskStatusFilter !== "all") {
    params.set("status", dashboardState.taskStatusFilter);
  }
  const query = dashboardSearchQuery();
  if (query) params.set("q", query);
  if (currentSessionId) params.set("session_id", currentSessionId);
  return params;
}

function auditSearchQuery() {
  return String(auditState.searchQuery || "").trim();
}

function syncAuditInputsFromState() {
  if (auditSearchInputEl) auditSearchInputEl.value = auditState.searchQuery || "";
  if (auditActorInputEl) auditActorInputEl.value = auditState.actorFilter || "";
  if (auditSessionInputEl) auditSessionInputEl.value = auditState.sessionFilter || "";
  if (auditToolInputEl) auditToolInputEl.value = auditState.toolFilter || "";
  if (auditSourceInputEl) auditSourceInputEl.value = auditState.sourceFilter || "";
  if (auditResultInputEl) auditResultInputEl.value = auditState.resultFilter || "";
}

function resetAuditFilters() {
  auditState.searchQuery = "";
  auditState.actorFilter = "";
  auditState.sessionFilter = currentSessionId || "";
  auditState.toolFilter = "";
  auditState.sourceFilter = "";
  auditState.resultFilter = "";
  auditState.page = 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  auditState.autoSelectFirst = false;
  auditState.focus = null;
  syncAuditInputsFromState();
}

function buildAuditParams() {
  const params = new URLSearchParams({
    page: String(auditState.page || 1),
    page_size: String(auditState.pageSize || 20),
  });
  const query = auditSearchQuery();
  if (query) params.set("q", query);
  if (auditState.actorFilter) params.set("actor_user_id", auditState.actorFilter);
  if (auditState.sessionFilter) params.set("session_id", auditState.sessionFilter);
  if (auditState.toolFilter) params.set("tool", auditState.toolFilter);
  if (auditState.sourceFilter) params.set("source", auditState.sourceFilter);
  if (auditState.resultFilter) params.set("result", auditState.resultFilter);
  return params;
}

function auditResultTag(entry) {
  const result = String(entry?.result || entry?.event_type || "unknown");
  return statusTag(result);
}

function auditMetadata(entry) {
  return entry?.metadata && typeof entry.metadata === "object" ? entry.metadata : {};
}

function renderDetailChips(items) {
  if (!Array.isArray(items) || !items.length) return "";
  return [
    `<div class="detail-chip-row">`,
    ...items.map((item) => (
      `<span class="detail-chip"><span class="detail-chip-label">${escapeHtml(item.label || "meta")}</span><span class="detail-chip-value">${escapeHtml(item.value || "-")}</span></span>`
    )),
    `</div>`,
  ].join("");
}

function normalizeAuditFocus(focus) {
  if (!focus || typeof focus !== "object") return null;
  const normalized = {};
  [
    "entryId",
    "requestId",
    "taskId",
    "runId",
    "sessionId",
    "toolName",
    "source",
    "result",
    "searchQuery",
  ].forEach((key) => {
    const value = String(focus[key] || "").trim();
    if (value) normalized[key] = value;
  });
  return Object.keys(normalized).length ? normalized : null;
}

function auditSearchText(entry) {
  const metadata = auditMetadata(entry);
  const parts = [
    entry?.entry_id,
    entry?.event_type,
    entry?.user_id,
    entry?.session_id,
    entry?.action,
    entry?.resource,
    entry?.result,
    metadata.tool_name,
    metadata.tool_pattern,
    metadata.source,
    metadata.actor_user_id,
    metadata.target_session_id,
  ];
  try {
    parts.push(JSON.stringify(metadata));
  } catch (_) {
    parts.push(String(metadata || ""));
  }
  return parts
    .filter(Boolean)
    .map((part) => String(part).toLowerCase())
    .join(" ");
}

function auditEntryFocusScore(entry, focus) {
  if (!focus) return 0;
  const metadata = auditMetadata(entry);
  const resource = String(entry?.resource || "");
  const searchText = auditSearchText(entry);
  let score = 0;

  if (focus.entryId && entry?.entry_id === focus.entryId) score += 1000;
  if (focus.requestId) {
    const requestMatches = [
      resource,
      metadata.request_id,
      metadata.source_request_id,
    ].filter(Boolean).map((value) => String(value));
    if (requestMatches.includes(focus.requestId)) score += 700;
    else if (requestMatches.some((value) => value.includes(focus.requestId))) score += 480;
  }
  if (focus.taskId) {
    const taskMatches = [
      metadata.task_id,
      metadata.parent_task_id,
      metadata.winner_task_id,
      resource,
    ].filter(Boolean).map((value) => String(value));
    if (taskMatches.includes(focus.taskId)) score += 520;
    else if (taskMatches.some((value) => value.includes(focus.taskId))) score += 340;
  }
  if (focus.runId) {
    const runMatches = [
      metadata.run_id,
      metadata.runId,
      resource,
    ].filter(Boolean).map((value) => String(value));
    if (runMatches.includes(focus.runId)) score += 460;
    else if (runMatches.some((value) => value.includes(focus.runId))) score += 300;
  }
  if (focus.toolName) {
    const toolText = [
      metadata.tool_name,
      metadata.tool_pattern,
      entry?.action,
      resource,
    ].filter(Boolean).join(" ").toLowerCase();
    if (toolText.includes(focus.toolName.toLowerCase())) score += 180;
  }
  if (focus.source && String(metadata.source || "").toLowerCase() === focus.source.toLowerCase()) {
    score += 120;
  }
  if (focus.result && String(entry?.result || "").toLowerCase() === focus.result.toLowerCase()) {
    score += 90;
  }
  if (
    focus.sessionId
    && [entry?.session_id, metadata.target_session_id]
      .filter(Boolean)
      .map((value) => String(value))
      .includes(focus.sessionId)
  ) {
    score += 80;
  }
  if (focus.searchQuery && searchText.includes(focus.searchQuery.toLowerCase())) score += 50;
  return score;
}

function selectPreferredAuditEntry(entries, focus) {
  if (!Array.isArray(entries) || !entries.length) return null;
  if (!focus) return entries[0];
  let bestEntry = entries[0];
  let bestScore = auditEntryFocusScore(bestEntry, focus);
  for (const entry of entries.slice(1)) {
    const score = auditEntryFocusScore(entry, focus);
    if (score > bestScore) {
      bestEntry = entry;
      bestScore = score;
    }
  }
  return bestEntry;
}

function renderAuditListItem(entry) {
  const metaBits = [
    entry.event_type || "-",
    entry.user_id || entry.metadata?.actor_user_id || "-",
    formatTimestamp(entry.timestamp),
  ];
  if (entry.session_id) metaBits.push(entry.session_id);
  const detailBits = [];
  if (entry.action) detailBits.push(`action=${entry.action}`);
  if (entry.metadata?.tool_name) detailBits.push(`tool=${entry.metadata.tool_name}`);
  if (entry.metadata?.source) detailBits.push(`source=${entry.metadata.source}`);
  if (entry.resource) detailBits.push(`resource=${compactText(entry.resource, 90)}`);
  return [
    "<li>",
    `<button class="list-item-button${auditState.selectedEntryId === entry.entry_id ? " active" : ""}" type="button" data-audit-id="${escapeAttr(entry.entry_id || "")}">`,
    `<div class="item-card">`,
    `<div class="item-head">`,
    `<div class="item-title">${escapeHtml(entry.event_type || entry.action || "audit")}</div>`,
    auditResultTag(entry),
    `</div>`,
    `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
    detailBits.length ? `<div class="item-detail mono">${escapeHtml(detailBits.join(" · "))}</div>` : "",
    entry.metadata?.resolve_reason ? `<div class="item-detail">${escapeHtml(compactText(entry.metadata.resolve_reason, 180))}</div>` : "",
    `</div>`,
    `</button>`,
    "</li>",
  ].join("");
}

function renderScopeTransition(entry) {
  const metadata = auditMetadata(entry);
  const rows = [
    ["State", metadata.state_before, metadata.state_after],
    ["Scope", metadata.scope_before, metadata.scope_after],
    ["Tool Pattern", metadata.tool_pattern_before, metadata.tool_pattern_after],
    ["Path Scope", metadata.path_scope_before, metadata.path_scope_after],
    ["Propagate", metadata.propagate_to_subagents_before, metadata.propagate_to_subagents_after],
  ].filter((row) => row[1] != null || row[2] != null);
  if (!rows.length) return "";
  return [
    `<div class="detail-section">`,
    `<div class="k">Before / After</div>`,
    `<div class="audit-diff-grid">`,
    ...rows.map(([label, before, after]) => (
      `<div class="detail-card"><div class="k">${escapeHtml(label)}</div><div class="mono">${escapeHtml(String(before ?? "-"))}</div><div class="item-meta mono">→ ${escapeHtml(String(after ?? "-"))}</div></div>`
    )),
    `</div>`,
    `</div>`,
  ].join("");
}

function renderApprovalAuditSummary(entry) {
  if (entry?.event_type !== "tool_approval") return "";
  const metadata = auditMetadata(entry);
  const requestId = entry.resource || metadata.request_id || metadata.source_request_id || "-";
  const resolveReason = metadata.resolve_reason || "-";
  const sourceRequestId = metadata.source_request_id || "-";
  return [
    `<div class="detail-section">`,
    `<div class="k">Approval Resolve Summary</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Request</div><div class="mono">${escapeHtml(requestId)}</div><div class="item-meta mono">${escapeHtml(sourceRequestId)}</div></div>`,
    `<div class="detail-card"><div class="k">Actor / Source</div><div class="mono">${escapeHtml(metadata.actor_user_id || entry.user_id || "-")}</div><div class="item-meta">${escapeHtml(metadata.source || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Tool / Scope</div><div>${escapeHtml(metadata.tool_name || metadata.tool_pattern || "-")}</div><div class="item-meta mono">${escapeHtml(`${metadata.scope_before || "-"} → ${metadata.scope_after || "-"}`)}</div></div>`,
    `<div class="detail-card"><div class="k">Reason</div><div>${escapeHtml(compactText(resolveReason, 220))}</div><div class="item-meta">${escapeHtml(entry.result || "-")}</div></div>`,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderAuditDetail(entry) {
  const metadata = auditMetadata(entry);
  const chips = [
    { label: "actor", value: entry.user_id || metadata.actor_user_id || "-" },
    { label: "source", value: metadata.source || "-" },
    { label: "result", value: entry.result || "-" },
    { label: "tool", value: metadata.tool_name || metadata.tool_pattern || "-" },
  ].filter((item) => item.value && item.value !== "-");
  return [
    `<div class="detail-section">`,
    `<div class="detail-heading">`,
    `<div>`,
    `<h5>${escapeHtml(entry.event_type || "audit")}</h5>`,
    `<div class="detail-meta mono">${escapeHtml([entry.entry_id || "-", formatTimestamp(entry.timestamp), entry.session_id || metadata.target_session_id || "-"].join(" · "))}</div>`,
    `</div>`,
    auditResultTag(entry),
    `</div>`,
    `</div>`,
    renderDetailChips(chips),
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Actor</div><div class="mono">${escapeHtml(entry.user_id || metadata.actor_user_id || "-")}</div><div class="item-meta">${escapeHtml(metadata.source || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Session</div><div class="mono">${escapeHtml(entry.session_id || metadata.target_session_id || "-")}</div><div class="item-meta">${escapeHtml(entry.action || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Tool</div><div>${escapeHtml(metadata.tool_name || metadata.tool_pattern || "-")}</div><div class="item-meta mono">${escapeHtml(entry.resource || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Result</div><div>${escapeHtml(entry.result || "-")}</div><div class="item-meta">${escapeHtml(metadata.resolve_reason || "-")}</div></div>`,
    `</div>`,
    renderApprovalAuditSummary(entry),
    entry.event_type === "tool_approval" ? renderScopeTransition(entry) : "",
    `<div class="detail-section">`,
    `<div class="k">Metadata</div>`,
    `<pre class="detail-pre">${formatJsonBlock(metadata)}</pre>`,
    `</div>`,
  ].join("");
}

function renderAuditDetailPanel() {
  const badge = auditState.selectedEntry?.entry_id || "none selected";
  const html = auditState.selectedEntry
    ? renderAuditDetail(auditState.selectedEntry)
    : "Select an audit event to inspect actor, source, result, and before/after scope changes.";
  if (auditDetailBadgeEl) auditDetailBadgeEl.textContent = badge;
  if (auditDetailPanelEl) {
    auditDetailPanelEl.classList.toggle("selection-detail-empty", !auditState.selectedEntry);
    auditDetailPanelEl.innerHTML = html;
  }
}

function updateAuditUi() {
  if (auditCurrentSessionEl) {
    auditCurrentSessionEl.textContent = currentSessionId || "-";
  }
  if (auditMatchCountEl) {
    auditMatchCountEl.textContent = String(auditState.total || 0);
  }
  if (auditCaptionEl) {
    auditCaptionEl.textContent = paginationLabel(
      auditState.total,
      auditState.page,
      auditState.pageSize,
      auditState.entries.length,
    );
  }
  updatePagerButtons(auditPrevBtn, auditNextBtn, auditState.page, auditState.hasMore);
  renderCompactList(
    auditListEl,
    auditState.entries,
    renderAuditListItem,
    "No audit events matched these filters."
  );
  renderAuditDetailPanel();
}

function selectAuditEntry(entryId) {
  auditState.selectedEntryId = entryId || null;
  auditState.selectedEntry = auditState.entries.find((entry) => entry.entry_id === entryId) || null;
  updateAuditUi();
}

function openAuditView(filters = {}) {
  if (Object.prototype.hasOwnProperty.call(filters, "searchQuery")) {
    auditState.searchQuery = filters.searchQuery || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "actorFilter")) {
    auditState.actorFilter = filters.actorFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "sessionFilter")) {
    auditState.sessionFilter = filters.sessionFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "toolFilter")) {
    auditState.toolFilter = filters.toolFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "sourceFilter")) {
    auditState.sourceFilter = filters.sourceFilter || "";
  }
  if (Object.prototype.hasOwnProperty.call(filters, "resultFilter")) {
    auditState.resultFilter = filters.resultFilter || "";
  }
  auditState.focus = normalizeAuditFocus(filters.focus);
  auditState.page = 1;
  auditState.autoSelectFirst = true;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  syncAuditInputsFromState();
  activateTab("audit");
  scheduleAuditRefresh(0);
}

function renderApprovalListItem(item) {
  const title = item.tool_name || item.tool_pattern || "approval";
  const sessionScope = item.scope === "session" ? "session" : "single";
  const meta = [item.agent_name || "-", sessionScope, formatTimestamp(item.created_at)].join(" · ");
  const detail = item.reason || item.resolve_reason || "";
  const scopeBits = [];
  if (item.tool_pattern && item.tool_pattern !== item.tool_name) scopeBits.push(`tool=${item.tool_pattern}`);
  if (item.path_scope) scopeBits.push(`path=${item.path_scope}`);
  if (item.propagate_to_subagents) scopeBits.push("subagents");
  if (item.source_request_id) scopeBits.push(`source=${item.source_request_id}`);
  const extra = scopeBits.length ? `<div class="item-meta mono">${escapeHtml(scopeBits.join(" · "))}</div>` : "";
  return [
    "<li>",
    `<button class="list-item-button${dashboardState.selectedKind === "approval" && dashboardState.selectedId === item.request_id ? " active" : ""}" type="button" data-approval-id="${escapeAttr(item.request_id || "")}">`,
    `<div class="item-card">`,
    `<div class="item-head">`,
    `<div class="item-title">${escapeHtml(title)}</div>`,
    statusTag(item.state || "pending"),
    "</div>",
    `<div class="item-meta">${escapeHtml(meta)}</div>`,
    detail ? `<div class="item-detail">${escapeHtml(compactText(detail))}</div>` : "",
    extra,
    `</div>`,
    `</button>`,
    "</li>"
  ].join("");
}

function renderTaskListItem(task) {
  const metaBits = [task.kind || "-", formatTimestamp(task.updated_at)];
  if (task.task_id) metaBits.unshift(task.task_id);
  const detailBits = [];
  if (task.run_id) detailBits.push(`run=${task.run_id}`);
  if (task.winner_task_id) detailBits.push(`winner=${task.winner_task_id}`);
  if (Array.isArray(task.loser_task_ids) && task.loser_task_ids.length) {
    detailBits.push(`losers=${task.loser_task_ids.length}`);
  }
  if (Array.isArray(task.approval_dependencies) && task.approval_dependencies.length) {
    detailBits.push(`approvals=${task.approval_dependencies.length}`);
  }
  if (task.error) detailBits.push(`error=${task.error}`);
  const artifactKeys = task.artifacts && typeof task.artifacts === "object"
    ? Object.keys(task.artifacts).slice(0, 4)
    : [];
  return [
    "<li>",
    `<button class="list-item-button${dashboardState.selectedKind === "task" && dashboardState.selectedId === task.task_id ? " active" : ""}" type="button" data-task-id="${escapeAttr(task.task_id || "")}">`,
    `<div class="item-card">`,
    `<div class="item-head">`,
    `<div class="item-title">${escapeHtml(task.title || task.kind || task.task_id || "task")}</div>`,
    statusTag(task.status || "unknown"),
    "</div>",
    `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
    detailBits.length ? `<div class="item-detail mono">${escapeHtml(compactText(detailBits.join(" · "), 220))}</div>` : "",
    artifactKeys.length ? `<div class="item-meta mono">artifacts=${escapeHtml(artifactKeys.join(", "))}</div>` : "",
    `</div>`,
    `</button>`,
    "</li>"
  ].join("");
}

function renderCompactList(targetEl, items, renderer, emptyText) {
  if (!targetEl) return;
  if (!items.length) {
    targetEl.innerHTML = `<li class="muted">${escapeHtml(emptyText)}</li>`;
    return;
  }
  targetEl.innerHTML = items.map((item) => renderer(item)).join("");
}

function updateDashboardUi() {
  const pendingCount = dashboardState.pendingApprovalsTotal;
  const openTaskCount = dashboardState.openTaskCount;
  const filteredApprovals = dashboardState.dashboardApprovals || [];
  const filteredTasks = dashboardState.dashboardTasks || [];
  if (dashboardSessionBackendEl) {
    dashboardSessionBackendEl.textContent = dashboardState.sessionBackend || "-";
  }
  if (dashboardSessionNamespaceEl) {
    dashboardSessionNamespaceEl.textContent = dashboardState.sessionNamespace || "-";
  }
  if (dashboardPendingApprovalsEl) {
    dashboardPendingApprovalsEl.textContent = String(pendingCount);
  }
  if (dashboardOpenTasksEl) {
    dashboardOpenTasksEl.textContent = String(openTaskCount);
  }
  if (dashboardApprovalsCaptionEl) {
    dashboardApprovalsCaptionEl.textContent = paginationLabel(
      dashboardState.approvalTotal,
      dashboardState.approvalPage,
      dashboardState.approvalPageSize,
      filteredApprovals.length,
    );
  }
  if (dashboardTasksCaptionEl) {
    const label = paginationLabel(
      dashboardState.taskTotal,
      dashboardState.taskPage,
      dashboardState.taskPageSize,
      filteredTasks.length,
    );
    dashboardTasksCaptionEl.textContent = currentSessionId
      ? `${label} · ${currentSessionId}`
      : label;
  }
  if (inspectorSessionBackendEl) {
    inspectorSessionBackendEl.textContent = dashboardState.sessionBackend || "-";
  }
  if (inspectorCurrentSessionEl) {
    inspectorCurrentSessionEl.textContent = currentSessionId || "-";
  }
  if (inspectorPendingApprovalsEl) {
    inspectorPendingApprovalsEl.textContent = String(pendingCount);
  }
  if (inspectorOpenTasksEl) {
    inspectorOpenTasksEl.textContent = String(openTaskCount);
  }
  if (inspectorApprovalCountBadgeEl) {
    inspectorApprovalCountBadgeEl.textContent = String(pendingCount);
  }
  if (inspectorTaskCountBadgeEl) {
    inspectorTaskCountBadgeEl.textContent = String(dashboardState.recentTasksTotal);
  }
  updatePagerButtons(
    dashboardApprovalsPrevBtn,
    dashboardApprovalsNextBtn,
    dashboardState.approvalPage,
    dashboardState.approvalHasMore,
  );
  updatePagerButtons(
    dashboardTasksPrevBtn,
    dashboardTasksNextBtn,
    dashboardState.taskPage,
    dashboardState.taskHasMore,
  );

  renderCompactList(
    inspectorApprovalsListEl,
    dashboardState.pendingApprovals,
    renderApprovalListItem,
    "No pending approvals."
  );
  renderCompactList(
    inspectorTasksListEl,
    dashboardState.recentTasks.slice(0, 5),
    renderTaskListItem,
    "No recent tasks."
  );
  renderCompactList(
    dashboardApprovalsListEl,
    filteredApprovals,
    renderApprovalListItem,
    "No approvals yet."
  );
  renderCompactList(
    dashboardTasksListEl,
    filteredTasks,
    renderTaskListItem,
    "No tasks yet."
  );

  renderSelectionDetail();
}

function renderRelationChips(kind, items, emptyText) {
  if (!items.length) {
    return `<div class="muted">${escapeHtml(emptyText)}</div>`;
  }
  return [
    `<div class="relation-list">`,
    ...items.map((item) => {
      if (kind === "task") {
        return `<button class="relation-chip mono" type="button" data-task-ref="${escapeAttr(item.task_id || "")}">${escapeHtml(item.title || item.task_id || "task")}</button>`;
      }
      return `<button class="relation-chip mono" type="button" data-approval-ref="${escapeAttr(item.request_id || "")}">${escapeHtml(item.tool_name || item.request_id || "approval")}</button>`;
    }),
    `</div>`
  ].join("");
}

function renderReuseSuggestions(reuseSuggestions) {
  if (!Array.isArray(reuseSuggestions) || !reuseSuggestions.length) {
    return "";
  }
  const items = reuseSuggestions.map((item) => {
    const metaBits = [];
    if (item.memory_id != null) metaBits.push(`#${item.memory_id}`);
    if (item.score != null) metaBits.push(`score=${Number(item.score).toFixed(3)}`);
    const diffStat = item.metadata && item.metadata.diff_stat ? String(item.metadata.diff_stat) : "";
    return [
      `<div class="detail-card">`,
      `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
      `<div>${escapeHtml(compactText(item.content, 240))}</div>`,
      diffStat ? `<div class="item-meta mono">${escapeHtml(diffStat)}</div>` : "",
      `</div>`
    ].join("");
  });
  return [
    `<div class="detail-section">`,
    `<div class="k">Reusable Approved Improvements</div>`,
    `<div class="detail-grid">`,
    ...items,
    `</div>`,
    `</div>`
  ].join("");
}

function renderTaskTimeline(entries, pagination) {
  const items = Array.isArray(entries) ? entries : [];
  if (!items.length) {
    return `<div class="muted">No timeline events yet.</div>`;
  }
  const caption = pagination && pagination.total
    ? `<div class="item-meta mono">${escapeHtml(paginationLabel(pagination.total, pagination.page || 1, pagination.page_size || items.length, items.length))}</div>`
    : "";
  const rows = items.map((entry) => {
    const kind = String(entry.kind || "timeline");
    const title = String(entry.title || entry.event_type || kind);
    const summary = compactText(entry.summary || "", 220);
    const metaBits = [formatTimestamp(entry.timestamp)];
    if (entry.event_type) metaBits.push(String(entry.event_type));
    if (entry.request_id) metaBits.push(String(entry.request_id));
    if (entry.audit_entry_id) metaBits.push(String(entry.audit_entry_id));
    const body = [
      `<div class="timeline-entry-card detail-card">`,
      `<div class="item-head">`,
      `<div class="item-title">${escapeHtml(title)}</div>`,
      statusTag(entry.status || kind),
      `</div>`,
      `<div class="item-meta mono">${escapeHtml(metaBits.join(" · "))}</div>`,
      summary ? `<div class="item-detail">${escapeHtml(summary)}</div>` : "",
      `</div>`,
    ].join("");
    if (kind === "approval" && entry.request_id) {
      return `<button class="timeline-entry-button" type="button" data-approval-ref="${escapeAttr(entry.request_id)}">${body}</button>`;
    }
    if (kind === "audit" && entry.audit_entry_id) {
      const focus = entry.audit_focus || {};
      return [
        `<button class="timeline-entry-button" type="button"`,
        ` data-action="open-related-audit"`,
        ` data-audit-session-id="${escapeAttr(focus.sessionId || "")}"`,
        ` data-audit-query="${escapeAttr(focus.searchQuery || "")}"`,
        ` data-audit-request-id="${escapeAttr(focus.requestId || "")}"`,
        ` data-audit-task-id="${escapeAttr(focus.taskId || "")}"`,
        ` data-audit-run-id="${escapeAttr(focus.runId || "")}"`,
        ` data-audit-tool="${escapeAttr(focus.toolName || "")}"`,
        ` data-audit-source="${escapeAttr(focus.source || "")}"`,
        ` data-audit-result="${escapeAttr(focus.result || "")}"`,
        ` data-audit-entry-id="${escapeAttr(entry.audit_entry_id)}">`,
        body,
        `</button>`,
      ].join("");
    }
    return `<div class="timeline-entry-static">${body}</div>`;
  });
  return [
    `<div class="detail-section">`,
    `<div class="k">Task Timeline</div>`,
    caption,
    `<div class="timeline-list">`,
    ...rows,
    `</div>`,
    `</div>`,
  ].join("");
}

function renderTaskComparison(comparison) {
  if (!comparison || typeof comparison !== "object") {
    return "";
  }
  const leftTask = comparison.left_task || {};
  const rightTask = comparison.right_task || {};
  const left = comparison.left || {};
  const right = comparison.right || {};
  const leftResult = left.result || {};
  const rightResult = right.result || {};
  const stepCompare = comparison.step_compare || {};
  const stepRows = Array.isArray(stepCompare.rows) ? stepCompare.rows : [];
  const summaryItems = Array.isArray(comparison.summary) ? comparison.summary : [];
  const summaryHtml = summaryItems.length
    ? `<ul class="detail-list">${summaryItems.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}</ul>`
    : `<div class="muted">No comparison summary.</div>`;
  const leftIsReplay = Boolean(
    leftTask.parent_task_id
    && rightTask.task_id
    && leftTask.parent_task_id === rightTask.task_id
  );
  const rightIsReplay = Boolean(
    rightTask.parent_task_id
    && leftTask.task_id
    && rightTask.parent_task_id === leftTask.task_id
  );
  const leftLabel = leftIsReplay ? "Replay" : "Baseline";
  const rightLabel = rightIsReplay ? "Replay" : "Baseline";
  const renderStepRow = (row) => {
    const leftStep = row.left || {};
    const rightStep = row.right || {};
    return [
      `<div class="detail-card">`,
      `<div class="detail-heading">`,
      `<div>`,
      `<div class="k">${escapeHtml(row.title || row.step_id || "step")}</div>`,
      `<div class="item-meta mono">${escapeHtml(row.step_id || "-")}</div>`,
      `</div>`,
      row.changed ? `<span class="tag warning">changed</span>` : `<span class="tag">same</span>`,
      `</div>`,
      `<div class="item-detail">${escapeHtml(`${leftStep.status || "-"} -> ${rightStep.status || "-"}`)}</div>`,
      `<div class="item-meta">${escapeHtml(`${leftStep.output_summary || "-"} -> ${rightStep.output_summary || "-"}`)}</div>`,
      `<div class="item-meta">${escapeHtml(`failed: ${(leftStep.failed_criteria || []).join(", ") || "-"} -> ${(rightStep.failed_criteria || []).join(", ") || "-"}`)}</div>`,
      `</div>`,
    ].join("");
  };
  const renderSide = (label, task, snapshot) => [
    `<div class="detail-card">`,
    `<div class="k">${escapeHtml(label)}</div>`,
    `<div class="item-title">${escapeHtml(task.title || task.task_id || label)}</div>`,
    `<div class="item-meta mono">${escapeHtml([task.task_id || "-", task.status || "-", snapshot.verification_status || "-"].join(" · "))}</div>`,
    `<div class="item-detail">score=${escapeHtml(String((snapshot.overall_score || 0).toFixed ? snapshot.overall_score.toFixed(2) : snapshot.overall_score || 0))} repairs=${escapeHtml(String(snapshot.repair_count ?? "-"))}</div>`,
    snapshot.failed_criteria?.length
      ? `<div class="item-meta">${escapeHtml(`failed: ${snapshot.failed_criteria.join(", ")}`)}</div>`
      : `<div class="item-meta">failed: -</div>`,
    snapshot.screenshot_refs?.length
      ? `<div class="item-meta mono">${escapeHtml(snapshot.screenshot_refs.join(" · "))}</div>`
      : `<div class="item-meta mono">screenshots: -</div>`,
    `<pre class="detail-pre">${formatJsonBlock(snapshot.verification_report || {})}</pre>`,
    `</div>`,
  ].join("");

  return [
    `<div class="detail-section">`,
    `<div class="k">Replay Compare</div>`,
    summaryHtml,
    `<div class="detail-grid">`,
    renderSide(leftLabel, leftTask, leftResult),
    renderSide(rightLabel, rightTask, rightResult),
    `</div>`,
    `<div class="k">Step Diff</div>`,
    stepRows.length
      ? `<div class="detail-grid">${stepRows.map(renderStepRow).join("")}</div>`
      : `<div class="muted">No step-level diff available.</div>`,
    `</div>`,
  ].join("");
}

function renderTaskDetail(task) {
  const relatedTasks = dashboardState.relatedTasks || [];
  const childTasks = dashboardState.childTasks || [];
  const relatedApprovals = dashboardState.relatedApprovals || [];
  const subagentRun = dashboardState.subagentRun;
  const artifacts = task.artifacts && typeof task.artifacts === "object" ? task.artifacts : {};
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const reuseSuggestions = Array.isArray(artifacts.reuse_suggestions) ? artifacts.reuse_suggestions : [];
  const taskTimeline = Array.isArray(dashboardState.taskTimeline) ? dashboardState.taskTimeline : [];
  const taskTimelinePagination = dashboardState.taskTimelinePagination;
  const taskComparison = dashboardState.taskComparison;
  const resultSnapshot = artifacts.result && typeof artifacts.result === "object" ? artifacts.result : {};
  const stepTrace = Array.isArray(resultSnapshot.step_trace) ? resultSnapshot.step_trace : [];
  const tailReplayFromStep = String(resultSnapshot.tail_replay_from_step_id || "");
  const detailMeta = [
    task.task_id,
    task.kind || "-",
    `updated ${formatTimestamp(task.updated_at)}`,
  ].filter(Boolean);
  const subagentArtifacts = artifacts.subagent && typeof artifacts.subagent === "object" ? artifacts.subagent : {};
  const isSessionSubagent = task.kind === "subagent" && String(subagentArtifacts.mode || "").toLowerCase() === "session";
  const canKillSubagent = Boolean(subagentRun && ["accepted", "running", "idle"].includes(String(subagentRun.status || "").toLowerCase()));
  const auditQuery = task.run_id || task.task_id || task.title || "";
  const canReplay = task.kind === "control_loop" && Boolean(artifacts.resume_context?.goal || task.title);
  const renderStepTrace = () => {
    if (!stepTrace.length) {
      return `<div class="muted">No step trace recorded yet.</div>`;
    }
    return `<div class="detail-grid">${stepTrace.map((step) => {
      const stepId = String(step.step_id || "");
      const canReplayFromHere = canReplay && String(step.step_type || "") === "plan" && stepId;
      return [
        `<div class="detail-card">`,
        `<div class="detail-heading">`,
        `<div>`,
        `<div class="k">${escapeHtml(step.title || stepId || "step")}</div>`,
        `<div class="item-meta mono">${escapeHtml(stepId || "-")}</div>`,
        `</div>`,
        statusTag(step.status || "unknown"),
        `</div>`,
        `<div class="item-meta">${escapeHtml(step.output_summary || step.description || "-")}</div>`,
        `<div class="item-meta">${escapeHtml(`scope=${step.replay_scope || "-"} failed=${(step.failed_criteria || []).join(", ") || "-"}`)}</div>`,
        canReplayFromHere
          ? `<div class="detail-actions"><button class="btn" type="button" data-action="task-replay-from-step" data-task-id="${escapeAttr(task.task_id || "")}" data-from-step="${escapeAttr(stepId)}">Replay From Here</button></div>`
          : "",
        `</div>`,
      ].join("");
    }).join("")}</div>`;
  };
  const compareTarget = task.parent_task_id || childTasks[0]?.task_id || "";
  const compareLabel = task.parent_task_id ? "Compare With Parent" : (childTasks[0] ? "Compare With Latest Replay" : "");

  return [
    `<div class="detail-section">`,
    `<div class="detail-heading">`,
    `<div>`,
    `<h5>${escapeHtml(task.title || task.task_id || "task")}</h5>`,
    `<div class="detail-meta mono">${escapeHtml(detailMeta.join(" · "))}</div>`,
    `</div>`,
    statusTag(task.status || "unknown"),
    `</div>`,
    `</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Owner</div><div class="mono">${escapeHtml(task.owner_session_id || "-")}</div><div class="item-meta">${escapeHtml(task.owner_user_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Run</div><div class="mono">${escapeHtml(task.run_id || "-")}</div><div class="item-meta">${escapeHtml(formatTimestamp(task.created_at))}</div></div>`,
    `<div class="detail-card"><div class="k">Started</div><div class="mono">${escapeHtml(formatTimestamp(task.started_at))}</div><div class="item-meta">ended ${escapeHtml(formatTimestamp(task.ended_at))}</div></div>`,
    `<div class="detail-card"><div class="k">Error</div><div class="detail-error mono">${escapeHtml(task.error || "-")}</div></div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Related Tasks</div>`,
    renderRelationChips("task", relatedTasks.concat(childTasks), "No linked tasks."),
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Approval Dependencies</div>`,
    renderRelationChips("approval", relatedApprovals, "No linked approvals."),
    `</div>`,
    renderTaskTimeline(taskTimeline, taskTimelinePagination),
    `<div class="detail-section">`,
    `<div class="k">Audit Trail</div>`,
    `<div class="detail-actions">`,
    `<button class="btn" type="button" data-action="open-related-audit" data-audit-session-id="${escapeAttr(task.owner_session_id || "")}" data-audit-query="${escapeAttr(auditQuery)}" data-audit-task-id="${escapeAttr(task.task_id || "")}" data-audit-run-id="${escapeAttr(task.run_id || "")}">Open Related Audit</button>`,
    canReplay ? `<button class="btn primary" type="button" data-action="task-replay" data-task-id="${escapeAttr(task.task_id || "")}">Replay Task</button>` : "",
    (canReplay && tailReplayFromStep) ? `<button class="btn" type="button" data-action="task-replay-from-step" data-task-id="${escapeAttr(task.task_id || "")}" data-from-step="${escapeAttr(tailReplayFromStep)}">Replay Verification Tail</button>` : "",
    compareTarget ? `<button class="btn" type="button" data-action="task-compare" data-task-id="${escapeAttr(task.task_id || "")}" data-other-task-id="${escapeAttr(compareTarget)}">${escapeHtml(compareLabel)}</button>` : "",
    `</div>`,
    `</div>`,
    renderTaskComparison(taskComparison),
    `<div class="detail-section">`,
    `<div class="k">Step Trace</div>`,
    renderStepTrace(),
    `</div>`,
    subagentRun ? [
      `<div class="detail-section">`,
      `<div class="k">Subagent Run</div>`,
      `<div class="detail-grid">`,
      `<div class="detail-card"><div class="k">Run Status</div><div>${statusTag(subagentRun.status || "unknown")}</div><div class="item-meta mono">${escapeHtml(subagentRun.run_id || "-")}</div></div>`,
      `<div class="detail-card"><div class="k">Mode</div><div>${escapeHtml(subagentRun.mode || "-")}</div><div class="item-meta mono">pending=${escapeHtml(String(subagentRun.pending_messages ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Subagent Session</div><div class="mono">${escapeHtml(subagentRun.session_id || "-")}</div><div class="item-meta mono">processed=${escapeHtml(String(subagentRun.messages_processed ?? "-"))}</div></div>`,
      `<div class="detail-card"><div class="k">Current Task</div><div>${escapeHtml(compactText(subagentRun.current_task || "-", 180))}</div></div>`,
      `</div>`,
      `<div class="detail-form">`,
      isSessionSubagent ? `<textarea class="mono" rows="3" data-role="steer-message" placeholder="追加の指示を送る..."></textarea>` : `<div class="muted">This subagent was not started in session mode, so steer is unavailable.</div>`,
      `<div class="detail-actions">`,
      `<button class="btn primary" type="button" data-action="subagent-steer" data-run-id="${escapeAttr(subagentRun.run_id || "")}" ${isSessionSubagent ? "" : "disabled"}>Steer</button>`,
      `<button class="btn danger" type="button" data-action="subagent-kill" data-run-id="${escapeAttr(subagentRun.run_id || "")}" ${canKillSubagent ? "" : "disabled"}>Kill</button>`,
      `</div>`,
      `</div>`,
      `</div>`
    ].join("") : "",
    renderReuseSuggestions(reuseSuggestions),
    artifacts.resume_context ? [
      `<div class="detail-section">`,
      `<div class="k">Resume Context</div>`,
      `<pre class="detail-pre">${formatJsonBlock(artifacts.resume_context)}</pre>`,
      `</div>`
    ].join("") : "",
    `<div class="detail-section">`,
    `<div class="k">Artifacts</div>`,
    `<pre class="detail-pre">${formatJsonBlock(artifacts)}</pre>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Metadata</div>`,
    `<pre class="detail-pre">${formatJsonBlock(metadata)}</pre>`,
    `</div>`
  ].join("");
}

function renderApprovalDetail(approval) {
  const relatedTasks = dashboardState.relatedTasks || [];
  const suggestions = Array.isArray(dashboardState.selectedApprovalSuggestions)
    ? dashboardState.selectedApprovalSuggestions
    : [];
  const history = Array.isArray(approval.history) ? approval.history : [];
  const historyText = history.map((entry) => {
    const reason = entry.reason ? ` · ${entry.reason}` : "";
    const metadata = entry.metadata && Object.keys(entry.metadata).length
      ? ` · ${JSON.stringify(entry.metadata)}`
      : "";
    return `${new Date((entry.ts || 0) * 1000).toLocaleString()} · ${entry.state}${reason}${metadata}`;
  }).join("\n");

  return [
    `<div class="detail-section">`,
    `<div class="detail-heading">`,
    `<div>`,
    `<h5>${escapeHtml(approval.tool_name || approval.request_id || "approval")}</h5>`,
    `<div class="detail-meta mono">${escapeHtml([approval.request_id, approval.agent_name || "-", formatTimestamp(approval.created_at)].filter(Boolean).join(" · "))}</div>`,
    `</div>`,
    statusTag(approval.state || "pending"),
    `</div>`,
    `</div>`,
    `<div class="detail-grid">`,
    `<div class="detail-card"><div class="k">Scope</div><div>${escapeHtml(approval.scope || "-")}</div><div class="item-meta mono">${escapeHtml(approval.tool_pattern || approval.tool_name || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Path Scope</div><div class="mono">${escapeHtml(approval.path_scope || "-")}</div><div class="item-meta">${approval.propagate_to_subagents ? "propagates to subagents" : "local only"}</div></div>`,
    `<div class="detail-card"><div class="k">Session</div><div class="mono">${escapeHtml(approval.session_id || "-")}</div><div class="item-meta mono">${escapeHtml(approval.source_request_id || "-")}</div></div>`,
    `<div class="detail-card"><div class="k">Reason</div><div>${escapeHtml(compactText(approval.reason || approval.resolve_reason || "-", 200))}</div></div>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Related Tasks</div>`,
    renderRelationChips("task", relatedTasks, "No related tasks."),
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">Audit Trail</div>`,
    `<div class="detail-actions">`,
    `<button class="btn" type="button" data-action="open-related-audit" data-audit-session-id="${escapeAttr(approval.session_id || "")}" data-audit-query="${escapeAttr(approval.request_id || approval.source_request_id || "")}" data-audit-request-id="${escapeAttr(approval.request_id || approval.source_request_id || "")}" data-audit-tool="${escapeAttr(approval.tool_name || approval.tool_pattern || "")}">Open Related Audit</button>`,
    `</div>`,
    `</div>`,
    (approval.state === "pending" || approval.state === "expiring") ? [
      `<div class="detail-section">`,
      `<div class="k">Resolve Approval</div>`,
      `<div class="detail-form">`,
      suggestions.length ? `<div class="detail-actions">${suggestions.map((suggestion) => (
        `<button class="btn" type="button" data-action="approval-resolve-bundle" data-request-id="${escapeAttr(approval.request_id || "")}" data-strategy="${escapeAttr(suggestion.strategy || "")}">${escapeHtml(`${suggestion.label} (${suggestion.affected_count || 1})`)}</button>`
      )).join("")}</div>` : "",
      `<div class="detail-form-grid">`,
      `<label class="field-label">scope<select class="text-input mono" data-approval-field="scope"><option value="single"${approval.scope === "single" ? " selected" : ""}>single</option><option value="session"${approval.scope === "session" ? " selected" : ""}>session</option></select></label>`,
      `<label class="field-label">tool pattern<input class="text-input mono" type="text" value="${escapeAttr(approval.tool_pattern || approval.tool_name || "")}" data-approval-field="tool-pattern" /></label>`,
      `<label class="field-label">path scope<input class="text-input mono" type="text" value="${escapeAttr(approval.path_scope || "")}" data-approval-field="path-scope" /></label>`,
      `<label class="field-label"><input type="checkbox" data-approval-field="propagate"${approval.propagate_to_subagents ? " checked" : ""} /> propagate to subagents</label>`,
      `</div>`,
      `<div class="detail-actions">`,
      `<button class="btn primary" type="button" data-action="approval-resolve" data-approved="true" data-request-id="${escapeAttr(approval.request_id || "")}">Approve</button>`,
      `<button class="btn danger" type="button" data-action="approval-resolve" data-approved="false" data-request-id="${escapeAttr(approval.request_id || "")}">Deny</button>`,
      `</div>`,
      `</div>`,
      `</div>`
    ].join("") : "",
    `<div class="detail-section">`,
    `<div class="k">Arguments</div>`,
    `<pre class="detail-pre">${formatJsonBlock(approval.args || {})}</pre>`,
    `</div>`,
    `<div class="detail-section">`,
    `<div class="k">History</div>`,
    `<pre class="detail-pre">${escapeHtml(historyText || "No approval history yet.")}</pre>`,
    `</div>`
  ].join("");
}

function renderSelectionDetail() {
  let badge = "none";
  let html = "Click a task or approval to inspect full metadata, links, and actions.";
  if (dashboardState.selectedKind === "task" && dashboardState.selectedTask) {
    badge = dashboardState.selectedTask.task_id || "task";
    html = renderTaskDetail(dashboardState.selectedTask);
  } else if (dashboardState.selectedKind === "approval" && dashboardState.selectedApproval) {
    badge = dashboardState.selectedApproval.request_id || "approval";
    html = renderApprovalDetail(dashboardState.selectedApproval);
  }
  if (dashboardDetailBadgeEl) {
    dashboardDetailBadgeEl.textContent = badge;
  }
  if (inspectorSelectionBadgeEl) {
    inspectorSelectionBadgeEl.textContent = badge;
  }
  for (const target of [dashboardDetailPanelEl, inspectorSelectionDetailEl]) {
    if (!target) continue;
    target.classList.toggle("selection-detail-empty", badge === "none");
    target.innerHTML = html;
  }
}

async function fetchJsonOrThrow(url, init = {}) {
  const response = await apiFetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data?.detail || `HTTP ${response.status}`);
  }
  return data;
}

async function loadTaskDetail(taskId) {
  if (!taskId) return;
  const base = toHttpBaseUrl(currentSettings());
  try {
    const taskPayload = await fetchJsonOrThrow(`${base}/tasks/${encodeURIComponent(taskId)}`);
    const task = taskPayload.task || {};
    const relatedTaskIds = [
      task.parent_task_id,
      task.winner_task_id,
      ...(Array.isArray(task.loser_task_ids) ? task.loser_task_ids : []),
    ].filter(Boolean);
    const [relatedTasksPayload, relatedApprovals, subagentRunsPayload, childTasksPayload, timelinePayload] = await Promise.all([
      Promise.all(
        Array.from(new Set(relatedTaskIds)).map(async (relatedTaskId) => {
          try {
            const payload = await fetchJsonOrThrow(`${base}/tasks/${encodeURIComponent(relatedTaskId)}`);
            return payload.task || null;
          } catch (_) {
            return null;
          }
        })
      ),
      Promise.all(
        (Array.isArray(task.approval_dependencies) ? task.approval_dependencies : []).map(async (approvalId) => {
          try {
            const payload = await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(approvalId)}`);
            return payload.approval || null;
          } catch (_) {
            return null;
          }
        })
      ),
      task.kind === "subagent" && task.owner_session_id
        ? fetchJsonOrThrow(`${base}/subagents/${encodeURIComponent(task.owner_session_id)}`).catch(() => ({ runs: [] }))
        : Promise.resolve({ runs: [] }),
      fetchJsonOrThrow(
        `${base}/tasks?${new URLSearchParams({ session_id: task.owner_session_id || "", parent_task_id: task.task_id || "", limit: "50" })}`
      ).catch(() => ({ tasks: [] })),
      fetchJsonOrThrow(
        `${base}/tasks/${encodeURIComponent(task.task_id || taskId)}/timeline?${new URLSearchParams({ limit: "80" })}`
      ).catch(() => ({ entries: [], pagination: null })),
    ]);
    const runs = Array.isArray(subagentRunsPayload.runs) ? subagentRunsPayload.runs : [];
    dashboardState.selectedKind = "task";
    dashboardState.selectedId = taskId;
    dashboardState.selectedTask = task;
    dashboardState.selectedApproval = null;
    dashboardState.relatedTasks = relatedTasksPayload.filter(Boolean);
    dashboardState.relatedApprovals = relatedApprovals.filter(Boolean);
    dashboardState.childTasks = Array.isArray(childTasksPayload.tasks) ? childTasksPayload.tasks : [];
    dashboardState.subagentRun = runs.find((run) => run.run_id === task.run_id) || null;
    dashboardState.taskTimeline = Array.isArray(timelinePayload.entries) ? timelinePayload.entries : [];
    dashboardState.taskTimelinePagination = timelinePayload.pagination || null;
    dashboardState.selectedApprovalSuggestions = [];
    dashboardState.taskComparison = null;
  } catch (err) {
    dashboardState.selectedKind = "task";
    dashboardState.selectedId = taskId;
    dashboardState.selectedTask = {
      task_id: taskId,
      title: "Failed to load task",
      status: "failed",
      error: String(err),
      artifacts: {},
      metadata: {},
    };
    dashboardState.selectedApproval = null;
    dashboardState.relatedTasks = [];
    dashboardState.relatedApprovals = [];
    dashboardState.childTasks = [];
    dashboardState.subagentRun = null;
    dashboardState.taskTimeline = [];
    dashboardState.taskTimelinePagination = null;
    dashboardState.selectedApprovalSuggestions = [];
    dashboardState.taskComparison = null;
  }
  renderSelectionDetail();
  updateDashboardUi();
}

async function loadApprovalDetail(requestId) {
  if (!requestId) return;
  const base = toHttpBaseUrl(currentSettings());
  try {
    const payload = await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}`);
    const approval = payload.approval || {};
    const suggestions = Array.isArray(payload.resolve_suggestions) ? payload.resolve_suggestions : [];
    const taskPayload = approval.session_id
      ? await fetchJsonOrThrow(
          `${base}/tasks?${new URLSearchParams({ session_id: approval.session_id, limit: "100" })}`
        ).catch(() => ({ tasks: [] }))
      : { tasks: [] };
    const ids = new Set([approval.request_id, approval.source_request_id].filter(Boolean));
    const relatedTasks = (Array.isArray(taskPayload.tasks) ? taskPayload.tasks : []).filter((task) => (
      Array.isArray(task.approval_dependencies)
      && task.approval_dependencies.some((dependency) => ids.has(dependency))
    ));
    dashboardState.selectedKind = "approval";
    dashboardState.selectedId = requestId;
    dashboardState.selectedApproval = approval;
    dashboardState.selectedTask = null;
    dashboardState.relatedTasks = relatedTasks;
    dashboardState.relatedApprovals = [];
    dashboardState.childTasks = [];
    dashboardState.subagentRun = null;
    dashboardState.taskTimeline = [];
    dashboardState.taskTimelinePagination = null;
    dashboardState.selectedApprovalSuggestions = suggestions;
    dashboardState.taskComparison = null;
  } catch (err) {
    dashboardState.selectedKind = "approval";
    dashboardState.selectedId = requestId;
    dashboardState.selectedApproval = {
      request_id: requestId,
      tool_name: "Failed to load approval",
      state: "failed",
      reason: String(err),
      history: [],
      args: {},
    };
    dashboardState.selectedTask = null;
    dashboardState.relatedTasks = [];
    dashboardState.relatedApprovals = [];
    dashboardState.childTasks = [];
    dashboardState.subagentRun = null;
    dashboardState.taskTimeline = [];
    dashboardState.taskTimelinePagination = null;
    dashboardState.selectedApprovalSuggestions = [];
    dashboardState.taskComparison = null;
  }
  renderSelectionDetail();
  updateDashboardUi();
}

async function refreshSelectedDetail() {
  if (dashboardState.selectedKind === "task" && dashboardState.selectedId) {
    await loadTaskDetail(dashboardState.selectedId);
    return;
  }
  if (dashboardState.selectedKind === "approval" && dashboardState.selectedId) {
    await loadApprovalDetail(dashboardState.selectedId);
    return;
  }
  renderSelectionDetail();
}

function scheduleDashboardRefresh(delay = 250) {
  if (_dashboardRefreshHandle) {
    window.clearTimeout(_dashboardRefreshHandle);
  }
  _dashboardRefreshHandle = window.setTimeout(() => {
    _dashboardRefreshHandle = null;
    void refreshDashboard();
  }, delay);
}

async function resolveApprovalFromPanel(container, requestId, approved) {
  const base = toHttpBaseUrl(currentSettings());
  const scope = container.querySelector('[data-approval-field="scope"]')?.value || "single";
  const toolPattern = container.querySelector('[data-approval-field="tool-pattern"]')?.value || "";
  const pathScope = container.querySelector('[data-approval-field="path-scope"]')?.value || "";
  const propagate = Boolean(container.querySelector('[data-approval-field="propagate"]')?.checked);
  const sessionId = dashboardState.selectedApproval?.session_id || currentSessionId || "";
  const reason = approved ? "Approved in Control UI panel" : "Denied in Control UI panel";
  try {
    await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved,
        reason,
        session_id: sessionId,
        scope,
        tool_pattern: toolPattern,
        path_scope: pathScope,
        propagate_to_subagents: propagate,
      }),
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`approval error: ${err}`);
  }
}

async function resolveApprovalBundleFromPanel(container, requestId, strategy) {
  const base = toHttpBaseUrl(currentSettings());
  const sessionId = dashboardState.selectedApproval?.session_id || currentSessionId || "";
  const pathScope = container.querySelector('[data-approval-field="path-scope"]')?.value || "";
  const propagate = Boolean(container.querySelector('[data-approval-field="propagate"]')?.checked);
  try {
    await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}/resolve_bundle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved: true,
        strategy,
        reason: `Approved in Control UI panel (${strategy})`,
        session_id: sessionId,
        path_scope: pathScope,
        propagate_to_subagents: propagate,
      }),
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`approval error: ${err}`);
  }
}

async function steerSubagentFromPanel(container, runId) {
  const base = toHttpBaseUrl(currentSettings());
  const message = container.querySelector('[data-role="steer-message"]')?.value?.trim() || "";
  if (!message) {
    addSystemMessage("steer message is required");
    return;
  }
  try {
    await fetchJsonOrThrow(`${base}/subagents/${encodeURIComponent(runId)}/steer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`subagent steer error: ${err}`);
  }
}

async function killSubagentFromPanel(runId) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    await fetchJsonOrThrow(`${base}/subagents/${encodeURIComponent(runId)}`, {
      method: "DELETE",
    });
    scheduleDashboardRefresh(100);
  } catch (err) {
    addSystemMessage(`subagent kill error: ${err}`);
  }
}

async function replayTaskFromPanel(taskId, fromStep = "") {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const payload = await fetchJsonOrThrow(`${base}/tasks/${encodeURIComponent(taskId)}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fromStep ? { from_step: fromStep } : {}),
    });
    addSystemMessage(
      fromStep
        ? `tail replay accepted from ${fromStep}: ${payload.task?.task_id || "-"}`
        : `replay accepted: ${payload.task?.task_id || "-"}`
    );
    scheduleDashboardRefresh(50);
    if (payload.task?.task_id) {
      void loadTaskDetail(payload.task.task_id);
    }
  } catch (err) {
    addSystemMessage(`task replay error: ${err}`);
  }
}

async function loadTaskComparison(taskId, otherTaskId = "") {
  const base = toHttpBaseUrl(currentSettings());
  const params = new URLSearchParams();
  if (otherTaskId) params.set("other_task_id", otherTaskId);
  try {
    dashboardState.taskComparison = await fetchJsonOrThrow(
      `${base}/tasks/${encodeURIComponent(taskId)}/compare${params.toString() ? `?${params}` : ""}`,
    );
    renderSelectionDetail();
  } catch (err) {
    addSystemMessage(`task compare error: ${err}`);
  }
}

function renderAnalytics(data) {
  const overview = data.overview || {};
  const rankingPayload = data.step_failure_ranking || {};
  const ranking = Array.isArray(rankingPayload.steps) ? rankingPayload.steps : [];
  const rankingTruncated = Boolean(rankingPayload.truncated);
  const rankingSampled = Number(rankingPayload.sampled_events || 0);
  const rankingTotal = Number(rankingPayload.total_events || 0);
  const improvementPayload = data.replay_improvement || {};
  const improvement = Array.isArray(improvementPayload.steps) ? improvementPayload.steps : [];
  const improvementTruncated = Boolean(improvementPayload.truncated);
  const byStatus = overview.by_status || {};
  const statusEntries = Object.entries(byStatus)
    .sort(([, a], [, b]) => b - a)
    .map(([k, v]) => `<span class="tag">${escapeHtml(k)}: ${v}</span>`)
    .join(" ");
  const rankingRows = ranking.map((item) => [
    `<tr>`,
    `<td class="mono">${escapeHtml(item.step_id || "-")}</td>`,
    `<td>${escapeHtml(item.title || "-")}</td>`,
    `<td>${item.total}</td>`,
    `<td>${item.succeeded}</td>`,
    `<td>${item.failed}</td>`,
    `<td class="${item.failure_rate > 0.5 ? "text-danger" : ""}">${(item.failure_rate * 100).toFixed(1)}%</td>`,
    `<td>${item.task_count}</td>`,
    `<td class="mono">${(item.top_failed_criteria || []).map((c) => `${escapeHtml(c.name)}(${c.count})`).join(", ") || "-"}</td>`,
    `</tr>`,
  ].join("")).join("");
  const improvementRows = improvement.map((item) => [
    `<tr>`,
    `<td class="mono">${escapeHtml(item.step_id || "-")}</td>`,
    `<td>${escapeHtml(item.title || "-")}</td>`,
    `<td>${item.source_fail}</td>`,
    `<td>${item.replay_pass}</td>`,
    `<td>${item.replay_fail}</td>`,
    `<td class="${item.improvement_rate > 0.5 ? "text-success" : ""}">${(item.improvement_rate * 100).toFixed(1)}%</td>`,
    `</tr>`,
  ].join("")).join("");
  return [
    `<div class="analytics-overview">`,
    `<div class="summary-card"><div class="k">Control Loop Tasks</div><div class="summary-value">${overview.total_tasks || 0}</div></div>`,
    `<div class="summary-card"><div class="k">Replays</div><div class="summary-value">${overview.total_replays || 0}</div></div>`,
    `<div class="summary-card"><div class="k">Replay Success Rate</div><div class="summary-value">${((overview.replay_success_rate || 0) * 100).toFixed(1)}%</div></div>`,
    `<div class="summary-card"><div class="k">Status Breakdown</div><div class="summary-value">${statusEntries || "-"}</div></div>`,
    `</div>`,
    ranking.length ? [
      `<div class="analytics-section">`,
      `<h4>Step Failure Ranking</h4>`,
      rankingTruncated ? `<div class="muted">Showing ${rankingSampled} of ${rankingTotal} step events (sampled).</div>` : "",
      `<div class="table-scroll"><table class="analytics-table"><thead><tr>`,
      `<th>Step ID</th><th>Title</th><th>Total</th><th>Pass</th><th>Fail</th><th>Fail%</th><th>Tasks</th><th>Top Failed Criteria</th>`,
      `</tr></thead><tbody>${rankingRows}</tbody></table></div>`,
      `</div>`,
    ].join("") : "",
    improvement.length ? [
      `<div class="analytics-section">`,
      `<h4>Replay Improvement</h4>`,
      improvementTruncated ? `<div class="muted">Replay task sample truncated — results may be incomplete.</div>` : "",
      `<div class="table-scroll"><table class="analytics-table"><thead><tr>`,
      `<th>Step ID</th><th>Title</th><th>Source Fail</th><th>Replay Pass</th><th>Replay Fail</th><th>Improvement%</th>`,
      `</tr></thead><tbody>${improvementRows}</tbody></table></div>`,
      `</div>`,
    ].join("") : `<div class="muted">No replay pairs found yet.</div>`,
  ].join("");
}

async function loadAnalytics() {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const data = await fetchJsonOrThrow(`${base}/tasks/analytics`);
    if (analyticsContentEl) {
      analyticsContentEl.innerHTML = renderAnalytics(data);
    }
  } catch (err) {
    if (analyticsContentEl) {
      analyticsContentEl.innerHTML = `<div class="muted">Analytics load error: ${escapeHtml(String(err))}</div>`;
    }
  }
}

function handleDashboardListSelectionClick(event) {
  const taskButton = event.target.closest("[data-task-id]");
  if (taskButton?.dataset.taskId) {
    void loadTaskDetail(taskButton.dataset.taskId);
    return;
  }
  const approvalButton = event.target.closest("[data-approval-id]");
  if (approvalButton?.dataset.approvalId) {
    void loadApprovalDetail(approvalButton.dataset.approvalId);
    return;
  }
  const auditButton = event.target.closest("[data-audit-id]");
  if (auditButton?.dataset.auditId) {
    selectAuditEntry(auditButton.dataset.auditId);
  }
}

function handleSelectionPanelClick(event) {
  const taskRef = event.target.closest("[data-task-ref]");
  if (taskRef?.dataset.taskRef) {
    void loadTaskDetail(taskRef.dataset.taskRef);
    return;
  }
  const approvalRef = event.target.closest("[data-approval-ref]");
  if (approvalRef?.dataset.approvalRef) {
    void loadApprovalDetail(approvalRef.dataset.approvalRef);
    return;
  }
  const actionButton = event.target.closest("[data-action]");
  if (!actionButton) return;
  const container = event.currentTarget;
  if (actionButton.dataset.action === "approval-resolve") {
    void resolveApprovalFromPanel(
      container,
      actionButton.dataset.requestId || "",
      actionButton.dataset.approved === "true",
    );
    return;
  }
  if (actionButton.dataset.action === "approval-resolve-bundle") {
    void resolveApprovalBundleFromPanel(
      container,
      actionButton.dataset.requestId || "",
      actionButton.dataset.strategy || "session_exact",
    );
    return;
  }
  if (actionButton.dataset.action === "subagent-steer") {
    void steerSubagentFromPanel(container, actionButton.dataset.runId || "");
    return;
  }
  if (actionButton.dataset.action === "subagent-kill") {
    void killSubagentFromPanel(actionButton.dataset.runId || "");
    return;
  }
  if (actionButton.dataset.action === "task-replay") {
    void replayTaskFromPanel(actionButton.dataset.taskId || "");
    return;
  }
  if (actionButton.dataset.action === "task-replay-from-step") {
    void replayTaskFromPanel(
      actionButton.dataset.taskId || "",
      actionButton.dataset.fromStep || "",
    );
    return;
  }
  if (actionButton.dataset.action === "task-compare") {
    void loadTaskComparison(
      actionButton.dataset.taskId || "",
      actionButton.dataset.otherTaskId || "",
    );
    return;
  }
  if (actionButton.dataset.action === "open-related-audit") {
    openAuditView({
      sessionFilter: actionButton.dataset.auditSessionId || "",
      searchQuery: actionButton.dataset.auditQuery || "",
      toolFilter: actionButton.dataset.auditTool || "",
      sourceFilter: actionButton.dataset.auditSource || "",
      resultFilter: actionButton.dataset.auditResult || "",
      focus: {
        entryId: actionButton.dataset.auditEntryId || "",
        sessionId: actionButton.dataset.auditSessionId || "",
        searchQuery: actionButton.dataset.auditQuery || "",
        requestId: actionButton.dataset.auditRequestId || "",
        taskId: actionButton.dataset.auditTaskId || "",
        runId: actionButton.dataset.auditRunId || "",
        toolName: actionButton.dataset.auditTool || "",
        source: actionButton.dataset.auditSource || "",
        result: actionButton.dataset.auditResult || "",
      },
    });
  }
}

// -----------------------------------------------------------------------
// Gateway history (source of truth)
// -----------------------------------------------------------------------

function requestGatewayHistory() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ event: "chat.history", limit: 200 }));
  logEvent("chat.history.request", { limit: 200 });
}

function handleChatHistory(payload) {
  const entries = payload.entries || [];
  messageHistory.length = 0;
  inlineApprovals.clear();
  entries.forEach((e) => {
    if (shouldSkipHistoryEntry(e)) return;
    if (e.role === "user") {
      messageHistory.push({ kind: "user", text: e.content });
    } else if (e.role === "assistant") {
      const suffix = e.aborted ? " (aborted)" : "";
      messageHistory.push({ kind: "agent", text: e.content + suffix });
    } else if (e.role === "system") {
      messageHistory.push({ kind: "system", text: e.content });
    } else if (e.role === "inject") {
      const role = e.metadata?.role || "system";
      messageHistory.push({ kind: "system", text: `[inject:${role}] ${e.content}` });
    }
  });
  restoreMessages();
  if (!entries.length) {
    logEvent("chat.history.empty", { session_id: payload.session_id });
    return;
  }
  logEvent("chat.history.loaded", { count: entries.length });
}

function shouldSkipHistoryEntry(entry) {
  if (!entry || entry.role !== "system") return false;
  const source = entry.metadata?.source || "";
  const content = String(entry.content || "");
  if (source === "tools.approval") return true;
  if (/^Approval\s+[a-z0-9]+:\s+(approved|denied)$/i.test(content)) return true;
  if (/^\[approval\]/i.test(content)) return true;
  return false;
}

// -----------------------------------------------------------------------
// Sessions
// -----------------------------------------------------------------------

function getSessionSummary(sessionId) {
  const session = sessions.find((s) => s.id === sessionId);
  return session?.preview || null;
}

function renderSessions() {
  if (!sessions.length) {
    sessionListEl.innerHTML = "<li>No sessions yet.</li>";
    return;
  }
  const isOnline = socket && socket.readyState === WebSocket.OPEN;
  sessionListEl.innerHTML = sessions.map((s) => {
    const isActive = isOnline && s.id === currentSessionId;
    const activeTag = isActive ? " <span class=\"tag\">active</span>" : "";
    const summary = getSessionSummary(s.id);
    const summaryHtml = summary ? `<div class="session-summary">${escapeHtml(summary)}</div>` : "";
    return [
      `<li class="session-item${isActive ? " session-active" : ""}" data-session-id="${escapeAttr(s.id)}">`,
      `<div class="mono">${escapeHtml(s.id)}${activeTag}</div>`,
      `<div class="muted">${escapeHtml(s.userId || "-")} / ${escapeHtml(s.when || "-")}</div>`,
      summaryHtml,
      "</li>"
    ].join("");
  }).join("");

  sessionListEl.querySelectorAll(".session-item").forEach((li) => {
    li.addEventListener("click", () => switchSession(li.dataset.sessionId));
  });
}

function addSession(sessionId, userId) {
  const existing = sessions.find((s) => s.id === sessionId);
  if (existing) {
    existing.when = new Date().toLocaleString();
    existing.userId = userId;
    existing.lastActivity = Date.now() / 1000;
    sessions.splice(sessions.indexOf(existing), 1);
    sessions.unshift(existing);
  } else {
    sessions.unshift({
      id: sessionId,
      userId,
      when: new Date().toLocaleString(),
      preview: "",
      entryCount: 0,
      lastActivity: Date.now() / 1000
    });
    if (sessions.length > 15) sessions.length = 15;
  }
  renderSessions();
}

async function syncServerSessions() {
  const settings = currentSettings();
  const base = toHttpBaseUrl(settings);
  const userId = (settings.userId || "web_user").trim();
  try {
    const res = await apiFetch(`${base}/sessions/${encodeURIComponent(userId)}`);
    if (!res.ok) return;
    const data = await res.json();
    const serverSessions = Array.isArray(data.sessions) ? data.sessions : [];
    const serverIds = new Set(serverSessions.map((s) => s.id));
    serverSessions.forEach((s) => {
      const existing = sessions.find((x) => x.id === s.id);
      const when = s.last_activity
        ? new Date(s.last_activity * 1000).toLocaleString()
        : "(server)";
      if (existing) {
        existing.userId = s.user_id || userId;
        existing.when = when;
        existing.preview = s.preview || "";
        existing.entryCount = s.entry_count || 0;
        existing.lastActivity = s.last_activity || 0;
      } else {
        sessions.push({
          id: s.id,
          userId: s.user_id || userId,
          when,
          preview: s.preview || "",
          entryCount: s.entry_count || 0,
          lastActivity: s.last_activity || 0
        });
      }
    });
    const toRemove = sessions.filter((s) => !serverIds.has(s.id));
    toRemove.forEach((s) => sessions.splice(sessions.indexOf(s), 1));
    sessions.sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0));
    renderSessions();
  } catch (_) {}
}

// -----------------------------------------------------------------------
// Dashboard
// -----------------------------------------------------------------------

async function fetchDashboardHealth(base) {
  try {
    const res = await apiFetch(`${base}/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    dashboardState.sessionBackend = data.session_backend || "memory";
    dashboardState.sessionNamespace = data.session_namespace || "";
  } catch (_) {
    dashboardState.sessionBackend = "-";
    dashboardState.sessionNamespace = "";
  }
}

async function fetchDashboardApprovals(base) {
  const pendingParams = new URLSearchParams({ state: "pending", page: "1", page_size: "4" });
  if (currentSessionId) pendingParams.set("session_id", currentSessionId);
  const filteredParams = buildDashboardApprovalParams();
  try {
    const [pendingRes, filteredRes] = await Promise.all([
      apiFetch(`${base}/tools/approvals?${pendingParams}`),
      apiFetch(`${base}/tools/approvals?${filteredParams}`)
    ]);
    if (pendingRes.ok) {
      const pendingData = await pendingRes.json();
      dashboardState.pendingApprovals = Array.isArray(pendingData.approvals) ? pendingData.approvals : [];
      dashboardState.pendingApprovalsTotal = Number(pendingData.pagination?.total || dashboardState.pendingApprovals.length || 0);
    } else {
      dashboardState.pendingApprovals = [];
      dashboardState.pendingApprovalsTotal = 0;
    }
    if (filteredRes.ok) {
      const filteredData = await filteredRes.json();
      dashboardState.dashboardApprovals = Array.isArray(filteredData.approvals) ? filteredData.approvals : [];
      dashboardState.approvalTotal = Number(filteredData.pagination?.total || 0);
      dashboardState.approvalHasMore = Boolean(filteredData.pagination?.has_more);
    } else {
      dashboardState.dashboardApprovals = [];
      dashboardState.approvalTotal = 0;
      dashboardState.approvalHasMore = false;
    }
  } catch (_) {
    dashboardState.pendingApprovals = [];
    dashboardState.pendingApprovalsTotal = 0;
    dashboardState.dashboardApprovals = [];
    dashboardState.approvalTotal = 0;
    dashboardState.approvalHasMore = false;
  }
}

async function fetchDashboardTasks(base) {
  const recentParams = new URLSearchParams({ page: "1", page_size: "5" });
  if (currentSessionId) recentParams.set("session_id", currentSessionId);
  const openParams = new URLSearchParams({ status: "open", page: "1", page_size: "1" });
  if (currentSessionId) openParams.set("session_id", currentSessionId);
  const filteredParams = buildDashboardTaskParams();
  try {
    const [recentRes, openRes, filteredRes] = await Promise.all([
      apiFetch(`${base}/tasks?${recentParams}`),
      apiFetch(`${base}/tasks?${openParams}`),
      apiFetch(`${base}/tasks?${filteredParams}`),
    ]);
    if (recentRes.ok) {
      const recentData = await recentRes.json();
      dashboardState.recentTasks = Array.isArray(recentData.tasks) ? recentData.tasks : [];
      dashboardState.recentTasksTotal = Number(recentData.pagination?.total || dashboardState.recentTasks.length || 0);
    } else {
      dashboardState.recentTasks = [];
      dashboardState.recentTasksTotal = 0;
    }
    if (openRes.ok) {
      const openData = await openRes.json();
      dashboardState.openTaskCount = Number(openData.pagination?.total || 0);
    } else {
      dashboardState.openTaskCount = 0;
    }
    if (filteredRes.ok) {
      const filteredData = await filteredRes.json();
      dashboardState.dashboardTasks = Array.isArray(filteredData.tasks) ? filteredData.tasks : [];
      dashboardState.taskTotal = Number(filteredData.pagination?.total || 0);
      dashboardState.taskHasMore = Boolean(filteredData.pagination?.has_more);
    } else {
      dashboardState.dashboardTasks = [];
      dashboardState.taskTotal = 0;
      dashboardState.taskHasMore = false;
    }
  } catch (_) {
    dashboardState.recentTasks = [];
    dashboardState.recentTasksTotal = 0;
    dashboardState.dashboardTasks = [];
    dashboardState.openTaskCount = 0;
    dashboardState.taskTotal = 0;
    dashboardState.taskHasMore = false;
  }
}

async function fetchAuditEntries(base) {
  const params = buildAuditParams();
  try {
    const response = await apiFetch(`${base}/audit?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    auditState.entries = Array.isArray(payload.entries) ? payload.entries : [];
    auditState.total = Number(payload.pagination?.total || 0);
    auditState.hasMore = Boolean(payload.pagination?.has_more);
    if (auditState.autoSelectFirst || (!auditState.selectedEntryId && auditState.focus)) {
      auditState.selectedEntry = selectPreferredAuditEntry(auditState.entries, auditState.focus);
      auditState.selectedEntryId = auditState.selectedEntry?.entry_id || null;
      auditState.autoSelectFirst = false;
    } else if (auditState.selectedEntryId) {
      auditState.selectedEntry = auditState.entries.find(
        (entry) => entry.entry_id === auditState.selectedEntryId,
      ) || auditState.selectedEntry;
    }
  } catch (_) {
    auditState.entries = [];
    auditState.total = 0;
    auditState.hasMore = false;
    if (!auditState.selectedEntryId) {
      auditState.selectedEntry = null;
    }
  }
}

async function refreshDashboard() {
  if (_dashboardRefreshPromise) {
    return _dashboardRefreshPromise;
  }
  const base = toHttpBaseUrl(currentSettings());
  _dashboardRefreshPromise = (async () => {
    await Promise.all([
      fetchDashboardHealth(base),
      fetchDashboardApprovals(base),
      fetchDashboardTasks(base)
    ]);
    updateDashboardUi();
    if (dashboardState.selectedKind && dashboardState.selectedId) {
      await refreshSelectedDetail();
    }
  })();
  try {
    await _dashboardRefreshPromise;
  } finally {
    _dashboardRefreshPromise = null;
  }
}

async function refreshAudit() {
  if (_auditRefreshPromise) {
    return _auditRefreshPromise;
  }
  const base = toHttpBaseUrl(currentSettings());
  _auditRefreshPromise = (async () => {
    await fetchAuditEntries(base);
    updateAuditUi();
  })();
  try {
    await _auditRefreshPromise;
  } finally {
    _auditRefreshPromise = null;
  }
}

function scheduleAuditRefresh(delay = 250) {
  if (_auditRefreshHandle) {
    window.clearTimeout(_auditRefreshHandle);
  }
  _auditRefreshHandle = window.setTimeout(() => {
    _auditRefreshHandle = null;
    void refreshAudit();
  }, delay);
}

// -----------------------------------------------------------------------
// WS event handlers
// -----------------------------------------------------------------------

function handleConnected(payload) {
  currentSessionId = payload.session_id || null;
  sessionBadgeEl.textContent = currentSessionId || "-";
  const pv = payload.protocol_version || "?";
  addSession(currentSessionId || "unknown", payload.user_id || currentSettings().userId);
  if (!auditState.sessionFilter) {
    auditState.sessionFilter = currentSessionId || "";
    syncAuditInputsFromState();
  }
  logEvent("protocol", { version: pv });
  // Request history from Gateway (source of truth)
  requestGatewayHistory();
  void syncServerSessions();
  scheduleDashboardRefresh(50);
  if (isTabActive("audit")) scheduleAuditRefresh(50);
}

function handleChatDone(payload) {
  clearWaiting();
  // Finalize streaming bubble if any
  if (_streamingBubble) {
    if (_streamingText) {
      messageHistory.push({ kind: "agent", text: _streamingText });
    }
    _streamingBubble = null;
    _streamingText = "";
  } else {
    const text = payload.text || (payload.aborted ? "(aborted)" : "(empty response)");
    if (!payload.aborted || text) appendBubble("agent", text);
  }
  if (payload.aborted) addSystemMessage("request aborted");
  setRunInProgress(false);
  logEvent("chat.done", { aborted: payload.aborted, len: (payload.text || "").length });
  void syncServerSessions();
}

function handleChatToken(payload) {
  clearWaiting();
  if (!_streamingBubble) {
    _streamingBubble = appendBubble("agent", "", { persist: false });
  }
  _streamingText += payload.text || "";
  _streamingBubble.textContent = _streamingText;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function handleSystemEvent(payload) {
  const msg = payload.message || "";
  logEvent("system.event", { source: payload.source, status: payload.status, run_id: payload.run_id });
  if (payload.source === "tools.approval" && payload.status === "resolved") {
    const resolved = parseApprovalResolutionMessage(msg);
    if (resolved) {
      updateInlineApprovalStatus(
        resolved.requestId,
        resolved.status,
        resolved.status === "approved" ? "Approved in chat UI" : "Denied in chat UI"
      );
      return;
    }
  }
  addSystemMessage(msg);
}

function handleHealthTick(payload) {
  if (heartbeatDotEl) {
    heartbeatDotEl.classList.add("pulse");
    setTimeout(() => heartbeatDotEl.classList.remove("pulse"), 400);
  }
  logEvent("health.tick", { active_sessions: payload.active_sessions });
}

function handleCronUpdate(payload) {
  logEvent("cron.update", payload);
  addSystemMessage(`[cron] ${payload.message || payload.status}`);
}

function handleToolsApprovalRequest(payload) {
  logEvent("tools.approval_request", payload);
  const reqId = payload.request_id || "?";
  const tool = payload.tool_name || "?";
  const agent = payload.agent_name || "?";
  const reason = payload.reason || "";
  upsertInlineApproval({
    kind: "tool",
    requestId: reqId,
    toolName: tool,
    sessionId: payload.session_id || "",
    title: `${tool} by ${agent}`,
    subtitle: "tool approval request",
    reason: reason || "approval required",
    argsPreview: JSON.stringify(payload.args || {}).slice(0, 220),
    status: "pending",
    expiresAt: payload.expires_at || null,
    note: "Respond inline to continue this run."
  });
}

function handleControlApprovalRequest(payload) {
  logEvent("control.approval_request", payload);
  const reqId = payload.request_id || "?";
  const goal = payload.goal || "?";
  const planId = payload.plan_id || "?";
  const risk = payload.risk_level || "?";
  const caps = Array.isArray(payload.required_capabilities)
    ? payload.required_capabilities.join(", ")
    : "";
  const reason = payload.reason || "";
  upsertInlineApproval({
    kind: "control",
    requestId: reqId,
    title: `control plan ${planId}`,
    subtitle: caps ? `risk=${risk} caps=${caps}` : `risk=${risk}`,
    reason: goal || reason || "control approval required",
    argsPreview: reason && goal !== reason ? reason : "",
    status: "pending",
    note: "Respond inline to continue the control loop."
  });
}

async function sendApprovalAction(requestId, approved, strategy = "single", sessionId = "") {
  if (strategy !== "single") {
    const base = toHttpBaseUrl(currentSettings());
    const note = approved
      ? `Approval bundle sent (${strategy}). Waiting for gateway confirmation...`
      : `Denial bundle sent (${strategy}). Waiting for gateway confirmation...`;
    try {
      await fetchJsonOrThrow(`${base}/tools/approvals/${encodeURIComponent(requestId)}/resolve_bundle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          approved,
          strategy,
          session_id: sessionId || currentSessionId || "",
          reason: approved ? `Approved in Web UI (${strategy})` : `Denied in Web UI (${strategy})`,
        }),
      });
      updateInlineApprovalStatus(
        requestId,
        approved ? "approving" : "denying",
        note,
      );
      scheduleDashboardRefresh(50);
    } catch (err) {
      addSystemMessage(`approval error: ${err}`);
    }
    return;
  }
  sendApproval(requestId, approved);
}

function sendApproval(requestId, approved) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const payload = {
    event: "tools.approval",
    request_id: requestId,
    approved,
    reason: approved ? "Approved in Web UI" : "Denied in Web UI"
  };
  socket.send(JSON.stringify(payload));
  logEvent("tools.approval.sent", { request_id: requestId, approved });
  updateInlineApprovalStatus(
    requestId,
    approved ? "approving" : "denying",
    approved ? "Approval sent. Waiting for gateway confirmation..." : "Denial sent. Waiting for gateway confirmation..."
  );
  scheduleDashboardRefresh(50);
}

function handleTaskUpdate(payload) {
  const task = payload.task && typeof payload.task === "object" ? payload.task : {};
  logEvent("task.update", {
    task_id: payload.task_id || task.task_id,
    status: task.status || payload.timeline_event?.status,
    event_type: payload.timeline_event?.event_type || "",
  });
  if (
    dashboardState.selectedKind === "task"
    && dashboardState.selectedId
    && String(dashboardState.selectedId) === String(payload.task_id || task.task_id || "")
  ) {
    scheduleDashboardRefresh(25);
    return;
  }
  scheduleDashboardRefresh(40);
}

function handleApprovalUpdate(payload) {
  const approval = payload.approval && typeof payload.approval === "object" ? payload.approval : {};
  const requestId = approval.request_id || payload.request_id || "";
  logEvent("tools.approval_update", {
    request_id: requestId,
    state: approval.state || "",
    approval_event: payload.approval_event || "",
  });
  if (!requestId) {
    scheduleDashboardRefresh(40);
    return;
  }
  if (approval.state === "pending") {
    upsertInlineApproval({
      kind: "tool",
      requestId,
      toolName: approval.tool_name || approval.tool_pattern || "",
      sessionId: approval.session_id || "",
      title: `${approval.tool_name || approval.tool_pattern || "approval"} by ${approval.agent_name || "agent"}`,
      subtitle: "tool approval request",
      reason: approval.reason || "approval required",
      argsPreview: JSON.stringify(approval.args || {}).slice(0, 220),
      status: "pending",
      expiresAt: approval.expires_at || null,
      note: approval.propagate_to_subagents ? "Session-scoped approval can propagate to subagents." : "",
    });
  } else if (approval.state === "expiring") {
    const escalation = Array.isArray(payload.escalation_suggestions) ? payload.escalation_suggestions : [];
    upsertInlineApproval({
      kind: "tool",
      requestId,
      toolName: approval.tool_name || approval.tool_pattern || "",
      sessionId: approval.session_id || "",
      title: `${approval.tool_name || approval.tool_pattern || "approval"} by ${approval.agent_name || "agent"}`,
      subtitle: "tool approval request",
      reason: approval.reason || "approval required",
      argsPreview: JSON.stringify(approval.args || {}).slice(0, 220),
      status: "expiring",
      expiresAt: approval.expires_at || null,
      escalationSuggestions: escalation,
      note: "Expiring soon — approve or upgrade scope to continue.",
    });
  } else {
    let note = approval.resolve_reason || "";
    if (!note && approval.state === "approved") note = "Approved";
    if (!note && approval.state === "denied") note = "Denied";
    if (!note && approval.state === "expired") note = "Expired — the control loop was aborted.";
    updateInlineApprovalStatus(requestId, approval.state || "pending", note);
  }
  scheduleDashboardRefresh(30);
}

function handleAuditAppend(payload) {
  const entry = payload.entry && typeof payload.entry === "object" ? payload.entry : null;
  if (!entry) return;
  logEvent("audit.append", {
    entry_id: entry.entry_id,
    event_type: entry.event_type,
    session_id: entry.session_id,
  });
  if (dashboardState.selectedKind === "task" || dashboardState.selectedKind === "approval") {
    scheduleDashboardRefresh(35);
  }
  if (isTabActive("audit")) {
    scheduleAuditRefresh(30);
  }
}

// -----------------------------------------------------------------------
// Skills
// -----------------------------------------------------------------------

function renderSkills(items) {
  if (!items.length) {
    skillsListEl.innerHTML = "<li>No skills loaded.</li>";
    return;
  }
  skillsListEl.innerHTML = items.map((s) => {
    const tags = Array.isArray(s.tags) && s.tags.length
      ? s.tags.map((tag) => escapeHtml(tag)).join(", ")
      : "-";
    return [
      "<li>",
      `<div><strong>${escapeHtml(s.name || "-")}</strong></div>`,
      `<div class="muted">${escapeHtml(s.description || "")}</div>`,
      `<div class="muted mono">version=${escapeHtml(s.version || "-")} author=${escapeHtml(s.author || "-")}</div>`,
      `<div class="muted mono">tags=${tags}</div>`,
      "</li>"
    ].join("");
  }).join("");
}

async function fetchSkills() {
  const base = toHttpBaseUrl(currentSettings());
  try {
    logEvent("skills.fetch.start", {});
    const res = await apiFetch(`${base}/skills`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const items = Array.isArray(data.details) ? data.details : [];
    renderSkills(items);
    if (!skillNameInputEl.value && items.length > 0 && items[0].name) {
      skillNameInputEl.value = items[0].name;
    }
    skillResultEl.textContent = JSON.stringify(data, null, 2);
    logEvent("skills.fetch.ok", { count: items.length });
  } catch (err) {
    renderSkills([]);
    skillResultEl.textContent = String(err);
    logEvent("skills.fetch.error", { error: String(err) });
  }
}

async function executeSkill() {
  const base = toHttpBaseUrl(currentSettings());
  const skillName = (skillNameInputEl.value || "").trim();
  if (!skillName) { skillResultEl.textContent = "skill name is required"; return; }

  let params = {};
  const rawParams = (skillParamsInputEl.value || "").trim();
  if (rawParams) {
    try {
      params = JSON.parse(rawParams);
      if (!params || typeof params !== "object" || Array.isArray(params)) {
        skillResultEl.textContent = "params must be a JSON object"; return;
      }
    } catch (err) {
      skillResultEl.textContent = `invalid JSON: ${err}`; return;
    }
  }

  try {
    logEvent("skills.exec.start", { skillName });
    const res = await apiFetch(`${base}/skills/${encodeURIComponent(skillName)}/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
    skillResultEl.textContent = JSON.stringify(data, null, 2);
    logEvent("skills.exec.ok", { skillName });
  } catch (err) {
    skillResultEl.textContent = String(err);
    logEvent("skills.exec.error", { skillName, error: String(err) });
  }
}

// -----------------------------------------------------------------------
// Memory
// -----------------------------------------------------------------------

async function fetchMemory() {
  const base = toHttpBaseUrl(currentSettings());
  const query = (memoryQueryInputEl.value || "").trim();
  const tags = (memoryTagsInputEl.value || "").trim();

  try {
    const res = await apiFetch(`${base}/memory/stats`);
    if (res.ok) {
      const data = await res.json();
      const s = data.stats || {};
      memoryStatsEl.textContent = `${s.total_memories ?? "-"}\u4ef6 / embeddings: ${s.with_embedding ?? "-"}\u4ef6`;
    }
  } catch (_) { memoryStatsEl.textContent = "(stats unavailable)"; }

  const params = new URLSearchParams({ limit: "50" });
  if (query) params.set("query", query);
  if (tags) params.set("tags", tags);

  try {
    logEvent("memory.fetch.start", { query, tags });
    const res = await apiFetch(`${base}/memory?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderMemory(data.results || []);
    logEvent("memory.fetch.ok", { count: data.count });
  } catch (err) {
    memoryListEl.innerHTML = `<li class="muted">${err}</li>`;
    logEvent("memory.fetch.error", { error: String(err) });
  }
}

function renderMemory(items) {
  if (!items.length) {
    memoryListEl.innerHTML = "<li class='muted'>No memories found.</li>";
    return;
  }
  memoryListEl.innerHTML = items.map((m) => {
    const tags = Array.isArray(m.tags) && m.tags.length
      ? m.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" ")
      : "";
    const date = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : "-";
    const score = m.score != null ? ` <span class="muted mono">score=${m.score.toFixed(3)}</span>` : "";
    return [
      `<li data-memory-id="${escapeAttr(String(m.id))}">`,
      `<div class="memory-meta"><span class="mono">#${escapeHtml(String(m.id))}</span> ${escapeHtml(date)} ${tags}${score}</div>`,
      `<div class="memory-content">${escapeHtml(m.content)}</div>`,
      `<div class="memory-actions"><button class="btn btn-sm delete-memory-btn" data-id="${escapeAttr(String(m.id))}">Delete</button></div>`,
      "</li>"
    ].join("");
  }).join("");

  memoryListEl.querySelectorAll(".delete-memory-btn").forEach((btn) => {
    btn.addEventListener("click", () => void deleteMemory(Number(btn.dataset.id)));
  });
}

async function deleteMemory(id) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    logEvent("memory.delete.start", { id });
    const res = await apiFetch(`${base}/memory/${id}`, { method: "DELETE" });
    if (res.status === 404) throw new Error("not found");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("memory.delete.ok", { id });
    void fetchMemory();
  } catch (err) {
    logEvent("memory.delete.error", { id, error: String(err) });
  }
}

// -----------------------------------------------------------------------
// Cron (platform)
// -----------------------------------------------------------------------

function renderCronJobs(jobs) {
  if (!jobs.length) {
    cronListEl.innerHTML = "<li class='muted'>No cron jobs yet.</li>";
    return;
  }
  cronListEl.innerHTML = jobs.map((j) => {
    const statusTag = j.enabled
      ? `<span class="tag">enabled</span>`
      : `<span class="tag" style="opacity:.5">disabled</span>`;
    const lastRun = j.last_run ? new Date(j.last_run * 1000).toLocaleString() : "-";
    const nextRun = j.next_run ? new Date(j.next_run * 1000).toLocaleString() : "-";
    const deliveryTag = j.delivery_target && j.delivery_target !== "isolated"
      ? ` <span class="tag">${escapeHtml(j.delivery_target)}</span>` : "";
    const retryInfo = j.max_retries > 0
      ? ` retries: ${j.retry_count || 0}/${j.max_retries}` : "";
    const sysEvent = j.system_event
      ? ` <span class="tag">on:${escapeHtml(j.system_event)}</span>` : "";
    return [
      `<li class="cron-item" data-job-id="${escapeAttr(j.id)}">`,
      `<div><strong>${escapeHtml(j.name)}</strong> ${statusTag}${deliveryTag}${sysEvent}</div>`,
      `<div class="muted mono">${escapeHtml(j.cron_expr)} | agent: ${escapeHtml(j.agent_id)}${retryInfo}</div>`,
      `<div class="muted">${escapeHtml(j.task)}</div>`,
      `<div class="muted mono">last: ${escapeHtml(lastRun)} | next: ${escapeHtml(nextRun)} | runs: ${j.run_count}</div>`,
      j.last_error ? `<div class="muted mono" style="color:#f87171">error: ${escapeHtml(j.last_error)}</div>` : "",
      `<div class="memory-actions">`,
      `<button class="btn btn-sm toggle-cron-btn" data-id="${escapeAttr(j.id)}" data-enabled="${j.enabled}">${j.enabled ? "Disable" : "Enable"}</button>`,
      `<button class="btn btn-sm delete-cron-btn" data-id="${escapeAttr(j.id)}">Delete</button>`,
      `</div>`,
      "</li>"
    ].join("");
  }).join("");

  cronListEl.querySelectorAll(".delete-cron-btn").forEach((btn) => {
    btn.addEventListener("click", () => void deleteCronJob(btn.dataset.id));
  });
  cronListEl.querySelectorAll(".toggle-cron-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const enabled = btn.dataset.enabled === "true";
      void toggleCronJob(btn.dataset.id, !enabled);
    });
  });
}

async function fetchCron() {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const res = await apiFetch(`${base}/cron`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderCronJobs(data.jobs || []);
    cronResultEl.textContent = "";
  } catch (err) {
    cronListEl.innerHTML = `<li class="muted">${escapeHtml(String(err))}</li>`;
    cronResultEl.textContent = String(err);
  }
}

async function addCronJob() {
  const base = toHttpBaseUrl(currentSettings());
  const name = (cronNameEl.value || "").trim();
  const cron_expr = (cronExprEl.value || "").trim();
  const task = (cronTaskEl.value || "").trim();
  const agent_id = (cronAgentEl.value || "web_researcher").trim();
  const delivery_target = cronDeliveryEl ? (cronDeliveryEl.value || "isolated").trim() : "isolated";
  const max_retries = cronRetriesEl ? parseInt(cronRetriesEl.value || "0", 10) : 0;
  const system_event = cronSysEventEl ? (cronSysEventEl.value || "").trim() || null : null;

  if (!name || !task) {
    cronResultEl.textContent = "name and task are required";
    return;
  }
  if (!system_event && !cron_expr) {
    cronResultEl.textContent = "cron_expr is required (unless system_event is set)";
    return;
  }

  try {
    logEvent("cron.add.start", { name, cron_expr, system_event });
    const resolvedDelivery =
      delivery_target === "main" && currentSessionId
        ? `session:${currentSessionId}`
        : delivery_target;
    const body = {
      name,
      cron_expr,
      task,
      agent_id,
      delivery_target: resolvedDelivery,
      max_retries,
      session_id: currentSessionId || ""
    };
    if (system_event) body.system_event = system_event;
    const res = await apiFetch(`${base}/cron`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
    cronResultEl.textContent = JSON.stringify(data.job, null, 2);
    cronNameEl.value = "";
    cronTaskEl.value = "";
    logEvent("cron.add.ok", { id: data.job?.id });
    void fetchCron();
  } catch (err) {
    cronResultEl.textContent = String(err);
    logEvent("cron.add.error", { error: String(err) });
  }
}

async function deleteCronJob(id) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const res = await apiFetch(`${base}/cron/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("cron.delete.ok", { id });
    void fetchCron();
  } catch (err) {
    cronResultEl.textContent = String(err);
    logEvent("cron.delete.error", { id, error: String(err) });
  }
}

async function toggleCronJob(id, enabled) {
  const base = toHttpBaseUrl(currentSettings());
  try {
    const res = await apiFetch(`${base}/cron/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("cron.toggle.ok", { id, enabled });
    void fetchCron();
  } catch (err) {
    cronResultEl.textContent = String(err);
    logEvent("cron.toggle.error", { id, error: String(err) });
  }
}

// -----------------------------------------------------------------------
// Tab management
// -----------------------------------------------------------------------

function activateTab(tabKey) {
  navButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabKey));
  tabs.forEach((tab) => tab.classList.toggle("active", tab.id === `tab-${tabKey}`));
  const meta = NAV_META[tabKey] || NAV_META.chat;
  tabTitle.textContent = meta.title;
  tabSubtitle.textContent = meta.subtitle;
  if (tabKey === "chat") restoreMessages();
  if (tabKey === "dashboard") scheduleDashboardRefresh(0);
  if (tabKey === "audit") scheduleAuditRefresh(0);
  if (tabKey === "sessions") void syncServerSessions();
  if (tabKey === "skills") void fetchSkills();
  if (tabKey === "memory") void fetchMemory();
  if (tabKey === "cron") void fetchCron();
}

// -----------------------------------------------------------------------
// WebSocket
// -----------------------------------------------------------------------

function switchSession(targetSessionId) {
  if (socket) socket.close();
  resetDashboardPages();
  messageHistory.length = 0;
  inlineApprovals.clear();
  restoreMessages();
  connect(targetSessionId);
}

// Refresh countdown labels every 5 seconds (client-side only, no WS traffic).
(function startCountdownTicker() {
  setInterval(() => {
    document.querySelectorAll(".approval-countdown[data-expires-at]").forEach((el) => {
      const expiresAt = parseFloat(el.dataset.expiresAt);
      if (!expiresAt) return;
      const remaining = Math.max(0, Math.round(expiresAt - Date.now() / 1000));
      if (remaining <= 0) {
        el.textContent = "expired";
        el.classList.add("expired");
        return;
      }
      const mins = Math.floor(remaining / 60);
      const secs = remaining % 60;
      el.textContent = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
      el.classList.toggle("urgent", remaining <= 30);
      el.classList.toggle("warning", remaining > 30 && remaining <= 60);
    });
  }, 5000);
})();

function connect(targetSessionId = null) {
  if (typeof targetSessionId !== "string") targetSessionId = null;
  if (socket && socket.readyState === WebSocket.OPEN) {
    addSystemMessage("already connected");
    return;
  }

  const settings = currentSettings();
  const wsUrl = toWebSocketUrl(settings, targetSessionId);
  logEvent("socket.connecting", { wsUrl });
  setStatus(false, "connecting...");

  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    setStatus(true, "online");
    addSystemMessage(`connected: ${wsUrl}`);
    logEvent("socket.open");
    if (pendingMessage) {
      const toSend = pendingMessage;
      pendingMessage = null;
      sendMessage(toSend);
    }
  };

  socket.onclose = (event) => {
    setStatus(false, "offline");
    currentSessionId = null;
    sessionBadgeEl.textContent = "-";
    clearWaiting();
    setRunInProgress(false);
    _streamingBubble = null;
    _streamingText = "";
    addSystemMessage(`disconnected (code=${event.code})`);
    logEvent("socket.close", { code: event.code, reason: event.reason || "" });
    scheduleDashboardRefresh(50);
    if (isTabActive("audit")) scheduleAuditRefresh(50);
  };

  socket.onerror = () => {
    setStatus(false, "error");
    addSystemMessage("connection error");
    logEvent("socket.error");
  };

  socket.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      logEvent(`ws.${payload.event || payload.type || "message"}`, payload);

      const evName = payload.event || payload.type || "";

      // --- typed protocol v1 ---
      if (evName === "connected") { handleConnected(payload); return; }
      if (evName === "chat.done") { handleChatDone(payload); return; }
      if (evName === "chat.token") { handleChatToken(payload); return; }
      if (evName === "chat.history") { handleChatHistory(payload); return; }
      if (evName === "tool.start") { return; }
      if (evName === "tool.result") { return; }
      if (evName === "task.update") { handleTaskUpdate(payload); return; }
      if (evName === "tools.approval_update") { handleApprovalUpdate(payload); return; }
      if (evName === "audit.append") { handleAuditAppend(payload); return; }
      if (evName === "system.event") { handleSystemEvent(payload); return; }
      if (evName === "health.tick") { handleHealthTick(payload); return; }
      if (evName === "cron.update") { handleCronUpdate(payload); return; }
      if (evName === "tools.approval_request") { handleToolsApprovalRequest(payload); return; }
      if (evName === "control.approval_request") { handleControlApprovalRequest(payload); return; }

      // --- backward compat ---
      if (evName === "agent_message") {
        clearWaiting();
        appendBubble("agent", payload.message || "");
        setRunInProgress(false);
        return;
      }
      if (evName === "error") {
        clearWaiting();
        addSystemMessage(payload.message || "error");
        setRunInProgress(false);
        return;
      }
      if (evName === "user_message") return;
      if (evName === "pong") return;

      addSystemMessage(event.data);
    } catch (_) {
      logEvent("socket.message.raw", { data: event.data });
      addSystemMessage(event.data);
    }
  };
}

function disconnect() {
  if (socket) socket.close();
}

function sendMessage(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    addSystemMessage(`not connected -> connecting`);
    pendingMessage = text;
    connect();
    return;
  }
  let payload = { event: "chat.send", text };
  const controlGoal = text.startsWith("/control ")
    ? text.slice("/control ".length).trim()
    : (text.startsWith("/plan ") ? text.slice("/plan ".length).trim() : "");
  if (controlGoal) {
    payload = { event: "control.run", goal: controlGoal };
  }
  socket.send(JSON.stringify(payload));
  logEvent("socket.send", payload);
  appendBubble("user", text);
  const currentSession = sessions.find((s) => s.id === currentSessionId);
  if (currentSession && !currentSession.preview) {
    currentSession.preview = text.length > 96 ? `${text.slice(0, 95)}…` : text;
    renderSessions();
  }
  waitingIndicator = appendBubble("system", "thinking...", { persist: false });
  setRunInProgress(true);
}

function abortRun() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;

  const pendingApprovalIds = getPendingInlineApprovalIds();
  if (pendingApprovalIds.length) {
    pendingApprovalIds.forEach((requestId) => {
      sendApproval(requestId, false);
      updateInlineApprovalStatus(
        requestId,
        "denying",
        "Stop requested from Web UI."
      );
    });
    addSystemMessage("stop sent for pending approvals...");
  }

  if (_runInProgress) {
    socket.send(JSON.stringify({ event: "chat.abort" }));
    logEvent("socket.abort");
    addSystemMessage("abort sent...");
  }
}

// -----------------------------------------------------------------------
// Event listeners
// -----------------------------------------------------------------------

navButtons.forEach((btn) => btn.addEventListener("click", () => activateTab(btn.dataset.tab)));
connectBtn.addEventListener("click", () => connect());
disconnectBtn.addEventListener("click", disconnect);
abortBtn.addEventListener("click", abortRun);
saveSettingsBtn.addEventListener("click", persistSettings);
resetSettingsBtn.addEventListener("click", resetSettings);
refreshDashboardBtn.addEventListener("click", () => scheduleDashboardRefresh(0));
refreshAnalyticsBtn?.addEventListener("click", () => void loadAnalytics());
clearDashboardFiltersBtn?.addEventListener("click", () => {
  dashboardState.searchQuery = "";
  dashboardState.taskStatusFilter = "all";
  dashboardState.approvalStateFilter = "all";
  resetDashboardPages();
  if (dashboardSearchInputEl) dashboardSearchInputEl.value = "";
  updateDashboardFilterButtons();
  scheduleDashboardRefresh(0);
});
refreshAuditBtn?.addEventListener("click", () => scheduleAuditRefresh(0));
clearAuditFiltersBtn?.addEventListener("click", () => {
  resetAuditFilters();
  scheduleAuditRefresh(0);
});
dashboardSearchInputEl?.addEventListener("input", () => {
  dashboardState.searchQuery = dashboardSearchInputEl.value || "";
  resetDashboardPages();
  scheduleDashboardRefresh(250);
});
auditSearchInputEl?.addEventListener("input", () => {
  auditState.searchQuery = auditSearchInputEl.value || "";
  auditState.page = 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  auditState.focus = null;
  scheduleAuditRefresh(250);
});
[auditActorInputEl, auditSessionInputEl, auditToolInputEl, auditSourceInputEl, auditResultInputEl].forEach((element) => {
  element?.addEventListener("input", () => {
    auditState.actorFilter = auditActorInputEl?.value || "";
    auditState.sessionFilter = auditSessionInputEl?.value || "";
    auditState.toolFilter = auditToolInputEl?.value || "";
    auditState.sourceFilter = auditSourceInputEl?.value || "";
    auditState.resultFilter = auditResultInputEl?.value || "";
    auditState.page = 1;
    auditState.selectedEntryId = null;
    auditState.selectedEntry = null;
    auditState.focus = null;
    scheduleAuditRefresh(250);
  });
});
dashboardFilterChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    const kind = chip.dataset.filterKind;
    const value = chip.dataset.filterValue || "all";
    if (kind === "task-status") {
      dashboardState.taskStatusFilter = value;
      dashboardState.taskPage = 1;
    } else if (kind === "approval-state") {
      dashboardState.approvalStateFilter = value;
      dashboardState.approvalPage = 1;
    }
    updateDashboardFilterButtons();
    scheduleDashboardRefresh(0);
  });
});
dashboardApprovalsPrevBtn?.addEventListener("click", () => {
  if (dashboardState.approvalPage <= 1) return;
  dashboardState.approvalPage -= 1;
  scheduleDashboardRefresh(0);
});
dashboardApprovalsNextBtn?.addEventListener("click", () => {
  if (!dashboardState.approvalHasMore) return;
  dashboardState.approvalPage += 1;
  scheduleDashboardRefresh(0);
});
dashboardTasksPrevBtn?.addEventListener("click", () => {
  if (dashboardState.taskPage <= 1) return;
  dashboardState.taskPage -= 1;
  scheduleDashboardRefresh(0);
});
dashboardTasksNextBtn?.addEventListener("click", () => {
  if (!dashboardState.taskHasMore) return;
  dashboardState.taskPage += 1;
  scheduleDashboardRefresh(0);
});
auditPrevBtn?.addEventListener("click", () => {
  if (auditState.page <= 1) return;
  auditState.page -= 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  scheduleAuditRefresh(0);
});
auditNextBtn?.addEventListener("click", () => {
  if (!auditState.hasMore) return;
  auditState.page += 1;
  auditState.selectedEntryId = null;
  auditState.selectedEntry = null;
  scheduleAuditRefresh(0);
});
[
  dashboardApprovalsListEl,
  dashboardTasksListEl,
  inspectorApprovalsListEl,
  inspectorTasksListEl,
  auditListEl,
].forEach((element) => {
  element?.addEventListener("click", handleDashboardListSelectionClick);
});
[
  dashboardDetailPanelEl,
  inspectorSelectionDetailEl,
].forEach((element) => {
  element?.addEventListener("click", handleSelectionPanelClick);
});
refreshSkillsBtn.addEventListener("click", () => void fetchSkills());
runSkillBtn.addEventListener("click", () => void executeSkill());
refreshMemoryBtn.addEventListener("click", () => void fetchMemory());
searchMemoryBtn.addEventListener("click", () => void fetchMemory());
memoryQueryInputEl.addEventListener("keydown", (e) => { if (e.key === "Enter") void fetchMemory(); });
refreshCronBtn.addEventListener("click", () => void fetchCron());
addCronBtn.addEventListener("click", () => void addCronJob());
gatewayUrlEl.addEventListener("input", () => {
  gatewayHostLabelEl.textContent = gatewayUrlEl.value.trim() || DEFAULTS.gatewayUrl;
});
chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = messageInputEl.value.trim();
  if (!text) return;
  sendMessage(text);
  messageInputEl.value = "";
});
messageInputEl.addEventListener("compositionstart", () => {
  _messageInputComposing = true;
});
messageInputEl.addEventListener("compositionend", () => {
  _messageInputComposing = false;
});
messageInputEl.addEventListener("keydown", (e) => {
  if (e.isComposing || _messageInputComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatForm.requestSubmit(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || e.repeat) return;
  if (!_runInProgress && getPendingInlineApprovalIds().length === 0) return;
  e.preventDefault();
  abortRun();
});

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

const initialSettings = { ...parseStoredSettings(), ...parseUrlSettings() };
applySettings(initialSettings);
resetAuditFilters();
renderSessions();
updateDashboardFilterButtons();
updateDashboardUi();
updateAuditUi();
activateTab("chat");
setStatus(false, "offline");
setRunInProgress(false);
scheduleDashboardRefresh(0);
addSystemMessage("ready: Configure settings then press Connect");
logEvent("ui.ready", currentSettings());
