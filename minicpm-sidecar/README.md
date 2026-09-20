# minicpm-sidecar

On-device inference sidecar for the MiniCPM desk pet. GGUF inference via `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp), wrapped in a slim Python gateway (FastAPI) that keeps the existing HTTP/SSE contract with Electron unchanged.

> This directory replaces the old `minicpm-pet-bridge/` and `minicpm-pet-bridge-uv/`. Those are deprecated, kept for history only.

## Design

```
Electron (clawd-on-desk)
   │ HTTP/SSE :18765
   ▼
gateway (FastAPI, no torch)               <- gateway/ in this dir
   │ HTTP :18766 OpenAI-compat (stream)
   ▼
llama-server (native C++)                 <- official llama.cpp release build
   │
   ▼
*.gguf  in <userData>/models/
```

Why two stages:

- `llama-server` is already a mature OpenAI-compatible server upstream, but its protocol is OpenAI SSE (`event: data\n` style, `choices[].delta.content`), incompatible with the existing custom `event: start|delta|think|end|error` SSE in the Electron UI.
- The gateway handles protocol translation, `<think>` block splitting, pet-state pushes, the `/api/update-apply` flow, model/adapter directory management, and other interfaces Electron already depends on.

Both binaries are packaged under `clawd-on-desk` `resources/sidecar-bin/`:

```
sidecar-bin/
  minicpm-sidecar(.exe)   <- gateway, via PyInstaller
  llama-server(.exe)      <- official llama.cpp release build
  <runtime libs ...>
```

Electron only spawns `minicpm-sidecar`; the gateway forks `llama-server` itself.

## Layout

```
<repo-root>/
  llama.cpp/                  # git submodule (ggml-org/llama.cpp @ b9371, source tracing / manual fallback)

minicpm-sidecar/
  gateway/                  # FastAPI gateway source
    __main__.py             # entry: python -m gateway --model ... --port ...
    server.py               # FastAPI app + /api/* routes
    llama_client.py         # llama-server child-process mgmt + OpenAI streaming
    think_filter.py         # <think>...</think> splitting (ported from old bridge)
    clawd_state.py          # pet-state push (ported from old bridge)
    updater.py              # GGUF model download/verify
    log_setup.py            # cross-platform log dir resolution
  scripts/
    fetch-llama-release.sh  # download official llama.cpp release (mac/linux)
    fetch-llama-release.ps1 # download official llama.cpp release (windows)
    build-llama.sh          # manual fallback: local cmake build (mac/linux)
    build-llama.ps1         # manual fallback: local cmake build (windows)
    build-gateway.sh        # PyInstaller single-file gateway
    build-gateway.ps1       # Windows PyInstaller single-file gateway
    build-all.sh            # one-shot: fetch official llama-server + build gateway
    run-dev.sh              # local dev: start gateway directly, it pulls llama-server
  build/
    gateway.spec            # PyInstaller spec (for gateway)
  pyproject.toml            # uv project, only fastapi/uvicorn/httpx/huggingface_hub
  .python-version
```

## Dev start

```bash
# 1) First time: download llama-server from the official llama.cpp release
./scripts/fetch-llama-release.sh

# 2) Install gateway deps (slim, a few dozen MB)
uv sync

