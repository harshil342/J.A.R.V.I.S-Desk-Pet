# Special / Long-Idle State Specs

### 404 error final spec ⭐ locked 2026-04-26 (bold egg slot 1 / error state)

**Current lock**: `experiments/codex-pet/confirmed/states/error-thundercloud-loop-v9-tuned.svg.html` (from WIP `experiments/codex-pet/wip/error-thundercloud-loop-v9-tuned.svg.html`). Key references: `error-thundercloud-no-bolt-v5-rain.svg` (static direction), `error-thundercloud-loop-v6-rain-smooth.svg.html` (rain-line smoothing), `error-thundercloud-loop-v8-stable-color.svg.html` (frozen-material flicker check).

**Core semantics**: the little cloudling isn't struck by lightning — it "crashes into rain". The loop beats: light rain → wrung flat like a towel → squeezed rain pouring harder → slow rebound → rain easing → next round. This is the error / heavy-error / 404 state loop, not a one-shot lightning shot.

**Visual lock**:
- The dark cloud is a **stable diffuse material**, not a glossy highlight ball. Subtle gradient flow inside is fine, but position-shift only — no color/opacity jumps.
- Final cut drops the lightning. Early v2/v4 bolts stole the subject and clashed with Cloudling polish.
- Eyes use the C-variant soft X / crash eyes, keeping the purple-blue gradient + glow — never hollow eyeless.
- Rain lines use the v6 dual-copy smoothing build, not the v7 rain-curtain build. Currently `RAIN_COPIES = 2`: 8 rain lines become 16 paths, staggered phases + wide fades cut flicker.

**Default locked params** (opening `error-thundercloud-loop-v9-tuned.svg.html` defaults):

| Param | Value |
|------|-----|
| total period | 4.0 s |
| wring start phase | 0.53 |
| wring+rebound length | 0.66 |
| squash time share | 0.60 |
| squash curve hardness | 0.95 |
| breathing size / speed | 0.070 / 1.00 |
| towel-wring squash | 0.088 |
| squash lateral spread | 0.058 |
| squash sink | 0.30 |
| rebound overshoot / decay | 0.018 / 0.78 |
| rain-vs-wring offset | 0.05 |
| rain follow-through | 0.66 |
| rain peak curve hardness | 1.00 |
| light / dark diffuse flow | 0.56 / 0.48 |
| diffuse flow-band strength | 0.28 |
| flow shift / speed / soften | 4.1 / 1.25 / 0.50 |
| wring flow speedup feel | 0.55 (position speed only, never color/opacity) |
| rain line count | 8 |
| resting rain opacity / speed / length | 0.58 / 0.62 / 1.30 |
| squeezed brighten / speed-up / stretch | 0.34 / 0.82 / 1.60 |
| rain slant | 0.40 |
| eye darken under wring | 0.08 |

**Implementation notes**:
- `squeezeAt(phase)` is the master driver; `rainBurstAt(phase)` follows the same timeline with only a small `rainDelay` offset. Never let the downpour detach from the squash.
- `flow = t / period * flowSpeed * (1 + squeeze * twistFlowBoost)`, but flow only drives small `cx/cy/r/transform` shifts. `core-stop-a/b`, `hard-shadow`, `blue-glow`, `flowLight/flowDark/flowBand opacity` stay fixed to avoid screen flash in the downpour segment.
- Keep body breathing, but the towel-wring narrowing of vertical distance is the hero visual. Rebound must run slower than the squash; rain eases out with the rebound.
- Keep `statusbar` + sliders for further tuning; when Lulu drops another screenshot of numbers, bake them straight into `DEFAULTS`.

**Process lessons from this round**:
1. 404 "bold" doesn't need lightning. For Cloudling, the character crashing itself beats outside effects stealing the scene.
2. Dark clouds can't wear glossy highlights — they read as balls. Use a stable diffuse material; carry emotion via deformation + rain.
3. Downpour flicker is usually not just the rain lines — synced cloud color / opacity / filter shifts can cause it too. Freeze the material in a control build before tuning further.
4. Looped animation needs story-beat reading: light rain → wring → downpour → slow rebound; never drag the "light rain" stretch too long.
5. The tuning page must export its defaults. Once Lulu confirms via screenshot, freeze them into `DEFAULTS` — never track params by word of mouth.

### cloud-plane orbit final spec ⭐ locked 2026-04-26 (bold egg slot 2 + juggling master-state reuse 2026-04-29)

