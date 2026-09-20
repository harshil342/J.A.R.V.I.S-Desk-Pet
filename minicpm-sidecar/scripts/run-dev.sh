#!/usr/bin/env bash
# Launch the gateway in dev mode against an already-built llama-server.
#
# Prerequisites:
#   ./fetch-llama-release.sh   # one-time
#   uv sync                                 # populates .venv/
#
# Usage:
#   ./scripts/run-dev.sh --model /path/to/minicpm.gguf [--port 18765]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

cd "$ROOT"

if [[ ! -d ".venv" ]]; then
  echo "==> .venv missing, running uv sync first ..."
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not installed. Install first: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  uv sync
fi

exec ./.venv/bin/python -m gateway "$@"