# 3) Start the gateway (it pulls up llama-server automatically)
./scripts/run-dev.sh --model /path/to/minicpm5.gguf
```

Or just run `./go.sh` from the repo root (already migrated to the new flow).

## Production build

```bash
./scripts/build-all.sh
# Artifacts:
#   bin/<os>-<arch>/minicpm-sidecar(.exe)
#   bin/<os>-<arch>/llama-server(.exe)
# clawd-on-desk/package.json extraResources then packs them into the installer
```

## Official llama.cpp Release

CI and default local builds no longer compile `llama.cpp`; they download the official
[`ggml-org/llama.cpp` b9371 release](https://github.com/ggml-org/llama.cpp/releases/tag/b9371)
`llama-server` binary directly. MiniCPM5 tokenizer support is already upstream via
[PR #23384](https://github.com/ggml-org/llama.cpp/pull/23384).

`llama.cpp` stays pinned as a top-level git submodule at the same release for source tracing and
extreme-case manual local fallback; it is no longer part of the CI packaging path.

To upgrade to a newer official release:

1. Update the `LLAMA_CPP_RELEASE` default (`fetch-llama-release.*`) and confirm upstream release asset names.
2. `cd llama.cpp && git fetch origin <tag> && git checkout <tag>`, then `git add llama.cpp` at the repo root.
3. Run `./scripts/build-all.sh` once.
4. Diff tokens and Chinese output against the old HF model with the golden prompt.

## API

gateway exposes these endpoints to Electron (fully compatible with the old bridge):

| Type | State | Notes |
|------|------|------|
| `GET /api/health` | ok | wraps llama-server health + reports backend |
| `POST /api/chat` (SSE) | ok | OpenAI stream → custom SSE + ThinkBlockFilter |
| `POST /api/warmup` | ok | 1-token completion to warm mmap/KV cache |
| `GET /api/models` | ok | scans `<MODEL_ROOT>/**/*.gguf` |
| `POST /api/load-model` | ok | restarts the llama-server child onto the new gguf |
| `GET /api/devices`, `POST /api/set-device` | ok | reports metal/cuda/cpu backend |
| `GET /api/onboarding` | ok | model_present / stage_hint |
| `GET /api/update-check`, `POST /api/update-apply` | ok | GGUF incremental download |
| `POST /api/state` | ok | manual pet-state push |
| `GET /api/adapters` | ok | scans `<MINICPM_ADAPTER_DIR>/**/*.gguf`, returns `{items, current, current_name, adapter_dir}` |
| `POST /api/load-adapter` | ok | switches the globally active LoRA (`{path}`, `null` unloads); a new file restarts the llama-server child |
| `POST /api/classify` | stub | v1 returns 501 |

See [`gateway/server.py`](gateway/server.py) for field details.

### LoRA adapter protocol

- The gateway preloads no LoRA by default; only the currently active adapter starts with `llama-server --lora`.
- "Active" is a single in-memory `current_adapter` value; each request picks an adapter scale explicitly via the `lora` field.
- On each `POST /api/chat`, the gateway injects the `lora: [...]` field into the OpenAI request body (PR #10994) based on the active adapter + the request's `disable_adapter`:

| `disable_adapter` | active adapter | `lora` injected into llama-server |
|------|------|------|
| `false` (default) | none | `[]` (explicitly disable all adapters, run on base) |
| `false` | `lora_neko.gguf` | `[{"id": 0, "scale": 1.0}]` |
| `true` | any | `[]` (this request explicitly disables all adapters, used for pet narration) |

Main dialogue and narration can run concurrently without persona bleed from global scale switching.

- Switching to a `.gguf` LoRA restarts llama-server (~2-4s) loading only that adapter; switching back to Base restarts once more to free LoRA memory.
- Adapter weights on disk must be GGUF. The script + example for converting from PEFT safetensors live in [`adapters/README.md`](../adapters/README.md).

## Thinking protocol with llama-server

MiniCPM5's chat template prefills `<think>\n` by default for reasoning.
This gateway forwards the client's `thinking` flag to llama-server via the OpenAI request body's `chat_template_kwargs.enable_thinking` field:

| Client `thinking` | llama-server | gateway output |
|------|------|------|
| `true`  | uses the `<think>` template, reasoning lands in `delta.reasoning_content` | `event: think` frames + `event: delta` frames |
| `false` | skips the `<think>` template, all tokens land in `delta.content` | `event: delta` frames only |

This matches the v0.7 PyTorch sidecar `enable_thinking` semantics; old UI behavior is preserved.
ThinkBlockFilter stays on the `content` stream path as a safety net for future
non-MiniCPM5 models leaking `<think>` tags into content.

## Measured perf (M4 Pro / 18 GB / Metal)

- Q4_K_M (657 MB) load-to-ready: ~4s
- Decode: ~198 tok/s
- `/api/warmup` round-trip: ~20ms (prompt-cache hit)
- `/api/load-model` hot swap (Q4_K_M → Q8_0): ~3s, incl. stop-old / start-new / health polling
