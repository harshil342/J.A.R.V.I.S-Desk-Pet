import { chromium } from "playwright-core";

const cdp = await chromium.connectOverCDP("http://127.0.0.1:9222");
const ctx = cdp.contexts()[0];
const page = ctx.pages().find((p) => /minicpm-chat\.html/.test(p.url()));

page.on("console", (m) => console.log("[console]", m.type(), m.text().slice(0, 200)));
page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 300)));
page.on("response", async (r) => {
  if (!/\/api\/chat/.test(r.url())) return;
  let body = "";
  try { body = (await r.text()).slice(0, 400); } catch (e) { body = "<body unreadable: " + e.message + ">"; }
  console.log("[chat]", r.status(), r.request().method(), body.replace(/\n/g, " | ").slice(0, 380));
});

await new Promise((r) => setTimeout(r, 1500));
await page.focus("#ask-input");
await page.fill("#ask-input", "");
await page.type("#ask-input", "what is my weather", { delay: 15 });
await page.keyboard.press("Enter");
console.log("[sent] what is my weather");
await new Promise((r) => setTimeout(r, 12000));
const t = await page.evaluate(() => document.getElementById("content").innerText);
console.log("--- DOM ---");
console.log(t.slice(-500));
cdp.close();
