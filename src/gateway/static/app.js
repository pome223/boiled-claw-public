const NAV_META = {
  chat: { title: "Chat", subtitle: "Gateway WebSocket chat" },
  sessions: { title: "Sessions", subtitle: "Current browser sessions" },
  channels: { title: "Channels", subtitle: "Channel status overview" },
  skills: { title: "Skills", subtitle: "OpenClaw-style skill catalog and run" },
  memory: { title: "Memory", subtitle: "SQLite vector memory browser" },
  cron: { title: "Cron Jobs", subtitle: "Scheduled tasks overview" },
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

const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
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

let socket = null;
let waitingIndicator = null;
const sessions = [];
let pendingMessage = null;
const messageHistory = [];
let currentSessionId = null;
const SESSION_HISTORY_KEY = "boiled_claw_msg_history_v1";

function parseStoredSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch (_err) {
    return {};
  }
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

  // Accept multiple input styles:
  // - ws://host
  // - ws://host/chat
  // - ws://host/ws
  // - ws://host/ws/{user_id}
  if (parsed.pathname === "/chat" || parsed.pathname === "/chat/") {
    parsed.pathname = userPath;
  } else if (parsed.pathname === "/ws" || parsed.pathname === "/ws/") {
    parsed.pathname = userPath;
  } else if (/^\/ws\/[^/]+\/?$/.test(parsed.pathname)) {
    parsed.pathname = userPath;
  } else if (parsed.pathname === "/" || parsed.pathname === "") {
    parsed.pathname = userPath;
  } else if (!parsed.pathname.startsWith("/ws/")) {
    parsed.pathname = userPath;
  }

  const wsUrl = new URL(parsed.toString());
  if (settings.token) {
    wsUrl.searchParams.set("token", settings.token);
  }
  if (sessionId) {
    wsUrl.searchParams.set("session_id", sessionId);
  }
  return wsUrl.toString();
}

function toHttpBaseUrl(settings) {
  let base = settings.gatewayUrl || DEFAULTS.gatewayUrl;
  if (base.startsWith("ws://")) base = "http://" + base.slice(5);
  if (base.startsWith("wss://")) base = "https://" + base.slice(6);
  if (!base.startsWith("http://") && !base.startsWith("https://")) {
    base = `${window.location.protocol}//${base}`;
  }
  return base.replace(/\/+$/, "");
}

function setStatus(online, text) {
  statusDotEl.classList.toggle("online", online);
  statusDotEl.classList.toggle("offline", !online);
  statusTextEl.textContent = text;
  connectBtn.disabled = online;
  disconnectBtn.disabled = !online;
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
  } catch (_) {
    return [];
  }
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
  sessionListEl.innerHTML = sessions
    .map((s) => {
      const isActive = isOnline && s.id === currentSessionId;
      const activeTag = isActive ? " <span class=\"tag\">active</span>" : "";
      const summary = getSessionSummary(s.id);
      const summaryHtml = summary
        ? `<div class="session-summary">${escapeHtml(summary)}</div>`
        : "";
      return [
        `<li class="session-item${isActive ? " session-active" : ""}" data-session-id="${s.id}">`,
        `<div class="mono">${s.id}${activeTag}</div>`,
        `<div class="muted">${s.userId} / ${s.when}</div>`,
        summaryHtml,
        "</li>"
      ].join("");
    })
    .join("");

  sessionListEl.querySelectorAll(".session-item").forEach((li) => {
    li.addEventListener("click", () => switchSession(li.dataset.sessionId, li.querySelector(".muted").textContent));
  });
}

function addSession(sessionId, userId) {
  const existing = sessions.find((s) => s.id === sessionId);
  if (existing) {
    existing.when = new Date().toLocaleString();
    existing.userId = userId;
    // 先頭に移動
    sessions.splice(sessions.indexOf(existing), 1);
    sessions.unshift(existing);
  } else {
    sessions.unshift({ id: sessionId, userId, when: new Date().toLocaleString() });
    if (sessions.length > 15) sessions.length = 15;
  }
  renderSessions();
}

