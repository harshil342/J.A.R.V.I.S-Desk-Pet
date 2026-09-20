---
name: custom-lora-persona
description: >-
  Build a custom persona / skin for the MiniCPM-Desk-Pet: fine-tune a LoRA, convert to GGUF,
  upload and switch in the App. Covers the full "train → GGUF → upload → enable" chain.
  Use when the user wants a custom desktop-pet persona, asks
  "做一个自己的桌宠人格 / 角色", "训练 / 上传自定义 LoRA", "把我的 LoRA 用到桌宠上",
  "convert LoRA to GGUF for the pet", or hits errors uploading a .gguf adapter in Settings.
---

# Build a custom LoRA persona for the pet

MiniCPM-Desk-Pet accepts your own **LoRA adapter** to reskin how the pet talks (the built-in cat-girl came this way). This Skill chains the whole flow:

```
  Fine-tune LoRA        Convert to GGUF           In the App
  (any framework) ──► (llama.cpp convert) ──► upload + enable
  PEFT adapter        adapter.gguf            Settings → MiniCPM
```

> **Key fact**: the App backend is `llama-server`, which only takes **GGUF LoRA adapters** (loaded via `--lora`).
> Most fine-tune frameworks emit PEFT (`adapter_model.safetensors`), which **cannot be uploaded directly** — it needs a
> `safetensors → GGUF` conversion first. This is where most people get stuck.

## Prerequisite: train against MiniCPM5-1B

The pet runs MiniCPM5-1B (GGUF). Your LoRA **must be trained on the same base model**, or the adapter is garbage on top.

- Base model: `openbmb/MiniCPM5-1B` (fp16 HF build, for training).
- Training data: messages-format JSONL, e.g.
  `[{"messages":[{"role":"user","content":"..."},{"role":"assistant","content":"..."}]}]`.

## Full steps

### 1. Train a LoRA (in the OpenBMB/MiniCPM repo)

Training Skills live upstream in the [`OpenBMB/MiniCPM`](https://github.com/OpenBMB/MiniCPM) repo under `skills/`, **not in this repo**. Pick by hardware:

| Your case | Which Skill |
| --- | --- |
| First fine-tune, want easiest | `minicpm5-finetune-llamafactory` |
| Single consumer GPU / tight VRAM (≤24GB) | `minicpm5-finetune-unsloth` (`load_in_4bit=True` for QLoRA) |
| Undecided / want all options | `minicpm5-finetune` (router overview) |

Output is a dir with `adapter_config.json` + `adapter_model.safetensors`.

### 2. Convert to a GGUF adapter (the only form the pet accepts)

Also upstream, dedicated Skill: **`minicpm5-finetune-gguf-lora`**. Core command:

```bash
# 在 llama.cpp 仓库目录下
python convert_lora_to_gguf.py /path/to/你的adapter目录 \
    --base openbmb/MiniCPM5-1B \
    --outtype f16 \
    --outfile ~/my-pet-persona.gguf
```

> 🔑 **Biggest gotcha**: `adapter_config.json` often records `base_model_name_or_path` as an **absolute path on the training machine**,
> which doesn't exist on yours. So **always pass `--base openbmb/MiniCPM5-1B`** (or `--base-model-id openbmb/MiniCPM5-1B`)
> to override it. `--base` only needs the base model config, not full weights.

The resulting `.gguf` is small (an r=16 adapter is tens of MB) — that's the file to upload.

> The built-in cat-girl adapter under `adapters/` in this repo — `adapter_model.f16.gguf` next to
> `adapter_model.safetensors` — was converted exactly this way; use it as reference.

### 3. Upload in the App

1. Open **Settings → 🐾 MiniCPM**, find the Adapter (LoRA) section.
2. Hit **Upload**, pick your `.gguf` file. The App copies it into `<userData>/adapters/uploads/` and registers it.
3. Fill in a **display name** and **aliases** (comma-separated). Aliases drive voice/chat switching, e.g. alias "little fox" lets "switch to little fox" work.
4. **Select** it in the adapter list to enable — the sidecar restarts `llama-server` with your `--lora`.

App validation (rejected if unmet):

- **Single `.gguf` file only** (the one from step 2 — not `.safetensors`, not a merged full model).
- The adapter stacks on the App's bundled MiniCPM5-1B GGUF base, so it **must be trained for MiniCPM5-1B**.

### 4. Verify

Switch to your adapter and say a few lines — check whether persona/tone changed.

- **Changed** → success.
- **Identical to before** → adapter not actually live: usually step-2 `--base` mismatch, or you uploaded the wrong file.
- To confirm the GGUF is a legal LoRA: `python -c "import gguf; r=gguf.GGUFReader('~/my-pet-persona.gguf'); print(len(r.tensors),'tensors')"`, tensor count should be > 0.

## FAQ

### Upload says "must be a .gguf file"
You picked `.safetensors` (raw PEFT output). Go back to step 2 and convert to GGUF first.

### Uploaded but the pet acts the same
- LoRA is only a **style bias**; with too little weight the effect is subtle — add data/epochs or raise `lora_alpha`.
- Built-in personas (e.g. cat-girl) pair LoRA with a **system prompt**; custom uploads rely mostly on the LoRA style itself. For a stronger persona, feed the target tone/catchphrases generously into training data.

### Convert fails with `can't load base model config` / `FileNotFoundError`
Same gotcha as step 2: the base path in `adapter_config.json` doesn't exist on your machine. Override with `--base openbmb/MiniCPM5-1B`.

### Remove an uploaded adapter
Delete it in the same adapter list (only your own uploads; built-ins can't be deleted). If it's the active one, the App unloads it sidecar-side before deleting the file.

### Where did the adapter go / where is the file
Uploads live in `<userData>/adapters/uploads/` (`adapters/` in dev mode). The sidecar scans `*.gguf` there on start.

## What this Skill does NOT cover

- **How to train** (data prep, hyperparams, running training) → upstream `minicpm5-finetune*` series.
- **Full conversion details** → upstream `minicpm5-finetune-gguf-lora`.
- **Deploying the pet from source** → `deploy-minicpm-pet` in this repo.

This Skill chains those into one user-facing "build a pet persona" thread.

## References

- Upstream train/convert Skills: [`OpenBMB/MiniCPM`](https://github.com/OpenBMB/MiniCPM) repo `skills/` (`minicpm5-finetune`, `minicpm5-finetune-gguf-lora`).
- In-App entry: **Settings → 🐾 MiniCPM → Adapter (LoRA)**.
- The README Persona Adapters section has a short version.
