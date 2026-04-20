const DEFAULT_RELAY_URL = "ws://127.0.0.1:8768";
const DEFAULT_CONTROL_UI_ORIGIN = "";

const relayUrlInput = document.getElementById("relay-url");
const relayTokenInput = document.getElementById("relay-token");
const controlUiOriginInput = document.getElementById("control-ui-origin");
const statusEl = document.getElementById("status");
const saveButton = document.getElementById("save");

async function restore() {
  const stored = await chrome.storage.local.get({
    relayUrl: DEFAULT_RELAY_URL,
    relayToken: "",
    controlUiOrigin: DEFAULT_CONTROL_UI_ORIGIN
  });
  relayUrlInput.value = String(stored.relayUrl || DEFAULT_RELAY_URL);
  relayTokenInput.value = String(stored.relayToken || "");
  controlUiOriginInput.value = String(stored.controlUiOrigin || DEFAULT_CONTROL_UI_ORIGIN);
}

async function save() {
  const relayUrl = String(relayUrlInput.value || DEFAULT_RELAY_URL).trim();
  const relayToken = String(relayTokenInput.value || "").trim();
  const controlUiOrigin = String(controlUiOriginInput.value || DEFAULT_CONTROL_UI_ORIGIN).trim();
  await chrome.storage.local.set({ relayUrl, relayToken, controlUiOrigin });
  statusEl.textContent = "Saved. The relay connection will reconnect with the new settings.";
}

saveButton.addEventListener("click", () => {
  save().catch((error) => {
    statusEl.textContent = error instanceof Error ? error.message : String(error);
  });
});

restore().catch((error) => {
  statusEl.textContent = error instanceof Error ? error.message : String(error);
});
