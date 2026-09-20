#!/usr/bin/env bash
# go.sh — [dev shortcut] launch MiniCPM pet in dev mode
#
# ┌────────────────────────────────────────────────────────────────┐
# │  For DEVELOPERS, not end users.                                │
# │  End users: download the dmg/exe installer, then follow Onboarding. │
# │                                                                │
# │  ./go.sh does:                                                 │
# │  1) Install Node / uv deps                                     │
# │  2) Download official llama-server from the llama.cpp release  │
# │  3) uv sync: fastapi/uvicorn etc. for the gateway              │
# │  4) npm install + npm start (Electron dev mode)                │
# │                                                                │
# │  Packaged .app / .dmg / .exe bundles minicpm-sidecar already,  │
# │  no dependency on this script.                                 │
# └────────────────────────────────────────────────────────────────┘
#
# Usage:
#   ./go.sh                # install + launch (foreground)
#   ./go.sh setup          # deps only, no launch
#   ./go.sh start          # launch, skip dep checks
#   ./go.sh doctor         # check env, change nothing
#   ./go.sh fetch-llama    # re-download official llama-server
#   ./go.sh build          # full installer (mac arm64 dmg)

set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIDECAR_DIR="$HERE/minicpm-sidecar"
APP_DIR="$HERE/clawd-on-desk"
MODELS_DIR="$HERE/models"

cyan()   { printf "\033[36m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }

# ── fnm-installed node lives outside /usr/local on most setups, so make
#    sure we can see it on every script run (idempotent no-op if missing).
load_fnm_env() {
  local fnm_dir="${FNM_DIR:-$HOME/.local/share/fnm}"
  if [[ -x "$fnm_dir/fnm" ]]; then
    export PATH="$fnm_dir:$PATH"
    eval "$("$fnm_dir/fnm" env --shell bash 2>/dev/null)" || true
  fi
}

ensure_node() {
  load_fnm_env
  if command -v node >/dev/null 2>&1; then
    local ver
    ver=$(node -v | sed 's/^v\([0-9]*\).*/\1/')
    if [[ -n "$ver" && "$ver" -ge 18 ]]; then
      green "    ✓ Node $(node -v)"
      return 0
    fi
    yellow "    Node $(node -v) < 18, please upgrade"
  else
    yellow "    Node not installed, installing..."
  fi

  if command -v brew >/dev/null 2>&1; then
    cyan "    Installing Node 22 (LTS) via Homebrew..."
    if brew install node@22; then
      brew link --overwrite --force node@22 || true
      if command -v node >/dev/null 2>&1; then
        green "    ✓ Node $(node -v) (brew)"
        return 0
      fi
    fi
    yellow "    Homebrew install failed, trying fnm..."
  fi

  if ! command -v fnm >/dev/null 2>&1; then
    cyan "    Installing fnm (Node version manager, no sudo)..."
    curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell
  fi
  load_fnm_env
  if ! command -v fnm >/dev/null 2>&1; then
    red "fnm auto-install failed. Please install Node 18+ manually: https://nodejs.org/"
    exit 1
  fi

  cyan "    fnm install 22..."
  fnm install 22
  fnm use 22
  fnm default 22 || true
  load_fnm_env

  if ! command -v node >/dev/null 2>&1; then
    red "Node still not found after install. Check ~/.local/share/fnm/ or restart the terminal."
    exit 1
  fi
  green "    ✓ Node $(node -v) (fnm)"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    green "    ✓ uv $(uv --version)"
    return 0
  fi
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
    green "    ✓ uv $(uv --version) (~/.local/bin)"
    return 0
  fi
  yellow "    uv not installed, installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    red "uv auto-install failed. Install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
  green "    ✓ uv $(uv --version) (fresh install)"
}

check_environment() {
  cyan "==> Checking environment..."

  if [[ "$(uname)" != "Darwin" && "$(uname)" != "Linux" ]]; then
    yellow "    Windows users: use PowerShell scripts\\go.ps1 (coming later)"
  fi

  ensure_node
  ensure_uv

  if [[ ! -f "$SIDECAR_DIR/pyproject.toml" ]]; then
    red "Missing $SIDECAR_DIR/pyproject.toml — incomplete checkout?"
    exit 1
  fi
  green "    ✓ minicpm-sidecar/"

  if [[ ! -f "$APP_DIR/package.json" ]]; then
    red "Missing $APP_DIR/package.json"
    exit 1
  fi
  green "    ✓ clawd-on-desk/"
}

ensure_llama_server() {
  cyan "==> Checking llama-server..."
  local triple
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  triple="mac-arm64" ;;
    Darwin-x86_64) triple="mac-x64" ;;
    Linux-x86_64)  triple="linux-x64" ;;
    Linux-aarch64) triple="linux-arm64" ;;
    *)             triple="unknown" ;;
  esac
  local bin="$SIDECAR_DIR/bin/$triple/llama-server"
  if [[ -x "$bin" ]]; then
    green "    ✓ llama-server found ($bin)"
    return 0
  fi
  cyan "    First run: downloading official llama.cpp release llama-server..."
  ( cd "$SIDECAR_DIR" && ./scripts/fetch-llama-release.sh )
  green "    ✓ llama-server ready"
}

