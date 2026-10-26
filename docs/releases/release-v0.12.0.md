## J.A.R.V.I.S. Desk Pet v0.12.0

Official production release of J.A.R.V.I.S. Desk Pet — autonomous on-device AI companion and coding supervisor. Windows x64 is the only supported target.

**What's Included:**

- **Permission Policy Engine:** A tri-state allow / ask / deny matcher, plus a local-sign `finalize` step that keeps your signing key off CI.
- **Reproducible Signing:** A self-signed certificate generated and verified on your own machine, with a verified signed build.
- **New Audio Engine:** A rewritten engine with a generated cue set and mouth sync.
- **Sidecar Capabilities:** A sidecar capability handshake, live assistant configuration, and a Windows-first cleanup.
- **Model Selection:** MiniCPM5-1B and 2B selection during onboarding; the model row was removed from About settings.
- **Document Drafting:** Enriched drafting, a new `open_document` tool, and a reworked settings UI.
- **Chat Bubble Redesign:** A glassmorphism chat bubble and a fixed drawer i18n pass.
- **Dual-Mode Operation:** An online relay and a fully air-gapped offline assistant.
- **One-Liner Install:** A 1-liner installer, an instant debug hotkey, audio feedback, and terminal CLI docs.

**Fixed in this release:**

- **Nine Live Defects:** All nine from plan.md section 3 are closed, and the abandoned voice and PDF work was removed.
- **Correctness:** Companion names normalized; menu jump, math divide-by-zero, document path, and recall bugs fixed.
- **CI Honesty:** `check-syntax.js` now ships, npm scripts are tested for shippability, and three Windows-only tests are guarded so the new gate can pass.

**Performance & Design:**

- **Compositor-Only Motion:** All animation runs on `transform` and `opacity`, so nothing reflows per frame.
- **Legibility:** A new token layer, 19 legibility fixes, and a CI gate that fails on new UI anti-patterns.

**Windows Installation:**

Download \Deskpet-0.12.0-x64.exe below and run the setup wizard.

The installer adds the DeskPet signing certificate to your user trust store.
That is what lets the app verify the signature of a future update. It installs
into your own user account only - no administrator rights are required and
nothing is added machine-wide. Uninstalling removes it again.
