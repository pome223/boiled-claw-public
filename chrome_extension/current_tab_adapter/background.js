const RELAY_URL = "ws://127.0.0.1:8768";
const RECONNECT_DELAY_MS = 2000;

let socket = null;
let reconnectTimer = null;

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

function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;

    const cleanup = () => {
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

      const timer = setInterval(() => {
        if (Date.now() > deadline) {
          clearInterval(timer);
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
    const tab = await getActiveTab();
    if (!tab.id) {
      throw new Error("Active tab is missing an id");
    }
    await chrome.tabs.update(tab.id, { url: payload.url });
    const updated = await waitForTabComplete(tab.id, payload.timeout_ms || 15000);
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
    const { value } = await executeInActiveTab(selectorExtractText, [payload.selector || null]);
    return value;
  }

  throw new Error(`Unsupported action: ${action}`);
}

function sendMessage(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

function connectRelay() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  socket = new WebSocket(RELAY_URL);

  socket.addEventListener("open", () => {
    sendMessage({
      type: "hello",
      extension: "boiled-claw-current-tab-adapter",
      version: "0.1.0"
    });
  });

  socket.addEventListener("message", async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_error) {
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

  socket.addEventListener("close", () => {
    socket = null;
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
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

connectRelay();
