const DEFAULT_RELAY_URL = "ws://127.0.0.1:8768";
const DEFAULT_RELAY_TOKEN = "";
const DEFAULT_CONTROL_UI_ORIGIN = "";
const RECONNECT_DELAY_MS = 2000;
const KEEPALIVE_INTERVAL_MS = 20000;

let socket = null;
let reconnectTimer = null;
let keepaliveTimer = null;
let relayConfig = {
  relayUrl: DEFAULT_RELAY_URL,
  relayToken: DEFAULT_RELAY_TOKEN,
  controlUiOrigin: DEFAULT_CONTROL_UI_ORIGIN
};

async function loadRelayConfig() {
  const stored = await chrome.storage.local.get({
    relayUrl: DEFAULT_RELAY_URL,
    relayToken: DEFAULT_RELAY_TOKEN,
    controlUiOrigin: DEFAULT_CONTROL_UI_ORIGIN
  });
  relayConfig = {
    relayUrl: String(stored.relayUrl || DEFAULT_RELAY_URL),
    relayToken: String(stored.relayToken || DEFAULT_RELAY_TOKEN),
    controlUiOrigin: String(stored.controlUiOrigin || DEFAULT_CONTROL_UI_ORIGIN).trim()
  };
  return relayConfig;
}

function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "ping" }));
    }
  }, KEEPALIVE_INTERVAL_MS);
}

function stopKeepalive() {
  if (keepaliveTimer !== null) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimer !== null) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectRelay();
  }, RECONNECT_DELAY_MS);
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tabs.length) {
    throw new Error("No active tab found");
  }
  return tabs[0];
}

async function getTargetTab(targetTabId) {
  if (!targetTabId) {
    return getActiveTab();
  }
  return chrome.tabs.get(targetTabId);
}

function normalizeConfiguredOrigin(origin) {
  const value = String(origin || "").trim();
  if (!value) {
    return "";
  }
  try {
    const parsed = new URL(value);
    return `${parsed.protocol}//${parsed.host}`;
  } catch (_error) {
    return value.replace(/\/+$/, "");
  }
}

function isLoopbackHost(hostname) {
  const lowered = String(hostname || "").toLowerCase();
  return lowered === "localhost" || lowered === "127.0.0.1" || lowered === "::1" || lowered === "[::1]";
}

function isLoopbackChatUrl(url) {
  try {
    const parsed = new URL(String(url || ""));
    return isLoopbackHost(parsed.hostname) && parsed.pathname.replace(/\/+$/, "") === "/chat";
  } catch (_error) {
    return false;
  }
}

function matchesConfiguredControlUiOrigin(url, configuredOrigin) {
  const normalizedOrigin = normalizeConfiguredOrigin(configuredOrigin);
  if (!normalizedOrigin) {
    return false;
  }
  try {
    const parsed = new URL(String(url || ""));
    const candidateOrigin = `${parsed.protocol}//${parsed.host}`;
    return (
      candidateOrigin === normalizedOrigin &&
      parsed.pathname.replace(/\/+$/, "") === "/chat"
    );
  } catch (_error) {
    return false;
  }
}

function isControlUiTab(tab) {
  const url = String(tab?.url || "").toLowerCase();
  const title = String(tab?.title || "").toLowerCase();
  return (
    matchesConfiguredControlUiOrigin(url, relayConfig.controlUiOrigin) ||
    isLoopbackChatUrl(url) ||
    title.includes("boiled-claw control ui")
  );
}

function isExternalNavigationTarget(url) {
  const value = String(url || "");
  return Boolean(value) && !isControlUiTab({ url: value, title: "" });
}

function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    let timer = null;

    const cleanup = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
      chrome.tabs.onUpdated.removeListener(listener);
    };

    const listener = (updatedTabId, changeInfo, tab) => {
      if (updatedTabId !== tabId) {
        return;
      }
      if (changeInfo.status === "complete") {
        cleanup();
        resolve(tab);
      }
    };

    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") {
        cleanup();
        resolve(tab);
        return;
      }

      timer = setInterval(() => {
        if (Date.now() > deadline) {
          cleanup();
          reject(new Error("Tab navigation timed out"));
        }
      }, 100);
    }).catch((error) => {
      cleanup();
      reject(error);
    });
  });
}

async function executeInActiveTab(func, args) {
  const tab = await getActiveTab();
  if (!tab.id) {
    throw new Error("Active tab is missing an id");
  }
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func,
    args
  });
  if (!result) {
    throw new Error("Script execution returned no result");
  }
  if (result.result && result.result.__boiledClawError) {
    throw new Error(result.result.__boiledClawError);
  }
  return { tab, value: result.result };
}

function selectorClick(selector) {
  const element = document.querySelector(selector);
  if (!element) {
    return { __boiledClawError: `Selector not found: ${selector}` };
  }
  element.click();
  return { selector };
}