function activateTab(tabKey) {
  navButtons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabKey);
  });
  tabs.forEach((tab) => {
    tab.classList.toggle("active", tab.id === `tab-${tabKey}`);
  });
  const meta = NAV_META[tabKey] || NAV_META.chat;
  tabTitle.textContent = meta.title;
  tabSubtitle.textContent = meta.subtitle;
  if (tabKey === "chat") {
    restoreMessages();
  }
  if (tabKey === "sessions") {
    void syncServerSessions();
  }
  if (tabKey === "skills") {
    void fetchSkills();
  }
  if (tabKey === "memory") {
    void fetchMemory();
  }
}

function renderSkills(items) {
  if (!items.length) {
    skillsListEl.innerHTML = "<li>No skills loaded.</li>";
    return;
  }
  skillsListEl.innerHTML = items
    .map((s) => {
      const tags = Array.isArray(s.tags) && s.tags.length ? s.tags.join(", ") : "-";
      return [
        "<li>",
        `<div><strong>${s.name || "-"}</strong></div>`,
        `<div class="muted">${s.description || ""}</div>`,
        `<div class="muted mono">version=${s.version || "-"} author=${s.author || "-"}</div>`,
        `<div class="muted mono">tags=${tags}</div>`,
        "</li>"
      ].join("");
    })
    .join("");
}

async function fetchSkills() {
  const base = toHttpBaseUrl(currentSettings());
  const url = `${base}/skills`;
  try {
    logEvent("skills.fetch.start", { url });
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
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
  if (!skillName) {
    skillResultEl.textContent = "skill name is required";
    return;
  }

  let params = {};
  const rawParams = (skillParamsInputEl.value || "").trim();
  if (rawParams) {
    try {
      params = JSON.parse(rawParams);
      if (!params || typeof params !== "object" || Array.isArray(params)) {
        skillResultEl.textContent = "params must be a JSON object";
        return;
      }
    } catch (err) {
      skillResultEl.textContent = `invalid JSON: ${err}`;
      return;
    }
  }

  const url = `${base}/skills/${encodeURIComponent(skillName)}/execute`;
  try {
    logEvent("skills.exec.start", { skillName });
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data?.detail || `HTTP ${res.status}`);
    }
    skillResultEl.textContent = JSON.stringify(data, null, 2);
    logEvent("skills.exec.ok", { skillName });
  } catch (err) {
    skillResultEl.textContent = String(err);
    logEvent("skills.exec.error", { skillName, error: String(err) });
  }
}

async function syncServerSessions() {
  const settings = currentSettings();
  const base = toHttpBaseUrl(settings);
  const userId = (settings.userId || "web_user").trim();
  try {
    const res = await fetch(`${base}/sessions/${encodeURIComponent(userId)}`);
    if (!res.ok) return;
    const data = await res.json();
    const serverIds = new Set((data.sessions || []).map((s) => s.id));
    // サーバーに存在するセッションのみ残し、ローカルにないものを追加
    (data.sessions || []).forEach((s) => {
      if (!sessions.find((x) => x.id === s.id)) {
        sessions.push({ id: s.id, userId, when: "(server)" });
      }
    });
    // サーバーに存在しないローカルエントリを削除
    const toRemove = sessions.filter((s) => !serverIds.has(s.id));
    toRemove.forEach((s) => sessions.splice(sessions.indexOf(s), 1));
    renderSessions();
  } catch (_) {}
}

