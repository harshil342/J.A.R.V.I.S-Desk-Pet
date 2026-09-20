# Core State Specs

### idle (eye-follow) final spec ⭐ locked 2026-04-25

**Visuals**: soft-rubber white cloud + purple-blue bean eyes (V2 final). At render, viewBox expands to `-12 -12 48 48` (core geometry stays in [0,24], doubled margin around it leaves room for far-range mouse behavior).

**Motion language**: whole-SVG `transform="rotate scale"` (no discrete paths, no noise). **Eyes lead, body responds**:
- Mouse inside canvas: eyes track the mouse 1:1 at constant speed (no easing, no "huh?")
- Cloud follows eyes: lagged rotate follow (eye.x drives cloud rotate, with delay)
- **Farther mouse from cloudling → bigger overall scale up** (actively "leans in to look", a lively little personality)
- Mouse leaves canvas: eyes + cloud + scale all ease back to center

**Locked params** (opening idle-follow.svg.html defaults):

| Param | Value |
|------|-----|
| breathing scale lo / hi | 0.96 / 1.00 |
| breathing period | 5.0 s |
| cloud rotate at full eye deflect | 20° |
| cloud eye-follow lag (lerp/frame) | 0.20 |
| eye max offset | 0.50 units |
| eye recenter time | 0.45 s |
| flow gradient period | 5.5 s |
| flow gradient amplitude | 4 units |
| blink interval | 6.0 s ±20% |
| blink duration | 160 ms |
| distance-scale trigger range | 6 units |
| distance-scale saturate range | 20 units |
| distance-scale max | 1.15× |
| distance-scale easing | 0.50 s |
| eye glass stroke width | 0.08 |
| eye glass stroke color / opacity | `#ffffff` / 0.70 |
| cloud drop shadow opacity | 0.18 (V3 darkened from 0.22 to balance the white outline) |

**Clawd entry**: SVG + JS driven (Cloudling all-SVG route, decided 2026-04-26). idle tracks the mouse live = SVG + JS is the natural fit, now the standard for all Cloudling states.

**Personality**: "lively" = 4-8s slow periods + 5-10% subtle amplitudes + eyes lead + distance-driven scale leaning in (like being drawn to it). Not jitter, not exaggeration — exactly the right liveliness.