**Current lock**: `experiments/codex-pet/confirmed/states/cloud-plane-orbit-explore.svg.html` (in wip/ at commit `d0c4ff3`, since moved to confirmed/states/). Support files: `experiments/codex-pet/confirmed/library/paper-plane-v9-cloudling.svg.html` + `experiments/codex-pet/wip/paper-plane-face-debug.svg.html`.

**Dual mapping** (Lulu decided 2026-04-29):
- **Bold egg slot 2** — long-idle / rare visual egg (original slot).
- **juggling master state** — maps to clawd's SubagentStart(1): 1 subagent at work. Paper plane orbiting = 1 free object circling Cloudling, directly reading as "juggling 1 ball". Pairs with conducting (V5, 4 task sparks) as a **1-vs-4+ event-magnitude contrast**: 1 subagent → 1 object (paper plane) orbit, 2+ subagents → multiple task sparks called to place. One SVG serves both mappings, no second build (same reuse pattern as happy burst V6 doubling as attention).

**Core semantics**: the paper plane orbits Cloudling like an asteroid. Not a "plane flies in lower-right → loops once → exits upper-right" shot, but a seamlessly looping orbit egg. The plane pivot always rides the tilted ellipse; Cloudling tenses/relaxes with orbit phase: **far left/right reads as full cloud, near front/back passes read as ball**.

**Default locked params** (opening `cloud-plane-orbit-explore.svg.html` defaults):

| Param | Value |
|------|-----|
| total period | 5.2 s |
| orbit rx / ry | 17.8 / 4.9 |
| orbit tilt | -13deg |
| start angle | 0deg |
| plane palette | `bluePractical` (UI shows Blue) |
| plane size | 0.20 |
| plane global X / Y | 0.0 / 0.0 |
| plane heading correction | 123deg |
| plane pivot X / Y | 19.0 / 19.0 |
| back scale / opacity | 0.71 / 0.65 |
| flip squash | 0.85 |
| far ball / near ball | 8% / 60% |
| target R | 7.0 |
| eye follow | 1.20 |

**Paper-plane geometry source of truth**:
- Reference: `archive/stlabs-2026-04/deliverables/ticket-state-created.canvas.html`, not the `airplane-cheat` old visual with wrong colors and missing faces.
- Vertex names: `N(3,6)`, `TR(34,15.7)`, `MR(24,23)`, `C(20.6,29.5)`, `ML(20.1,25.6)`, `TL(6.7,32.2)`.
- Keep exactly 4 faces: `bodyRight N-MR-C`, `bodyLeft N-ML-C`, `leftWing N-ML-TL`, `rightWing N-TR-MR`.
- **P3 / backFace `ML-C-TL` was judged redundant by Lulu — never add it back.** If the plane looks "AI-ish" or face-count-off again, open `paper-plane-face-debug.svg.html` and align numbered faces first; don't guess inside the animation.

**Orbit / morph drivers**:
- `orbitDepth = sin(theta)` is front/back depth, `orbitProximity = smoothstep(abs(orbitDepth))` is whether the plane passes close.
- `morph = lerp(morphMin, morphMax, orbitProximity)`, currently `morphMin=8%`, `morphMax=60%`.
- Far left/right (`abs(sin(theta)) ≈ 0`) → looser, cloudier cloud; near front/back passes (`abs(sin(theta)) ≈ 1`) → tucked, ball-like cloud.
- Never drive ball-ness from `frontness`; `frontness` owns only z-order, opacity, and trail visibility. The detour here read "distance" as "front/back"; Lulu actually wanted "far left/right vs hugging the body".

**Plane heading / flip**:
- The plane's **center pivot** rides the orbit, nose along the orbit tangent, plus `direction correction = 123deg`. Don't nudge the plane frame alone, or the body center leaves the orbit.
- Fake-3D flip needs two SVG sets, not one rotated SVG. Currently top / under symbol pairs cross-fade on `orbitDepth`, squeezing local scaleY with `rollMinScale=0.85` at flip boundaries.
- Default palette `bluePractical`: blue-purple body + darker folds — more restrained than clash / Happy palettes, more unified with the Cloudling hero look.

**Orbit-line treatment**:
- A full visible orbit line all the way looks dirty; none at all reads as random flying.
- Final fix: **short trail** instead of a full orbit — the trail sticks behind the plane, z-order follows `frontness`, so viewers read the orbit without a dirtied frame.

