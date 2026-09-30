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
| 6 | `clipped-overflow-container` | Content clipped by a fixed-height container. |
| 4 | `tiny-text` | 11px body text. |
| 3 | `dark-glow` | Chromatic `box-shadow` halos (`#38bdf8`, `#373ed8`) — the default "cool" look of generated UIs. |
| 2 | `side-tab` | 3px `border-left` — called out as *the* most recognisable AI-UI tell. |
| 1 | `marquee` | Infinite skeleton sheen. |
| 1 | `layout-transition` | `max-height` on the collapsible body (see below). |

**37 total on `7411f43` → 32 after the performance pass below.**

## Done: performance first

Started with the findings that cost frames, because low resource use is the
primary goal — not because they were the most visible.

**Progress bars: `width` → `scaleX`.** Three of them (doctor connection,
onboarding download, chat update) animated `width`, which relayouts on every
frame. They now animate a transform, which runs entirely on the compositor.
Each needed a matching JS change, since all three were setting
`style.width = X%`.

**Removed `will-change: max-height, opacity, transform`** from the collapsible
body. `max-height` cannot composite, so the browser was permanently holding
resources for an animation that could never leave the main thread.

**Layout properties off transitions.** `padding` off the group header and the
collapsible body, `margin-top` off the doctor body. Nobody can see an
un-animated padding change on a settings row; everybody can feel the reflow.

**The infinite sheen is now conditional.** It ran forever whenever the
diagnostics modal was open — for a panel that usually just displays a finished
result. It now honours `prefers-reduced-motion` and stops entirely once loading
finishes.

## Deliberately left

**One `layout-transition`: `max-height` on the collapsible body.** The
`grid-template-rows: 0fr → 1fr` technique used by `.doctor-agent-body` is the
correct modern fix and is already in this codebase, so it is tempting to convert.
It needs the JS to toggle a class instead of setting the measured
`--collapsible-body-height`, and that touches the measured-height,
scroll-anchor-preservation and `inert`/`aria-hidden` logic — all of which have
their own tests. Worth doing as its own change; not worth doing inside a
performance pass.

**`marquee` still reports.** The two guards above cut its cost, but the pattern
is still an infinite loop, so the rule still fires. Honest result rather than a
suppression entry.

## Next stages

1. **Type and legibility** — the 19 text findings. Raise the functional floor to
   11px and body to 14px via tokens, not per-rule overrides. This is the single
   biggest legibility win and it touches the most lines.
2. **Kill the AI tells** — 3 chromatic glows and 2 side-tab borders. Replace with
   neutral elevation and a subtler accent. Small diff, large perceived-quality
   change.
3. **Establish tokens** — `settings.css` is 100 KB of hand-rolled rules and
   there are 138 KB of `settings-tab-anim-overrides` JS on top. That accretion
   *is* the generic look. Extract a token layer (`--space-*`, `--radius-*`,
   `--elevation-*`, `--dur-*`, `--ease-*`) and let components consume it.
   This is the change that makes everything after it cheaper.
4. **Chat bubble and settings surfaces** — `bubble.css` (12 KB),
   `minicpm-chat.html` (22 KB). Apply stages 1–3 there.
5. **Measure.** Re-run the detector after each stage and keep the count moving
   down. Wire it into CI so it cannot regress.