**Key decision lessons** (written down so the next state doesn't repeat them):
1. **idle is not a Canvas case** — simple rotate + scale + eye follow, SVG beats Canvas outright. CLAUDE.md already said idle goes SVG, but the code picked Canvas anyway (1 detour)
2. **4 direction detours** — rubber squash / float / path-normal noise (Lulu: "ghost") → the fix is whole-body rotate + scale (matches the official codex video). Checking official assets before writing demos saves 4 detours
3. **Proper use of the STlabs canvas toolchain** — great as a **tuning tool** (Lulu tuned the locked numbers on a Canvas demo, then migrated to SVG). **Tuning ≠ final deliverable**, that's the split

### typing final spec ⭐ polished + locked 2026-04-27

**Current polished lock (2026-04-27)**: `experiments/codex-pet/confirmed/states/typing-original-polish-v2.svg.html`.

**2026-04-27 polish verdict + 2026-05-02 touch-up**: keep the original 190° sticky rotation, code eyes, angular-velocity linkage, and mutual eye exclusion. Don't rebuild typing as fixed glyph pairs / little-beat versions; `typing-code-eyes-v2.svg.html` was rejected by Lulu for being too tidy and killing the "really typing" feel. On 2026-05-02 bare random tokens became A/B-tier pseudo-random combos: tier-A may rest, tier-B only flashes during fast-motion segments, and stop / near-stop windows force back to tier-A.

**Visuals**: soft-rubber white cloud + purple-blue code-symbol eyes (outline build, same stroke + linecap=round + gradient as happy / eye-shape-library).

**Motion language**: **sticky rotation + angular-velocity-linked glyph switching + deep breathing**:
- Whole cloud rotates counter-clockwise in a 4-phase loop (spin out → stuck at top → spring back → stuck at home)
- Eyes scale along, never rotate (glyphs stay upright)
- Glyph switching links to outer-ring angular velocity — **tokens only swap while spinning fast, freeze during sticks** (the "typing hit a hitch" feel)

**Token pool** (8): `> < _ : = + / \` (`` `*` `` `{` `}` rejected, see lessons). **Mutual exclusion + combo tiers**: the two eyes are never identical at any moment, like real typing; on switch, filter candidates by the left/right combo pool, then deterministic pseudo-random picks the next token.

**Combo tiers**:
- Tier A (may appear often and rest): `<>`, `<=`, `>=`, `=>`, `=<`, `>_`, `<_`, `/_`, `_\`, `=/`, `=\`.
- Tier B (only flashes during fast motion): `:>`, `:/`, `:\`, `:_`, `+_`, `/+`, `\+`.
- Tier C (deleted): `:=`, `:+`, `+:`, `++`, `::`, `><`, `/\`, `\/`.

**Stop rules**: last 20% of spin-out, last 25% of spring-back, top stick, and home stick allow tier-A only; if the current combo isn't tier-A on entering these windows, cut back to tier-A at once with the same 90ms soften.

**Locked params** (opening `typing-original-polish-v2.svg.html` defaults; original master rhythm kept):

| Param | Value |
|------|-----|
| glyph size EYE_SIZE | **1.40×** (uniform scale about glyph center, stroke follows) |
| stroke weight (single: > < _) | 1.25 |
| stroke weight (multi-line: = +) | 0.9 |
| stroke color | `url(#eye-grad)` (links with flow gradient) |
| `:` render mode | fill (dots), radius 0.78, spacing ±1.05 |
| total rotation | 190° |
| rotation direction | -1 (counter-clockwise) |
| spin-out T_SPIN | 1.40 s |
| overshoot | 8° |
| top stick T_HOLD_FAR | 0.85 s |
| spring-back T_BACK | 1.00 s |
| home stick T_HOLD_HOME | 1.20 s |
| **total period** | **4.45 s** |
| breathing scale lo / hi | 0.94 / 1.05 |
| breathing period | 1.60 s |
| switch threshold (left/right eye) | 96° / 96° (accumulated rotation) |
| switch jitter | ±25 %, deterministic pseudo-random |
| flow gradient period | 3.0 s |
| flow gradient amplitude | 4 units |

**Key decision lessons** (written down so the next state doesn't repeat them):
1. **v3 failed on filled + inner shadow** — glyphs read emoji-chunky, clashing with happy's arc-eye stroke round-cap style. **Fix: stroke-only outlines + linecap=round** (same build as the code symbols locked in eye-shape-library)
2. **`*` asterisk rejected** — 4 crossing strokes read noisy even at stroke 0.7, inconsistent with the other spare tokens (same logic as the heart/star rejection)
3. **`{` `}` braces rejected** — tried and Lulu said "too jokey, loses the polish". Braces read friendlier/cartoonier than `>` `<` `=`, clashing with Cloudling's Apple-grade polish. `/` `\` single slashes don't have this problem — spare, no mood. **Lesson: judge tokens by visual semantics vs overall style, not just drawable geometry**
4. **Glyph size needs its own knob** — glyphs read small inside the 24×24 viewBox, 1.20× still short, 1.40× right. Capped by the 6-unit eye spacing, 1.40× is near the limit
5. **Angular-velocity-linked switching ≠ constant-rate switching** — switches integrate rotation angle and freeze during sticks, the "typing hit a hitch" feel. 96°/switch means ~1-2 swaps per revolution, exactly busy enough
6. **Mutual-exclusion rule** — when swapping a token, exclude both your own last frame + the other eye's current token. Identical eyes read dead/symmetric; exclusion reads lively and more "really typing" (Lulu's words).
7. **Pure-random combos hit ugly pairs** — 2026-05-02 touch-up: `pickToken(side, prev, otherEye, aOnly)` first finds candidates in the A/B combo pools that form a legal pair with the other eye, then pseudo-random picks. Keeps the original liveliness but filters `:=`, `:+`, `++`, `><`, `/\`, `\/` etc.; stop / near-stop windows use `settleToA()` so resting frames only ever show tier-A.

**Clawd entry**: SVG + JS driven (Cloudling all-SVG route, decided 2026-04-26). Prefer `confirmed/states/typing-original-polish-v2.svg.html` for integration, no frame export needed. typing loops, 4.45s seamless cycle.

### thinking final spec ⭐ polished + locked 2026-04-27

**Current polished lock (2026-04-27)**: `experiments/codex-pet/confirmed/states/thinking-lens-code-v2.svg.html`.

**History baseline**: `experiments/codex-pet/wip/thinking.svg.html` / `experiments/codex-pet/confirmed/states/thinking.svg.html` (v5).

**Visuals / motion language**: keeps the "code running inside a magnifier" idea, but the 2026-04-27 polish swaps the big gray ring + big tokens for a light glass lens + in-lens code scan flow. The lens still sweeps an arc between the eyes, the cloud tilts slightly while eyes stay vertical; on focus 3 small glyph/scan lines appear in the lens, and the ending returns to capsule with 2 blinks.

**2026-04-27 removals**:
- Removed center pulse circle: reads as a mystery ball / anchor.
- Removed transit center curve: reads as a lens defect.
- Removed blink light-cover block: shows as a rectangle patch inside the transparent lens; replaced with clipped capsule + closed-eye line.

**Locked params**: `thinking-lens-code-v2.svg.html` default slider values are the truth. Current key numbers: 6.0s total, focus/slide ratio 3.00, rotate amplitude 7.0°, scale hi/lo 1.10 / 1.00, lens arc height 1.15, float 0.12 / 3.1s, code scan 0.78s, accel strength 1.65, capsule blink 0.22s ×2.

**State log**: Lulu approved the thinking concept on 2026-04-26; approved `thinking-lens-code-v2` as prettier on 2026-04-27. Don't drop the magnifier/code-flow later — keep polishing inside this language.

### notification final spec ⭐ polished + locked 2026-04-27

**Current polished lock (2026-04-27)**: `experiments/codex-pet/confirmed/states/notification-alert-polish-v2.svg.html`.

**History baseline**: `experiments/codex-pet/wip/notification-pulse-explore.svg.html` / `experiments/codex-pet/confirmed/states/notification-pulse-explore.svg.html`. Old backup: `experiments/codex-pet/wip/notification-pulse-v1-rings-backup.svg.html`.

**Core semantics**: notification is "a task arrived and the little cloudling got rattled by the ping" — not a radar target / magic circle / head-top decor. Lulu confirmed notification must alert hard and read loud; the problem was never "too noisy" but a visual language reading like external UI bubbles or radar broadcast.

**Locked rhythm**:
- Total `2600ms`, A/B/C = `0.22 / 0.50 / 0.28`.
- Segment A warm-color warning; segment B 7Hz hard shake + double-ping shatter arcs; segment C afterglow decay.
- Keep this hard alert + shake untouched. Only micro-tuning of visual intensity and local params from here.

**Visual lock**:
- Pulses change from broadcast-style long left/right arcs to **warm shatter arcs** (`path` arc, round linecap, soft glow). The body must stay the first read; warm color only carries alert intensity.
- No head-top notification badge / exclamation bubble / ball. Lulu's explicit feedback: "head bubbles look bad"; body shake + warm arcs are already loud enough.
- Segment B eyes stay Cloudling capsule bean eyes, visibly widened but never cut to `><`.
- Keep the cloud-edge `rim flash` — it reads more like Cloudling itself getting hit by the notification than any badge does.

**Current default key params**:

| Param | Value |
|------|-----|
| total period | 2600 ms |
| A / B share | 0.22 / 0.50 |
| shake amplitude / freq | 14deg / 7Hz |
| hop / squash | 0.46 / 0.040 |
| micro / macro scale | 0.034 / 0.060 |
| A / B arc life | 760ms / 520ms |
| arc end scale | 1.54 |
| arc stroke / glow | 0.76 / 0.36 |
| arc birth radius | 8.9 |
| eye widen | 0.22 |

**Lessons from this round**:
1. "Right rhythm" ≠ right visuals. Keep the timeline, redo the visual grammar alone — cheapest iteration.
2. Notification semantics can stand on Cloudling body shake + warm arcs; no head-top badge needed. Don't casually stick props onto Cloudling's outline.
3. Library eyes like `><` aren't generic excitement symbols; inside notification they read as buttons/glyphs and steal Cloudling's polish.

### carrying (cloud-eating) final spec ⭐ locked 2026-04-28

**Current lock (2026-04-28)**: `experiments/codex-pet/confirmed/states/carrying-eat-cloud-v5.svg.html`.

**Core semantics**: Cloudling has no mouth, so "eating clouds" can't use a mouth line / bite / suction lines. The fix is soft-body edge absorption: a small food cloud touches the flank, Cloudling's flank bulges out a temporary puff to wrap it, the food cloud assimilates into body color, then the bulge releases into a smile + duang rebound.

**Motion language**:
- Mirrored two-bite loop, `4.5s` total: right food cloud enters from upper right first, left food cloud from lower left second.
- Food clouds differ in size: right cloud factor `1.08`, left `0.82`; both use the white-blue food-cloud gradient, avoiding the v3 gray-hard-chunk feel.
- Food cloud rides the background layer before contact; cuts to the body surface layer after contact, stays visible.
- The body never dents inward to hide the small cloud; the contact flank bulges outward to wrap. Keep the wrap window short: v5 `T_SWALLOW_END = 1.24` — wrapped ~1.08s, visibly melting at 1.16s, melted by 1.24s into a smile, rebound from 1.30s.
- The spring must fire only after full assimilation, never on contact — or it reads as "bumped into something".

**Locked params**:

| Param | Value |
|------|-----|
| total `T_CYCLE` | 4.5 s |
| food enter / liftoff / contact / melted / recenter | 0.00 / 0.55 / 1.00 / 1.24 / 1.95 s |
| spring amp / freq / damping / duration | 0.16 / 4.0Hz / 5.0 / 0.65s |
| food start X / edge X / Y | 24 / 13.5 / 12 |
| right / left cloud factor | 1.08 / 0.82 |
| wrap strength | 0.90 |
| bulge distance / assimilate approach | 1.55 / 1.2 |
| assimilate X-squash / Y-bulge | 0.64 / 1.16 |
| intercept X / diagonal Y link | 5.0 / on |

**Key decision lessons**:
1. **Denting inward = hiding, not eating** — v2 dented the edge inward with the small cloud shrinking into the hole, reading as hiding. "Wrap-eating" must read as a flank bulging outward into a temporary puff.
2. **Food cloud must not vanish into the background layer on contact** — v1 kept it behind the body and it got covered at frames 0.95-1.08s, leaving only a sudden smile/spring. Cut to the surface layer on contact.
3. **Color close to the body but still separable** — grayish food reads foreign, full white vanishes. v4/v5 white-blue gradient + faint rim is the current balance.
4. **Keep the wrapped hold short** — lingering reads stuck. v5 moved melt-done from 1.34s to 1.24s, keeping a readable beat without drag.