function selectorFill(selector, text) {
  const element = document.querySelector(selector);
  if (!element) {
    return { __boiledClawError: `Selector not found: ${selector}` };
  }
  element.focus();
  element.value = text;
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
  return { selector, text_length: text.length };
}

function selectorExtractText(selector) {
  const target = selector ? document.querySelector(selector) : document.body;
  if (!target) {
    return { __boiledClawError: `Selector not found: ${selector}` };
  }
  const text = target.innerText || target.textContent || "";
  return {
    selector: selector || "body",
    text,
    length: text.length
  };
}

async function handleRequest(message) {
  const action = message.action;
  const payload = message.payload || {};

  if (action === "get_active_tab") {
    const tab = await getActiveTab();
    return {
      tab_id: tab.id,
      window_id: tab.windowId,
      url: tab.url || "",
      title: tab.title || ""
    };
  }

  if (action === "navigate") {
    const tab = await getTargetTab(payload.target_tab_id || null);
    const shouldOpenNewTab =
      Boolean(payload.new_tab) ||
      (isControlUiTab(tab) && isExternalNavigationTarget(payload.url));
    if (shouldOpenNewTab) {
      // Open in a new tab to preserve the relay host tab (e.g. Control UI).
      const created = await chrome.tabs.create({ url: payload.url, active: true });
      const completed = await waitForTabComplete(created.id, payload.timeout_ms || 15000);
      return {
        tab_id: completed.id,
        window_id: completed.windowId,
        url: completed.url || "",
        title: completed.title || "",
        new_tab: true
      };
    }
    if (!tab.id) {
      throw new Error("Active tab is missing an id");
    }
    await chrome.tabs.update(tab.id, { url: payload.url, active: true });
    const updated = await waitForTabComplete(tab.id, payload.timeout_ms || 15000);
    return {
      tab_id: updated.id,
      window_id: updated.windowId,
      url: updated.url || "",
      title: updated.title || ""
    };
  }

  if (action === "activate_tab") {
    if (!payload.tab_id) {
      throw new Error("tab_id is required");
    }
    const updated = await chrome.tabs.update(payload.tab_id, { active: true });
    if (updated.windowId) {
      await chrome.windows.update(updated.windowId, { focused: true });
    }
    return {
      tab_id: updated.id,
      window_id: updated.windowId,
      url: updated.url || "",
      title: updated.title || ""
    };
  }

  if (action === "click") {
    const { value } = await executeInActiveTab(selectorClick, [payload.selector]);
    return value;
  }

  if (action === "fill") {
    const { value } = await executeInActiveTab(selectorFill, [payload.selector, payload.text || ""]);
    return value;
  }

  if (action === "extract_text") {
    const targetTab = await getTargetTab(payload.target_tab_id || null);
    if (!targetTab.id) {
      throw new Error("Active tab is missing an id");
    }
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: targetTab.id },
      func: selectorExtractText,
      args: [payload.selector || null]
    });
    if (!result) {
      throw new Error("Script execution returned no result");
    }
    if (result.result && result.result.__boiledClawError) {
      throw new Error(result.result.__boiledClawError);
    }
    const value = result.result;
    return value;
  }

  throw new Error(`Unsupported action: ${action}`);
}

function sendMessage(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

async function connectRelay() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  const config = await loadRelayConfig();
  socket = new WebSocket(config.relayUrl);

  socket.addEventListener("open", () => {
    sendMessage({
      type: "hello",
      extension: "boiled-claw-current-tab-adapter",
      version: "0.1.0",
      token: config.relayToken || ""
    });
    startKeepalive();
  });

  socket.addEventListener("message", async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_error) {
      return;
    }
    if (message.type === "hello_ack") {
      console.log("[relay] connection established, server acknowledged hello");
      return;
    }
    if (message.type === "pong") {
      return;
    }
    if (message.type !== "request" || !message.request_id) {
      return;
    }

    try {
      const result = await handleRequest(message);
      sendMessage({
        type: "response",
        request_id: message.request_id,
        ok: true,
        result
      });
    } catch (error) {
      sendMessage({
        type: "response",
        request_id: message.request_id,
        ok: false,
        error: error instanceof Error ? error.message : String(error)
      });
    }
  });

  socket.addEventListener("close", (event) => {
    console.log(`[relay] closed: code=${event.code} reason=${event.reason || "(none)"}`);
    stopKeepalive();
    socket = null;
    scheduleReconnect();
  });

  socket.addEventListener("error", (event) => {
    console.warn("[relay] error:", event);
    if (socket) {
      socket.close();
    }
  });
}

chrome.runtime.onStartup.addListener(() => {
  connectRelay();
});

chrome.runtime.onInstalled.addListener(() => {
  connectRelay();
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local") {
    return;
  }
  if (!changes.relayUrl && !changes.relayToken && !changes.controlUiOrigin) {
    return;
  }
  if (socket) {
    socket.close();
  } else {
    connectRelay();
  }
});

connectRelay();
