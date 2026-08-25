#!/usr/bin/env node
// Isolated probe: task create → poll statuses, logging every list payload
// and every outgoing /api/tasks request, to find who deletes the task.
import { chromium } from "playwright-core";

const base = "http://127.0.0.1:9222";
const cdp = await chromium.connectOverCDP(base);
const ctx = cdp.contexts()[0];
const page = ctx.pages().find((p) => /minicpm-chat\.html/.test(p.url()));
if (!page) { console.error("no bubble page"); process.exit(1); }

page.on("request", (r) => {
  if (/\/api\/tasks/.test(r.url()) && r.method() !== "GET") {
    console.log(`[req] ${r.method()} ${new URL(r.url()).pathname}`);
  }
});

await new Promise((r) => setTimeout(r, 3000));
const created = await page.evaluate(() =>
  window.minicpm.tasksCreate({ name: "probe-5s", delaySeconds: 5 })
);
console.log("created:", JSON.stringify(created).slice(0, 120));

for (let i = 0; i < 14; i++) {
  await new Promise((r) => setTimeout(r, 1000));
  const snap = await page.evaluate(async () => {
    try {
      const r = await window.minicpm.tasksList();
      return (r.tasks || []).map((t) => `${t.name}:${t.status}`).join(" | ") || "(empty)";
    } catch (e) {
      return "LIST-ERROR: " + e.message;
    }
  });
  console.log(`t+${i + 1}s  ${snap}`);
}
cdp.close();
