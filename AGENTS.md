# DeskPet — Agent Guidance

## graphify knowledge graph (always-on)

A queryable knowledge graph of this repo lives at `graphify-out/graph.json`
(9k+ nodes: clawd-on-desk Electron app + minicpm-sidecar Python gateway).
Before grepping raw files for architecture questions, prefer:

```
graphify query "<question>"        # scoped subgraph for a plain-language question
graphify path "A" "B"              # shortest connection between two symbols
graphify explain "<symbol>"        # one node + its neighbors, with file:line refs
graphify god-nodes                 # architectural hubs
```

- Read `graphify-out/GRAPH_REPORT.md` before broad architecture work.
- `graph.json` is committed-friendly; rebuild after code changes with
  `graphify update .` (local AST only, no API cost). Full rebuild:
  `graphify extract . --code-only`.
---

## Web fetching and scraping

**Use Scrapling for anything that fetches or parses web content.** It is
installed and verified in the agent tooling venv at `.tools/`:

```powershell
.\.tools\Scripts\python.exe scripts\check-scrapling.py   # proves it still works
```

It is a **separate venv on purpose**. Scrapling pulls `curl_cffi` and
`playwright`, and `minicpm-sidecar/pyproject.toml` must stay lean: those would
land in the PyInstaller bundle and would also fail the frozen-import check in
`build-gateway.ps1`. Nothing in the shipped product may depend on `.tools/`.

When to reach for what:

| Need | Use |
|---|---|
| A single known URL, prose is enough | `webfetch` |
| A search, or a site behind a reader wall | `websearch` |
| Structure: tables, lists, many records, exact fields | **Scrapling** |
| A page that needs JavaScript before the data exists | Scrapling `DynamicFetcher`, then `playwright install chromium` (not installed by default) |
| Social platforms (X, Reddit, LinkedIn, 小红书, B站…) | the `agent-reach` skill |

Two API facts that are easy to get wrong and cost real time:

- `Selector(url)` **does not fetch**. It wraps the string and returns a 68-byte
  stub document. Use `Fetcher.get(url)`, or `StealthyFetcher` for a hardened
  request.
- There is no `css_first()`. Use `page.css(sel).extract_first()` for the first
  text/attribute and `page.css(sel)` for a matcher.

Full working example with the assertions: `scripts/check-scrapling.py`.

## Design and UI quality

**The renderer is vanilla HTML/CSS/JS. Keep it that way.** No React, no
Tailwind, no Framer Motion. Watermelon UI and Componentry are both excellent and
both are React-first; adopting either would add ~100 KB gzipped to a pet that
runs all day. Take their *design language* — motion curves, elevation, spacing
rhythm — and write it in CSS using **compositor-only properties** (`transform`,
`opacity`). Never `width`, `height`, `padding`, `margin`, `top`/`left` in a
`transition`: those reflow every frame.

Regenerate the design tooling (not committed — 14 MB of it is a Windows binary):

```powershell
npx impeccable install --providers=opencode --scope=project --no-hooks
```

Then run the **deterministic** detector before and after any UI change. It needs
no LLM and no API key, so it works in CI:

```powershell
.\.opencode\skills\impeccable\scripts\bin\windows-x64\impeccable.exe detect clawd-on-desk/src
```

Baseline on `7411f43` was **37 findings**; see `docs/ui-audit.md` for the
breakdown and what is deliberately left. It flags AI-slop tells (chromatic
glows, 3px side-tab borders, bounce easing) as well as the layout-transition and
oversized-text problems that cost frames and legibility.

## Release hygiene

- The release hook lives at `scripts/release.mjs` (`npm run release:check`).
  It single-sources the version across `package.json`, `pyproject.toml` and
  `CHANGELOG.md`, refuses a dirty tree, and creates a **draft** when no signing
  certificate is configured — an unsigned public release would be rejected by
  electron-updater at install time.
- `clawd-on-desk/scripts/*` is gitignored with an allowlist. A new script there
  needs a `!` line in `.gitignore` in the same commit or it will silently not ship.
- Signing: `npm run sign:dev` / `sign:check` / `build:win:signed`.
- Before any claim about what is or is not working: `npm test` in
  `clawd-on-desk`, and `uv run --project minicpm-sidecar pytest`. Both gate the
  release workflow.
