const NAV_META = {
  chat: { title: "Chat", subtitle: "Gateway WebSocket chat" },
  sessions: { title: "Sessions", subtitle: "Current browser sessions" },
  channels: { title: "Channels", subtitle: "Channel status overview" },
  skills: { title: "Skills", subtitle: "OpenClaw-style skill catalog and run" },
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
const skillsListEl = document.getElementById("skillsList");
const skillNameInputEl = document.getElementById("skillNameInput");
const skillParamsInputEl = document.getElementById("skillParamsInput");
const runSkillBtn = document.getElementById("runSkillBtn");
const skillResultEl = document.getElementById("skillResult");

let socket = null;
let waitingIndicator = null;
const sessions = [];
let pendingMessage = null;

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

function toWebSocketUrl(settings) {
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

function appendBubble(kind, text) {
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

function renderSessions() {
  if (!sessions.length) {
    sessionListEl.innerHTML = "<li>No sessions yet.</li>";
    return;
  }
  sessionListEl.innerHTML = sessions
    .map((s) => `<li><div class=\"mono\">${s.id}</div><div class=\"muted\">user: ${s.userId} / ${s.when}</div></li>`)
    .join("");
}

function addSession(sessionId, userId) {
  sessions.unshift({ id: sessionId, userId, when: new Date().toLocaleString() });
  if (sessions.length > 15) sessions.length = 15;
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
  if (tabKey === "skills") {
    void fetchSkills();
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

function connect() {
  if (socket && socket.readyState === WebSocket.OPEN) {
    addSystemMessage("already connected");
    return;
  }

  const settings = currentSettings();
  const wsUrl = toWebSocketUrl(settings);

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
        sessionBadgeEl.textContent = payload.session_id || "-";
        addSession(payload.session_id || "unknown", payload.user_id || settings.userId);
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
  waitingIndicator = addSystemMessage("thinking...");
}

navButtons.forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

connectBtn.addEventListener("click", connect);
disconnectBtn.addEventListener("click", disconnect);
saveSettingsBtn.addEventListener("click", persistSettings);
resetSettingsBtn.addEventListener("click", resetSettings);
refreshSkillsBtn.addEventListener("click", () => {
  void fetchSkills();
});
runSkillBtn.addEventListener("click", () => {
  void executeSkill();
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