async function fetchMemory() {
  const base = toHttpBaseUrl(currentSettings());
  const query = (memoryQueryInputEl.value || "").trim();
  const tags = (memoryTagsInputEl.value || "").trim();

  // stats
  try {
    const res = await fetch(`${base}/memory/stats`);
    if (res.ok) {
      const data = await res.json();
      const s = data.stats || {};
      memoryStatsEl.textContent = `${s.total_memories ?? "-"}件 / embeddings: ${s.with_embedding ?? "-"}件`;
    }
  } catch (_) {
    memoryStatsEl.textContent = "(stats unavailable)";
  }

  // search / list
  const params = new URLSearchParams({ limit: "50" });
  if (query) params.set("query", query);
  if (tags) params.set("tags", tags);

  try {
    logEvent("memory.fetch.start", { query, tags });
    const res = await fetch(`${base}/memory?${params}`);
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
    const tags = Array.isArray(m.tags) && m.tags.length ? m.tags.map((t) => `<span class="tag">${t}</span>`).join(" ") : "";
    const date = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : "-";
    const score = m.score != null ? ` <span class="muted mono">score=${m.score.toFixed(3)}</span>` : "";
    return [
      `<li data-memory-id="${m.id}">`,
      `<div class="memory-meta"><span class="mono">#${m.id}</span> ${date} ${tags}${score}</div>`,
      `<div class="memory-content">${escapeHtml(m.content)}</div>`,
      `<div class="memory-actions"><button class="btn btn-sm delete-memory-btn" data-id="${m.id}">Delete</button></div>`,
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
    const res = await fetch(`${base}/memory/${id}`, { method: "DELETE" });
    if (res.status === 404) throw new Error("not found");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    logEvent("memory.delete.ok", { id });
    void fetchMemory();
  } catch (err) {
    logEvent("memory.delete.error", { id, error: String(err) });
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function switchSession(targetSessionId) {
  if (socket) socket.close();
  messageHistory.length = 0;
  const saved = loadSessionHistory(targetSessionId);
  saved.forEach((m) => messageHistory.push(m));
  restoreMessages();
  connect(targetSessionId);
}

function connect(targetSessionId = null) {
  if (targetSessionId && typeof targetSessionId !== "string") {
    targetSessionId = null;
  }

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
    addSystemMessage(`disconnected (code=${event.code})`);
    logEvent("socket.close", { code: event.code, reason: event.reason || "" });
  };

  socket.onerror = () => {
    setStatus(false, "error");
    addSystemMessage("connection error");
    logEvent("socket.error");
  };

  socket.onmessage = (event) => {
    clearWaiting();
    try {
      const payload = JSON.parse(event.data);
      logEvent("socket.message", payload);
      if (payload.type === "connected") {
        currentSessionId = payload.session_id || null;
        sessionBadgeEl.textContent = currentSessionId || "-";
        addSession(currentSessionId || "unknown", payload.user_id || settings.userId);
        return;
      }
      if (payload.type === "agent_message") {
        appendBubble("agent", payload.message || "");
        return;
      }
      if (payload.type === "error") {
        addSystemMessage(payload.message || "error");
        return;
      }
      if (payload.type === "user_message") {
        return;
      }
      addSystemMessage(event.data);
    } catch (_err) {
      logEvent("socket.message.raw", { data: event.data });
      addSystemMessage(event.data);
    }
  };
}

function disconnect() {
  if (socket) {
    socket.close();
  }
}

function sendMessage(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    const wsUrl = toWebSocketUrl(currentSettings());
    addSystemMessage(`not connected -> connecting (${wsUrl})`);
    pendingMessage = text;
    connect();
    return;
  }

  const payload = { type: "message", message: text };
  socket.send(JSON.stringify(payload));
  logEvent("socket.send", payload);
  appendBubble("user", text);
  waitingIndicator = appendBubble("system", "thinking...", { persist: false });
}

navButtons.forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

connectBtn.addEventListener("click", () => connect());
disconnectBtn.addEventListener("click", disconnect);
saveSettingsBtn.addEventListener("click", persistSettings);
resetSettingsBtn.addEventListener("click", resetSettings);
refreshSkillsBtn.addEventListener("click", () => {
  void fetchSkills();
});
runSkillBtn.addEventListener("click", () => {
  void executeSkill();
});
refreshMemoryBtn.addEventListener("click", () => {
  void fetchMemory();
});
searchMemoryBtn.addEventListener("click", () => {
  void fetchMemory();
});
memoryQueryInputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") void fetchMemory();
});

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
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

const initialSettings = {
  ...parseStoredSettings(),
  ...parseUrlSettings()
};

applySettings(initialSettings);
renderSessions();
activateTab("chat");
setStatus(false, "offline");
addSystemMessage("ready: Configure settings then press Connect");
logEvent("ui.ready", currentSettings());
