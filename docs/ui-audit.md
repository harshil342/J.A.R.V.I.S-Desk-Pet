# UI audit — baseline and plan

Objective baseline from the deterministic detector in
[impeccable](https://impeccable.style) (Apache-2.0). No LLM, no API key, so this
is reproducible in CI.

```powershell
npx impeccable install --providers=opencode --scope=project --no-hooks
.\.opencode\skills\impeccable\scripts\bin\windows-x64\impeccable.exe detect clawd-on-desk/src
```

## Constraint: no framework

The renderer is vanilla HTML/CSS/JS with no bundler. **That stays.** Watermelon
UI (`ui.watermelon.sh`, shadcn-compatible) and Componentry
(`componentry.dev`, MIT) are both React 19 + Tailwind v4 + Framer Motion.
Installing either adds ~100 KB gzipped of runtime to an app that is open all
day, against the stated primary goal of using fewer resources.

So we take their **design language** — easing curves, elevation scale, spacing
rhythm, interaction patterns — and implement it in CSS against
compositor-only properties. Same feel, zero dependencies.

## Findings

| Count | Rule | What it means here |
|---|---|---|
| 15 | `undersized-ui-text` | 10px functional text in the model/voice recipe rows ("LLM: 1B Q8_0 (~1.15GB)", "VRAM: ~2.4 GB"). Below the 11px legibility floor; fails on high-DPI. |
| 6 | `clipped-overflow-container` | `html`/`body` plus `div.bubble` clipping absolutely-positioned children. |
| 4 | `tiny-text` | 11px body text. |
| 3 | `dark-glow` | Chromatic `box-shadow` halos. |
| 2 | `side-tab` | 3px `border-left`. |
| 1 | `marquee` | Infinite skeleton sheen. |
| 1 | `layout-transition` | `max-height` on the collapsible body (see below). |

**37 → 9.** The gate is `node scripts/ui-detect.mjs`; the ceiling lives in
`scripts/ui-detect-baseline.json` and only has to move down, so adding a finding
becomes a deliberate act. Wired into both workflows.

## What was fixed

**Type and legibility (19 findings).** Every hardcoded `10px`/`11px` in
`minicpm-onboarding.css` now comes from the token scale — `--text-micro` and
`--text-small`. All 19 cleared in one change, which is the argument for tokens
over per-rule overrides.

**Motion was made compositor-only.** Three progress bars animated `width`, which
relayouts every frame; they now animate `scaleX` with `transform-origin`, and
each needed a matching JS change because all three set `style.width` themselves.
`will-change: max-height, opacity, transform` came off the collapsible body —
`max-height` cannot composite, so the browser was holding resources permanently
for an animation that could never leave the main thread. `padding` and
`margin-top` came off two transitions.

**The infinite sheen is conditional.** It looped forever whenever the
diagnostics modal was open — for a panel that usually just shows a finished
result. Now honours `prefers-reduced-motion` and stops once loading ends.

**The genuine coloured glows went.** `.preset-option.auto-card.selected` had
`0 0 10px rgba(56,189,248,0.25)`; selection was already unambiguous from the
border, background tint and filled radio, so the glow was redundant. The volume
slider thumb hover and the language picker were the same. All now use
`--elevation-3`.

**Two input focus rings were rebuilt as outlines.** `.bubble-policy-seconds` and
`.hardware-buddy-text-input` set `outline: none` and substituted a tinted
`box-shadow`. That is worse in two ways: a shadow ring does not follow
`border-radius` the way `outline` does, and it disappears in forced-colours mode.
They now use the same `:focus-visible` outline as everything else.

**Tokens.** New `src/tokens.css`, loaded first in all ten pages: spacing (4px
base with semantic aliases), radius, elevation, duration, easing, type, focus,
and z-layers. `settings.css` already had 71 custom properties and every one was a
colour — no spacing, radius, shadow, duration or easing scale existed, which is
exactly how 100 KB of CSS accreted into an assembled-looking UI.

Colour deliberately stays in each page's own `:root`, so a page can be re-themed
without touching the shared file.

## Accepted, and why

These are the remaining 9. Each is a judgement call, stated rather than hidden.

**6 × `clipped-overflow-container` — not defects.** The positioned children are
decorative chrome that is *meant* to be contained: the bubble tail, the
"? new revision" pill, the header actions. No menu, dropdown or tooltip needs to
escape. The CSS already documents the inset as deliberate ("shell hugs window
edges with small inset so shadow never clips"). Making overflow visible would
let the tail and pill escape the bubble and break the design.

**1 × `layout-transition` — needs its own change.** `max-height` on the
collapsible body. The `grid-template-rows: 0fr → 1fr` technique is the correct
modern fix and `.doctor-agent-body` already uses it in this codebase, so the
conversion is obvious. It needs the JS to toggle a class instead of setting the
measured `--collapsible-body-height`, which touches the measured-height,
scroll-anchor-preservation and `inert`/`aria-hidden` logic — all of which have
their own tests. Worth doing properly rather than inside a perf pass.

**1 × `marquee` — cost reduced, pattern remains.** Two guards cut what it costs,
but it is still an infinite loop, so the rule still fires. Honest result rather
than a suppression entry.

**1 × `dark-glow` — a selection indicator.**
`.assistant-accent-swatch.selected` uses the standard double ring
(`0 0 0 2px panel-bg, 0 0 0 4px accent`) to show which accent is chosen. That
reads as deliberate, not generated, so it stays.

## Two corrections made during this work

**The doctor status rail was not decoration.** The first pass removed
`border-left-width: 3px` from `.doctor-check-row` on the reasoning that status
lives in `.doctor-check-dot`. A test failed and was right: `.doctor-check-row.pass`,
`.warning` and `.critical` each recolour `border-left-color`, so the rail *is* a
severity indicator. It was reduced to 2px instead, and the test now asserts the
rail exists at 1px or 2px and that it is never 3px or more. The same pass
correctly *removed* the rail from `.doctor-connection-panel`, where no status
rule ever recolours it.

**Focus rings were nearly deleted.** Three `dark-glow` findings turned out to be
zero-offset accent rings on `::-webkit-slider-thumb`. Deleting them would have
failed WCAG 2.4.7. They are legitimately kept, and the underlying rule is that a
focus indicator should use `outline` wherever the element allows it — which is
why two of them were rebuilt.

## Still to do

1. **Chat bubble and settings surfaces** — `bubble.css` (12 KB),
   `minicpm-chat.html` (22 KB). Tokens exist but the surfaces still hardcode
   spacing, radii and durations. Converting them is mechanical now.
2. **The `max-height` accordion**, as its own change.
3. **`settings-tab-anim-overrides.js` (84 KB) and
   `settings-animation-overrides-main.js` (54 KB).** 138 KB of animation
   overrides on top of 100 KB of CSS. This is the remaining "assembled" feel, and
   it is a bigger job than anything above: it needs the override model replaced
   with tokens, not restyled.
4. **Token coverage check** — a test that fails on a hardcoded duration, easing
   curve, or sub-11px font size, the way `ui-detect.mjs` fails on a new
   anti-pattern. The detector does not check token usage.