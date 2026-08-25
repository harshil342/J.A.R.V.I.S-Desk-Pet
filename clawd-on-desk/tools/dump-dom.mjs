import { chromium } from "playwright-core";

const cdp = await chromium.connectOverCDP("http://127.0.0.1:9222");
const ctx = cdp.contexts()[0];
const page = ctx.pages().find((p) => /minicpm-chat\.html/.test(p.url()));
if (!page) { console.error("bubble not open"); process.exit(1); }

const info = await page.evaluate(() => {
  const content = document.getElementById("content");
  const kids = content ? Array.from(content.children).map((c) => ({
    tag: c.tagName,
    cls: c.className,
    text: (c.innerText || "").slice(0, 90),
  })) : [];
  return { count: kids.length, kids };
});
console.log(JSON.stringify(info, null, 1));
cdp.close();