install_python_deps() {
  cyan "==> Gateway deps (uv sync)..."
  if [[ -d "$SIDECAR_DIR/.venv" && -f "$SIDECAR_DIR/.venv/bin/python" ]]; then
    green "    ✓ .venv exists, skipping sync (run ./go.sh setup --force-sync to force)"
  else
    cyan "    First install, only a few dozen MB (fastapi + uvicorn + httpx + huggingface_hub)..."
    ( cd "$SIDECAR_DIR" && uv sync )
    green "    ✓ Gateway deps installed"
  fi
}

install_npm_deps() {
  cyan "==> Electron deps (npm install)..."
  if [[ -d "$APP_DIR/node_modules" ]]; then
    green "    ✓ node_modules exists, skipping"
  else
    ( cd "$APP_DIR" && npm install --no-audit --no-fund )
    green "    ✓ npm deps installed"
  fi
}

# LoRA adapter weights (.gguf) are not in git; fetch from Hugging Face on first run / when missing
# (idempotent: skip if a valid local file already exists).
#   $1 == "required" → abort on failure (packaging path, missing file = broken bundle)
#   otherwise        → best effort (dev start, pet can run on Base persona if fetch fails)
fetch_adapters() {
  cyan "==> LoRA adapter weights (Hugging Face, download if missing)..."
  load_fnm_env
  if ! command -v node >/dev/null 2>&1; then
    yellow "    ⚠ Node not in PATH, skipping adapter download"
    return 0
  fi
  if ( cd "$APP_DIR" && node scripts/fetch-adapters.js ); then
    return 0
  fi
  if [[ "${1:-}" == "required" ]]; then
    red "    Adapter download failed, bundle will miss the neko persona. Check network and retry."
    exit 1
  fi
  yellow "    ⚠ Adapter download failed; pet can run on Base persona for now, retry later with npm run fetch:adapters."
}

start_pet() {
  cyan "==> Starting deskpet..."
  load_fnm_env
  if ! command -v node >/dev/null 2>&1; then
    red "Node not in PATH. Run ./go.sh setup once, or restart the terminal."
    exit 1
  fi
  # Hint the Electron host where to find the sidecar source and Python.
  export MINICPM_SIDECAR_DIR="$SIDECAR_DIR"
  export MINICPM_PYTHON="$SIDECAR_DIR/.venv/bin/python"
  # If the dev hasn't dropped a .gguf into <repo>/models/ yet, the sidecar
  # boots in "waiting for model" mode and Onboarding will offer to
  # download one.
  if [[ -d "$MODELS_DIR" ]]; then
    export MINICPM_MODEL_DIR="$MODELS_DIR"
  fi
  green "    MINICPM_SIDECAR_DIR=$MINICPM_SIDECAR_DIR"
  green "    MINICPM_PYTHON=$MINICPM_PYTHON"
  [[ -n "${MINICPM_MODEL_DIR:-}" ]] && green "    MINICPM_MODEL_DIR=$MINICPM_MODEL_DIR"
  echo
  green "Deskpet starting... Close the terminal (Ctrl+C) to stop."
  cd "$APP_DIR" && exec npm start
}

cmd="${1:-run}"
case "$cmd" in
  doctor)
    check_environment
    green ""
    green "✅ Environment ready, run ./go.sh to start."
    ;;
  setup)
    check_environment
    ensure_llama_server
    install_python_deps
    install_npm_deps
    fetch_adapters
    green ""
    green "✅ Setup complete. Next: ./go.sh start"
    ;;
  start)
    start_pet
    ;;
  fetch-llama|build-llama)
    check_environment
    if [[ "$cmd" == "build-llama" ]]; then
      yellow "    build-llama is now just a compat alias; it downloads the official llama.cpp release."
    fi
    ( cd "$SIDECAR_DIR" && ./scripts/fetch-llama-release.sh )
    ;;
  run|"")
    check_environment
    ensure_llama_server
    install_python_deps
    install_npm_deps
    fetch_adapters
    start_pet
    ;;
  build)
    # All-in-one: download official llama-server + PyInstaller gateway →
    # electron-builder dmg. Output in clawd-on-desk/dist/*.dmg.
    check_environment
    ensure_llama_server
    install_python_deps
    install_npm_deps
    fetch_adapters required
    cyan "==> Building gateway + staging sidecar-bin..."
    ( cd "$SIDECAR_DIR" && ./scripts/build-all.sh )
    cyan "==> Packaging Electron app (electron-builder)..."
    cd "$APP_DIR" && npx electron-builder --mac --arm64 -c.mac.target=dmg
    green "==> Done. dmg is at $APP_DIR/dist/"
    ;;
  *)
    red "Unknown command: $cmd"
    red "Usage: ./go.sh [doctor|setup|start|run|build|fetch-llama]"
    exit 1
    ;;
esac