**Eye requirements**:
- Eyes must hard-lock the plane: tracking uses the Cloudling-center → plane-pivot vector directly, never damped by things like `sidePass`.
- Keep idle eye aliveness: capsule blinks squash via height / y / rx / ry, never plain opacity; `eye-grad` keeps its y1/y2 flow, 5.5s period, amplitude 4.
- Missing any of tracking / blink / gradient flow turns the frame "dead" — Lulu flagged this explicitly before lock.

**Process lessons from this round**:
1. Get the source geometry right first. The plane was first drawn from `airplane-cheat` with missing/extra faces; the fix came from `ticket-state-created.canvas.html`.
2. Debug small complex props in their own numbered-face file first. Lulu saying "P3 is redundant" beats guessing inside the full animation.
3. Visual semantics beat naive math. Ball-ness isn't "more frontal = more ball" but "ball when the plane hugs the body".
4. Asteroid orbit needs pivot + tangent heading + z-order + flip all true together; tuning rotate alone never lands.
5. Cloudling's eyes are the character's lifeline. Tracking, blink, and gradient flow are infrastructure, not optional polish.

### long-idle cloud bush peek final spec ⭐ locked 2026-04-28 (long-idle egg / idle-reading candidate)

**Current lock**: `experiments/codex-pet/confirmed/states/long-idle-cloud-bush-peek-v1.svg.html`. History: `wip/long-idle-cloud-bush-peek-v1.backup-before-v2.svg.html` (pre-script-rewrite) + `wip/long-idle-cloud-bush-peek-v2-before-script-rewrite.svg.html` (last pre-rewrite).

**Trigger**: long-no-activity egg. Maps to clawd's idle-reading slot ("Idle (random) → Reading / patrol"), but Cloudling re-reads it as "peeking from behind clouds" pet comedy instead of reading / patrolling. Looped, 6.2s story + 1.0s gap = 7.2s seamless cycle.

**Core semantics**: two warm clouds drift in from the left hiding the cloudling → it ducks behind → "whoosh" half-face peek from upper left + a slow question mark over its head → retract → "whoosh" peek again from upper right, scanning around with two blinks → retract → warm clouds part left/right revealing the full cloudling → back to idle. All comedy comes from cartoon two-sided peeks + the head-top question mark, **never ball-morph / bounce / particles** (deliberately distinct grammar from other states).

**New visual asset — two-layer warm-cloud barrier**:
- Back warm cloud: linear gradient `#FFE3CE → #FFD3B3 → #FFB89A` (warm orange), stroke `#E69E78` 0.55w, soft glow + warm-shadow
- Front warm cloud: linear gradient `#FFF1CE → #FCE3B0 → #F5C374` (warm gold, brighter/yellower than back), stroke `#D9A04F` 0.55w
- Each with highlight ellipse + warm-hi radial gradient; warm-cloud paths are standalone `warm-cloud-path` (totally separate from the cloudling body cloud-shape)
- Barrier rest: back at `(12 - barrierGap, barrierY)`, front at `(12 + barrierGap, barrierY)`, defaults barrierGap=3.0 / barrierY=16.5 (below the cloudling)
- Entry start: back at `(3, barrierY)`, front at `(4, barrierY)` — starts stay on-canvas to avoid hard pet-window crop; back moves first, front follows 0.18s later

**9-beat story timeline** (6.2s, defaults):

| # | Beat | Range | Length | Main action |
|---|------|------|------|---------|
| 1 | warm-enter | 0.00 - 1.10s | 1.10s | warm clouds drift in from left and close, cloudling eyes track clouds left→center, slight hideTy settle at end |
| 2 | crouch-left | 1.10 - 1.45s | 0.35s | pre-left-peek squash crouch (sx 1.085 / sy 0.885 / rot -1.5°) |
| 3 | peek-left | 1.45 - 2.35s | 0.90s | whoosh peek upper-left (tx -8.5 / ty -4.5 / rot -12.5° + jitter) + curious wide eyes + scan + head-top question mark |
| 4 | retract-1 | 2.35 - 2.75s | 0.40s | retract, easeIn accelerating tail (0-0.65 fast pull, 0.65-1 fully hidden) |
| 5 | crouch-right | 2.75 - 3.10s | 0.35s | pre-right-peek crouch, slightly softer than left |
| 6 | peek-right | 3.10 - 4.65s | 1.55s | upper-right peek + held beat, left→up→right→2 blinks + body bodyCheck sway |
| 7 | retract-2 | 4.65 - 5.05s | 0.40s | retract, same rhythm as retract-1 |
| 8 | cloud-fade | 5.05 - 5.85s | 0.80s | warm clouds fade first then part back-left / front-right (driftDistance 42, drift from q=0.18, fade q=0.08-0.48), cloudling revealed |
| 9 | recover | 5.85 - 6.20s | 0.35s | eyes sweep (left→right→center) back to idle, breathing resumes |

