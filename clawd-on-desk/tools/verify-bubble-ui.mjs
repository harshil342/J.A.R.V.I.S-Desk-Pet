#!/usr/bin/env node
// UI smoke verifier for the chat bubble, via Chrome DevTools Protocol.
//
// Usage:
//   1) Start the app with DESKPET_REMOTE_DEBUGGING_PORT=9222 (launch.js
//      forwards it to Electron).
//   2) Open the chat bubble once (Ctrl+Shift+M) so the window exists.
//   3) node tools/verify-bubble-ui.mjs
//
// Verifies the exact regression class curl tests cannot see: the backend
// answered but the bubble rendered nothing (or leaked markup into the DOM).

import { chromium } from "playwright-core";

const PORT = process.env.DESKPET_REMOTE_DEBUGGING_PORT || "9222";
const base = `http://127.0.0.1:${PORT}`;

async function findBubblePage() {
  const cdp = await chromium.connectOverCDP(base);
  const ctx = cdp.contexts()[0];
  if (!ctx) throw new Error("no browser context — is the app running?");
  for (let i = 0; i < 20; i++) {
    const page = ctx.pages().find((p) => /minicpm-chat\.html/.test(p.url()));
    if (page) return { cdp, page };
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("chat bubble window not found — open it first (Ctrl+Shift+M)");
}

async function contentText(page) {
  return page.evaluate(() => {
    const el = document.getElementById("content");
    return el ? el.innerText : "";
  });
}

async function send(page, text) {
  // Retry until the input actually cleared — early keystrokes can be
  // swallowed while the auto-opened bubble settles.
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.focus("#ask-input");
    await page.fill("#ask-input", "");
    await page.type("#ask-input", text, { delay: 15 });
    await page.keyboard.press("Enter");
    await new Promise((r) => setTimeout(r, 600));
    const left = await page.inputValue("#ask-input").catch(() => "");
    if (!left.trim()) return;
  }
  throw new Error(`could not send "${text}" — input never cleared`);
}

async function waitUntil(page, predicate, timeoutMs, label) {
  const start = Date.now();
  let last = "";
  let lastLog = 0;
  while (Date.now() - start < timeoutMs) {
    last = await contentText(page);
    if (predicate(last)) return last;
    if (Date.now() - lastLog > 5000) {
      lastLog = Date.now();
      const probe = await page.evaluate(() => {
        const c = document.getElementById("content");
        const speaks = c ? Array.from(c.querySelectorAll(".speak")).map((s) => s.textContent.slice(0, 40)) : [];
        return { panes: c ? c.children.length : 0, speaks, vis: document.visibilityState };
      }).catch(() => null);
      console.log(`  [wait ${label}] ${JSON.stringify(probe)}`);
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`timeout waiting for ${label}\n--- last bubble text ---\n${last.slice(-600)}`);
}

const results = [];
function record(name, ok, detail = "") {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` :: ${detail}` : ""}`);
}

const { cdp, page } = await findBubblePage();
// NOTE: deliberately NOT calling page.bringToFront() — occluding the
// bubble throttles requestAnimationFrame and starves the typewriter, so
// text never paints even though SSE arrived (real Electron quirk).

// Self-diagnosis: log every chat SSE exchange and any renderer error.
page.on("console", (m) => {
  if (/error/i.test(m.type())) console.log("  [renderer-error]", m.text().slice(0, 200));
});
page.on("pageerror", (e) => console.log("  [pageerror]", String(e).slice(0, 250)));
page.on("response", async (r) => {
  if (!/\/api\/chat/.test(r.url())) return;
  let body = "";
  try { body = (await r.text()).replace(/\s+/g, " ").slice(0, 220); } catch {}
  console.log(`  [chat ${r.status()}] ${body}`);
});

try {
  // Settle like a human: window shown, layout done, focus stable.
  await new Promise((r) => setTimeout(r, 2500));
  // 1 — weather clarify ask appears in the DOM
  await send(page, "what is my weather");
  await waitUntil(page, (t) => /which city/i.test(t), 45000, "weather clarify ask");
  record("weather asks for a city once", true);
  await new Promise((r) => setTimeout(r, 1500));

  // 2 — bare city completes the request with real data
  await send(page, "mumbai");
  const t2 = await waitUntil(
    page,
    (t) => /°C/.test(t) && !/which city/i.test(t.split("mumbai")[1] ?? t),
    90000,
    "weather answer",
  );
  record("bare-city follow-up returns weather", true);

  // 3 — no tool-call markup ever leaks into the visible DOM
  record("no <function> markup in DOM", !/<function|<param|<tool_call/i.test(t2));
} catch (err) {
  record("flow", false, String(err.message || err).slice(0, 500));
}

cdp.close();
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
