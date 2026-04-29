const DEFAULT_RELAY_URL = "ws://127.0.0.1:8768";
const DEFAULT_RELAY_TOKEN = "";
const RECONNECT_DELAY_MS = 2000;
const KEEPALIVE_INTERVAL_MS = 20000;

let socket = null;
let reconnectTimer = null;
let keepaliveTimer = null;
let relayConfig = {
  relayUrl: DEFAULT_RELAY_URL,
  relayToken: DEFAULT_RELAY_TOKEN
};

async function loadRelayConfig() {
  const stored = await chrome.storage.local.get({
    relayUrl: DEFAULT_RELAY_URL,
    relayToken: DEFAULT_RELAY_TOKEN
  });
  relayConfig = {
    relayUrl: String(stored.relayUrl || DEFAULT_RELAY_URL),
    relayToken: String(stored.relayToken || DEFAULT_RELAY_TOKEN)
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

function isControlUiTab(tab) {
  const url = String(tab?.url || "").toLowerCase();
  const title = String(tab?.title || "").toLowerCase();
  return url.includes("localhost:18789/chat") || title.includes("boiled-claw control ui");
}

function isExternalNavigationTarget(url) {
  const lowered = String(url || "").toLowerCase();
  return !!lowered && !lowered.includes("localhost:18789/chat");
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

  if (action === "list_tabs") {
    // Read-only tab enumeration. Unlike activate_tab / navigate, this has no
    // side effects on focus or window state — used by the verification path to
    // discover candidate destination tabs (e.g. the Google Sheets tab we
    // opened earlier) without disturbing the currently-focused Control UI tab.
    const allTabs = await chrome.tabs.query({});
    return {
      tabs: allTabs.map((t) => ({
        tab_id: t.id,
        window_id: t.windowId,
        url: t.url || "",
        title: t.title || "",
        active: !!t.active,
        index: typeof t.index === "number" ? t.index : null
      }))
    };
  }

  if (action === "activate_tab") {
    if (!payload.tab_id) {
      throw new Error("tab_id is required");
    }
    // Pre-check: verify the tab still exists and surface a descriptive error
    // (with known-tab context) when it doesn't. Chrome's `tabs.update` on a
    // missing id rejects with just "No tab with id: N" — useless for debugging
    // when the underlying cause is e.g. a cross-window id mismatch or a tab
    // closed between navigate and activate.
    let existing;
    try {
      existing = await chrome.tabs.get(payload.tab_id);
    } catch (getError) {
      let knownTabsSummary = "";
      try {
        const allTabs = await chrome.tabs.query({});
        knownTabsSummary = allTabs
          .map((t) => `${t.id}@w${t.windowId}:${(t.url || t.title || "").slice(0, 60)}`)
          .join(" | ");
      } catch (_queryError) {
        knownTabsSummary = "<chrome.tabs.query failed>";
      }
      throw new Error(
        `activate_tab: target tab_id=${payload.tab_id} not found ` +
        `(${getError instanceof Error ? getError.message : String(getError)}). ` +
        `Known tabs: ${knownTabsSummary}`
      );
    }
    const updated = await chrome.tabs.update(payload.tab_id, { active: true });
    const effective = updated || existing;
    if (effective && effective.windowId) {
      try {
        await chrome.windows.update(effective.windowId, { focused: true });
      } catch (focusError) {
        // Don't fail the whole activation just because the window focus
        // request failed — surface it via the return so the caller can see
        // focus_succeeded is not implicit.
        return {
          tab_id: effective.id,
          window_id: effective.windowId,
          url: effective.url || "",
          title: effective.title || "",
          window_focus_error:
            focusError instanceof Error ? focusError.message : String(focusError)
        };
      }
    }
    return {
      tab_id: effective ? effective.id : payload.tab_id,
      window_id: effective ? effective.windowId : null,
      url: effective && effective.url ? effective.url : "",
      title: effective && effective.title ? effective.title : ""
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
  if (!changes.relayUrl && !changes.relayToken) {
    return;
  }
  if (socket) {
    socket.close();
  } else {
    connectRelay();
  }
});

connectRelay();
