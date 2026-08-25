"use strict";

// Chat history persistence limits + pure v2 session-store helpers.
// Extracted from minicpm-chat.js, which re-imports every name so its
// eval-based tests keep binding them from that module's scope.
// Chat history persistence limits. The renderer owns the authoritative
// in-memory copy and mirrors it to <userData>/minicpm-chat-history.json
// via IPC; sanitizeHistoryItems runs on BOTH sides of that boundary so
// load/save can never balloon context, inject roles, or persist junk.
const HISTORY_MAX_MESSAGES = 60;          // ~30 user/assistant turns
const HISTORY_MAX_CONTENT_CHARS = 24000;
const HISTORY_MAX_SESSIONS = 20;          // named chats kept per file
const HISTORY_DEFAULT_SESSION = "default";

function sanitizeHistoryItems(items) {
  const clean = [];
  if (!Array.isArray(items)) return clean;
  for (const m of items) {
    if (!m || typeof m !== "object") continue;
    const role = m.role === "user" || m.role === "assistant" ? m.role : null;
    if (!role) continue;
    if (typeof m.content !== "string" || !m.content.trim()) continue;
    clean.push({ role, content: m.content.slice(0, HISTORY_MAX_CONTENT_CHARS) });
  }
  return clean.slice(-HISTORY_MAX_MESSAGES);
}

// ── v2 session store (named chat conversations) ───────────────────────────
// On-disk shape:
//   { version: 2, activeId, sessions: { <id>: { name, messages: [...] } } }
// normalizeHistoryStore accepts every historical shape — v2 objects, the
// v1 wrapper { version: 1, history }, and bare JSON arrays — and always
// returns a valid store, so a corrupt or hand-edited file degrades to a
// fresh default session instead of breaking the chat.
function freshHistoryStore() {
  const sessions = {};
  sessions[HISTORY_DEFAULT_SESSION] = { name: "Chat", messages: [] };
  return { version: 2, activeId: HISTORY_DEFAULT_SESSION, sessions };
}

// Enforce the session cap (oldest non-active evicted first, insertion
// order = age) and resolve activeId to a session that actually exists.
// Guarantees at least one session — the store can never end up empty.
function capHistoryStore(store, wantedActiveId) {
  const ids = Object.keys(store.sessions);
  if (!ids.length) return freshHistoryStore();
  let activeId = typeof wantedActiveId === "string" && store.sessions[wantedActiveId]
    ? wantedActiveId
    : ids[0];
  for (const id of ids) {
    if (Object.keys(store.sessions).length <= HISTORY_MAX_SESSIONS) break;
    if (id === activeId) continue;
    delete store.sessions[id];
  }
  if (!store.sessions[activeId]) activeId = Object.keys(store.sessions)[0];
  return { version: 2, activeId, sessions: store.sessions };
}

function normalizeHistoryStore(parsed) {
  const store = freshHistoryStore();
  // Bare legacy array → the whole file is one session's messages.
  if (Array.isArray(parsed)) {
    store.sessions[HISTORY_DEFAULT_SESSION].messages = sanitizeHistoryItems(parsed);
    return store;
  }
  if (!parsed || typeof parsed !== "object") return store; // corrupt → fresh
  if (parsed.sessions && typeof parsed.sessions === "object" && !Array.isArray(parsed.sessions)) {
    for (const id of Object.keys(parsed.sessions)) {
      const sess = parsed.sessions[id];
      if (!sess || typeof sess !== "object") continue;
      const name = typeof sess.name === "string" && sess.name.trim()
        ? sess.name.trim().slice(0, 60)
        : "Chat";
      store.sessions[String(id)] = { name, messages: sanitizeHistoryItems(sess.messages) };
    }
  } else if (Array.isArray(parsed.history)) {
    // v1 wrapper { version: 1, history } → migrate into the default session.
    store.sessions[HISTORY_DEFAULT_SESSION].messages = sanitizeHistoryItems(parsed.history);
  }
  return capHistoryStore(store, parsed && typeof parsed.activeId === "string" ? parsed.activeId : HISTORY_DEFAULT_SESSION);
}

module.exports = {
  sanitizeHistoryItems,
  freshHistoryStore,
  capHistoryStore,
  normalizeHistoryStore,
  HISTORY_MAX_MESSAGES,
  HISTORY_MAX_CONTENT_CHARS,
  HISTORY_MAX_SESSIONS,
  HISTORY_DEFAULT_SESSION,
};
