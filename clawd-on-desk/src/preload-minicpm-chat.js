"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("minicpm", {
  // Sidecar lifecycle
  start: (opts) => ipcRenderer.invoke("minicpm:start", opts),
  status: () => ipcRenderer.invoke("minicpm:status"),

  // Bubble window controls
  resize: (width, height) => ipcRenderer.invoke("minicpm:resize", { width, height }),
  setChatAnchor: (bottomY) => ipcRenderer.invoke("minicpm:set-chat-anchor", { bottomY }),
  hideWindow: () => ipcRenderer.invoke("minicpm:hide-window"),
  showWindow: () => ipcRenderer.invoke("minicpm:show-window"),
  focusWindow: () => ipcRenderer.invoke("minicpm:focus-window"),
  openContextMenu: () => ipcRenderer.send("minicpm:open-context-menu"),

  // Updater
  updateStatus: () => ipcRenderer.invoke("minicpm:update-status"),
  updateApply:  () => ipcRenderer.invoke("minicpm:update-apply"),

  // Chat generation parameters (shared with Settings tab)
  getChatParams: () => ipcRenderer.invoke("minicpm:get-chat-params"),

  // Chat history persistence — mirrored to <userData>/minicpm-chat-history.json
  // by main (capped + sanitized there). v2 session store both ways:
  // loadHistory resolves { ok, store: { version, activeId, sessions } };
  // saveHistory accepts that same store object and returns { ok, count, sessions }.
  loadHistory: () => ipcRenderer.invoke("minicpm-chat:history-load"),
  saveHistory: (store) => ipcRenderer.invoke("minicpm-chat:history-save", { store }),

  // Sidecar lifecycle events from the crash auto-restart supervisor:
  // { state: "restarting", attempt } | { state: "back" } | { state: "down", reason }
  onSidecarState: (cb) => {
    const listener = (_e, p) => cb(p || {});
    ipcRenderer.on("minicpm:sidecar-state", listener);
    return () => ipcRenderer.removeListener("minicpm:sidecar-state", listener);
  },

  // Adapter (LoRA) load/unload — same IPC handler the Settings tab
  // uses, so chat-based switching ("切到猫娘") persists the user's
  // choice to prefs and shares the 90s timeout + bubble notification
  // pipeline. Pass `null` to unload.
  loadAdapter: (pathOrNull) => ipcRenderer.invoke("minicpm-settings:load-adapter", { path: pathOrNull }),

  // i18n: initial fetch + live updates
  getI18n: () => ipcRenderer.invoke("minicpm:get-i18n"),
  onLangChange: (cb) => {
    const listener = (_e, payload) => { try { cb(payload || {}); } catch {} };
    ipcRenderer.on("minicpm:lang-change", listener);
    return () => ipcRenderer.removeListener("minicpm:lang-change", listener);
  },

  // Assistant look/feel + behavior prefs (clawd-prefs.json → Settings →
  // Deskpet Assistant). getAssistantPrefs resolves the validated snapshot
  // projection from main; onAssistantPrefsChanged fires on the shared
  // "settings-changed" broadcast (no payload needed — refetch after it).
  getAssistantPrefs: () => ipcRenderer.invoke("minicpm:get-assistant-prefs"),
  onAssistantPrefsChanged: (cb) => {
    const listener = () => { try { cb(); } catch {} };
    ipcRenderer.on("settings-changed", listener);
    return () => ipcRenderer.removeListener("settings-changed", listener);
  },
  // Write path for the quick-customize popover: { patch } of whitelisted
  // assistant pref keys → validated settings-controller commits in main.
  // Resolves { status: "ok" } or { status: "error", message }.
  setAssistantPrefs: (patch) => ipcRenderer.invoke("minicpm:set-assistant-prefs", { patch }),

  // Messages from main → renderer
  onOpen:           (cb) => ipcRenderer.on("minicpm:cmd-open",            (_e, payload) => cb(payload || {})),
  onDismiss:        (cb) => ipcRenderer.on("minicpm:cmd-dismiss",         () => cb()),
  onReset:          (cb) => ipcRenderer.on("minicpm:cmd-reset",           () => cb()),
  onToggleThinking: (cb) => ipcRenderer.on("minicpm:cmd-toggle-thinking", () => cb()),
  onUpdateStatus:   (cb) => ipcRenderer.on("minicpm:update-status",       (_e, p) => cb(p || {})),
  onUpdateApplying: (cb) => ipcRenderer.on("minicpm:update-applying",     (_e, p) => cb(p || {})),
  onNarrate:        (cb) => ipcRenderer.on("minicpm:narrate",             (_e, p) => cb(p || {})),
  onCmdReply:       (cb) => ipcRenderer.on("minicpm:cmd-reply",           (_e, p) => cb(p || {})),
  onEditMode:       (cb) => ipcRenderer.on("minicpm:edit-mode",           (_e, p) => cb(p || {})),

  // Native LLM Tool Engine IPC streams
  submitChat: (payload) => ipcRenderer.send("minicpm:chat-request", payload),
  onChatDelta: (cb) => {
    const listener = (_e, p) => cb(p);
    ipcRenderer.on("minicpm:chat-delta", listener);
    return () => ipcRenderer.removeListener("minicpm:chat-delta", listener);
  },
  onChatDone: (cb) => {
    const listener = () => cb();
    ipcRenderer.on("minicpm:chat-done", listener);
    return () => ipcRenderer.removeListener("minicpm:chat-done", listener);
  },
  onChatError: (cb) => {
    const listener = (_e, p) => cb(p);
    ipcRenderer.on("minicpm:chat-error", listener);
    return () => ipcRenderer.removeListener("minicpm:chat-error", listener);
  },
});
