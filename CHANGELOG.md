# Changelog

All notable changes to Deskpet. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

The app and the sidecar ship as one product and carry the same version.

## [Unreleased]

Work in progress. See `plan.md` for the production-hardening plan and
`scripts/release.mjs changelog` to fold these into a version.

## [0.12.0] - 2026-09-30

### Added

- port the matcher and tri-state policy; add local-sign finalize (permissions)
- reproducible self-signed cert, verified signing, signed build (signing)
- new engine, generated cue set, mouth sync (plan.md D5/D6) (audio)
- jarvis sidecar capabilities, live assistant config, windows-first cleanup, english-only repo
- add MiniCPM5-1B and 2B model selection and remove model row in about settings (onboarding)
- enrich document drafting, add open_document tool, and elevate settings UI
- redesign chat bubble with UI/UX Pro Max glassmorphism and fix drawer i18n (ui)
- implement dual-mode online relay and air-gapped offline assistant (butler)
- add 1-liner installer, instant debug hotkey, audio feedback, and terminal CLI docs

### Fixed

- ship check-syntax.js and test that npm scripts are shippable (ci)
- guard three Windows-only tests so the new gate can pass (ci)
- close the nine live defects in plan.md section 3, delete voice + pdf work
- normalize companion names, fix menu jump, math div-by-zero, doc path and recall
- polish bubble UI, compound math, and local memory routing (chat)

### Performance

- compositor-only motion, and an objective UI baseline (ui)

### Changed

- token layer, 19 legibility fixes, and a CI gate on UI findings


## [0.11.0] - 2026-08-28

- Jarvis sidecar capabilities, live assistant config, Windows-first cleanup
- Document drafting, `open_document` tool, elevated settings UI
- Chat bubble redesign with glassmorphism
- Durable reminders, search fallback, pronoun follow-ups
- Windows packaging, theme packs, tool calling
