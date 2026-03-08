const NAV_META = {
  chat: { title: "Chat", subtitle: "Gateway WebSocket chat" },
  sessions: { title: "Sessions", subtitle: "Current browser sessions" },
  channels: { title: "Channels", subtitle: "Channel status overview" },
  skills: { title: "Skills", subtitle: "OpenClaw-style skill catalog and run" },
  memory: { title: "Memory", subtitle: "SQLite vector memory browser" },
  cron: { title: "Cron Jobs", subtitle: "Scheduled tasks" },
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
const rawLogEl = document.getElementById("rawLog");
const sessionListEl = document.getElementById("sessionList");
const statusDotEl = document.getElementById("statusDot");
const statusTextEl = document.getElementById("statusText");
const sessionBadgeEl = document.getElementById("sessionBadge");
const gatewayHostLabelEl = document.getElementById("gatewayHostLabel");
const heartbeatDotEl = document.getElementById("heartbeatDot");

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
const addCronBtn = document.getElementById("addCronBtn");
const refreshCronBtn = document.getElementById("refreshCronBtn");
const cronResultEl = document.getElementById("cronResult");

let socket = null;
let waitingIndicator = null;
const sessions = [];
let pendingMessage = null;
const messageHistory = [];
let currentSessionId = null;
const SESSION_HISTORY_KEY = "boiled_claw_msg_history_v1";

// --- streaming state ---
let _streamingBubble = null;
let _streamingText = "";
let _runInProgress = false;

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

function saveSessionHistory() {
  if (!currentSessionId) return;
  try {
    const stored = JSON.parse(localStorage.getItem(SESSION_HISTORY_KEY) || "{}");
    stored[currentSessionId] = messageHistory;
    localStorage.setItem(SESSION_HISTORY_KEY, JSON.stringify(stored));
  } catch (_) {}
}

function loadSessionHistory(sessionId) {
  try {
    const stored = JSON.parse(localStorage.getItem(SESSION_HISTORY_KEY) || "{}");
    return stored[sessionId] || [];
  } catch (_) { return []; }
}

function appendBubble(kind, text, { persist = true } = {}) {
  if (persist) {
    messageHistory.push({ kind, text });
    saveSessionHistory();
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

function restoreMessages() {
  messagesEl.innerHTML = "";
  messageHistory.forEach(({ kind, text }) => {
    const b = document.createElement("div");
    b.className = `bubble ${kind}`;
    b.textContent = text;
    messagesEl.appendChild(b);
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

// -----------------------------------------------------------------------
// Sessions
// -----------------------------------------------------------------------

function getSessionSummary(sessionId) {
  const history = loadSessionHistory(sessionId);
  const firstUser = history.find((m) => m.kind === "user");
  if (!firstUser) return null;
  const text = firstUser.text.trim().replace(/\n+/g, " ");
  return text.length > 60 ? text.slice(0, 60) + "…" : text;
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
    sessions.splice(sessions.indexOf(existing), 1);
    sessions.unshift(existing);
  } else {
    sessions.unshift({ id: sessionId, userId, when: new Date().toLocaleString() });
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
    const serverIds = new Set((data.sessions || []).map((s) => s.id));
    (data.sessions || []).forEach((s) => {
      if (!sessions.find((x) => x.id === s.id)) {
        sessions.push({ id: s.id, userId, when: "(server)" });
      }
    });
    const toRemove = sessions.filter((s) => !serverIds.has(s.id));
    toRemove.forEach((s) => sessions.splice(sessions.indexOf(s), 1));
    renderSessions();
  } catch (_) {}
}

// -----------------------------------------------------------------------
// WS event handlers
// -----------------------------------------------------------------------

function handleConnected(payload) {
  currentSessionId = payload.session_id || null;
  sessionBadgeEl.textContent = currentSessionId || "-";
  addSession(currentSessionId || "unknown", payload.user_id || currentSettings().userId);
}

function handleChatDone(payload) {
  clearWaiting();
  // ストリーミングバブルが残っていれば確定
  if (_streamingBubble) {
    if (_streamingText) {
      messageHistory.push({ kind: "agent", text: _streamingText });
      saveSessionHistory();
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
  addSystemMessage(msg);
  logEvent("system.event", { source: payload.source, status: payload.status, run_id: payload.run_id });
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
      memoryStatsEl.textContent = `${s.total_memories ?? "-"}件 / embeddings: ${s.with_embedding ?? "-"}件`;
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
// Cron
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
    return [
      `<li class="cron-item" data-job-id="${escapeAttr(j.id)}">`,
      `<div><strong>${escapeHtml(j.name)}</strong> ${statusTag}</div>`,
      `<div class="muted mono">${escapeHtml(j.cron_expr)} | agent: ${escapeHtml(j.agent_id)}</div>`,
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

  if (!name || !cron_expr || !task) {
    cronResultEl.textContent = "name, cron_expr, task are required";
    return;
  }

  try {
    logEvent("cron.add.start", { name, cron_expr });
    const res = await apiFetch(`${base}/cron`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, cron_expr, task, agent_id })
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
  messageHistory.length = 0;
  const saved = loadSessionHistory(targetSessionId);
  saved.forEach((m) => messageHistory.push(m));
  restoreMessages();
  connect(targetSessionId);
}

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

      // --- typed new protocol ---
      if (evName === "connected") { handleConnected(payload); return; }
      if (evName === "chat.done") { handleChatDone(payload); return; }
      if (evName === "chat.token") { handleChatToken(payload); return; }
      if (evName === "system.event") { handleSystemEvent(payload); return; }
      if (evName === "health.tick") { handleHealthTick(payload); return; }
      if (evName === "cron.update") { handleCronUpdate(payload); return; }

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
  // 新プロトコル
  const payload = { event: "chat.send", text };
  socket.send(JSON.stringify(payload));
  logEvent("socket.send", payload);
  appendBubble("user", text);
  waitingIndicator = appendBubble("system", "thinking...", { persist: false });
  setRunInProgress(true);
}

function abortRun() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ event: "chat.abort" }));
  logEvent("socket.abort");
  addSystemMessage("abort sent...");
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
messageInputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatForm.requestSubmit(); }
});

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

const initialSettings = { ...parseStoredSettings(), ...parseUrlSettings() };
applySettings(initialSettings);
renderSessions();
activateTab("chat");
setStatus(false, "offline");
setRunInProgress(false);
addSystemMessage("ready: Configure settings then press Connect");
logEvent("ui.ready", currentSettings());