**Default locked params DEFAULTS** (opening defaults are the lock):

| Param | Value | Notes |
|------|----|----|
| total duration | 6.2 s | 9 beats excl. gap |
| loop gap | 1.0 s | gap returns to idle (static + micro-breathing) |
| body breathing bodyBreath | 0.030 | slightly over idle, reads better on long-idle |
| outline micro-rotate rimRotate | 1.5° | whole-cloudShape feather sway |
| warm-cloud entry speed warmSpeed | 1.00 | warm-enter time-scale factor |
| warm-cloud opacity peak warmOp | 0.95 | under 1.0 keeps a sheer feel |
| barrier lateral offset barrierGap | 3.0 | front/back horizontal stagger |
| barrier height barrierY | 16.5 | viewBox center 12, 16.5 = below the cloudling |
| hide settle hideTy | 1.0 | cloudling sink when hidden |
| peek lift peekTy | -4.5 | peek rise |
| peek lateral peekTx | 8.5 | left peek -8.5 / right peek +8.5 |
| peek outline lean peekLean | 12.5° | body lean while peeking |
| peek outline tuck peekScaleDip | 0.075 | whole-body micro-tuck while peeking |
| curious widen eyeWiden | 0.19 | eye-width multiplier after peek |
| right-peek eye shift rightEyeOffsetX | 1.40 | extra right bias on right peek (pushes curious scan) |
| right-peek eye lift rightEyeLift | 0.30 | same, slight lift |
| question size qSize | 1.00 | head-top question scale |
| question X offset qOffsetX | -6.0 | left peek parks it upper-left (cloudling center + offset) |
| question head clearance qOffsetY | -10.0 | farther above the head, leaves breathing room |

**Question-mark implementation**:
- Path `M -1.4 -1.6 Q -1.4 -3.0 0 -3.0 Q 1.4 -3.0 1.4 -1.6 Q 1.4 -0.6 0.4 0.0 Q -0.1 0.35 -0.1 1.3` + bottom dot `circle r=0.44`
- Stroke uses the `eye-grad` purple-blue gradient (same as eyes), q-glow filter for soft light
- **Shows in peek-left only** — right peek skips it (curiosity already lands; repeating kills the joke)
- No bounce, smoother in/out (qOp eases 0.22 → 0.68, fades 0.78-0.98), drift 1.05 → -0.55 (light float upward)

**Key decision lessons** (written down so the next similar state doesn't repeat the grind):

1. **First scene-element intro** — warm clouds aren't particles / dress-up / props but "standalone scene elements". CLAUDE.md §2's change-grammar table had no such class; this is its first use. Later "surrounded by a scene / through a hole / over a fence" states can follow this pattern: standalone symbol + own transform + z-relationship to the body
2. **Two staggered layers make the "bush"** — one warm layer can't read "hidden". Two layers sandwich cloudling in front/back z-order so viewers read "popping out from behind clouds", not "passing through a cloud"
3. **Left/right peek beats must be asymmetric** — peek-left 0.90s vs peek-right 1.55s; the second must run longer. Symmetry kills the joke (mechanical repeat); asymmetry reads "first a probe, then confirming". Lulu confirmed this rhythm in tuning
4. **One question mark only** — first pop is "huh?", a second becomes "???", flipping cute into over-confused. Exclusive to peek-left; right peek carries curiosity with scanning glances only
5. **The gap is mandatory** — 7.2s is a long loop; the 1.0s gap returns to pure idle so viewers never feel "this cloud never stops performing". The gap is what makes long-idle eggs, unlike idle / typing loops that run continuously
6. **bodyCheck is peek-right's core micro-motion** — the extra 1.55s hold can't sit static; `bodyCheck = sin × wave × 0.16` adds a feather left/right sway (like a real pet's body follow-through while looking around); without it the hold reads dead

**Clawd entry**: SVG + JS driven (Cloudling all-SVG route, decided 2026-04-26). long-idle loops, 6.2s + 1.0s gap = 7.2s seamless cycle. Hook into clawd under long-idle trigger conditions (Lulu to decide: random after 60s vs only after 30min).
