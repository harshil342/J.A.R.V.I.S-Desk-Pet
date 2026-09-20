#!/usr/bin/env bash
# Legacy source-build fallback for llama-server on the current host platform.
# Default builds use fetch-llama-release.sh instead.
#
# Output:
#   bin/<os>-<arch>/llama-server[.exe]
#   bin/<os>-<arch>/*.dylib|*.so   (any runtime libs llama-server needs)
#
# Honors:
#   LLAMA_ACCEL = metal | cuda | cpu   (default: auto by platform)
#   MINICPM_MACOSX_DEPLOYMENT_TARGET = 14.0 by default on macOS

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
SRC="$REPO_ROOT/llama.cpp"
BUILD="$SRC/build"

cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }

version_le() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    split(a, A, "."); split(b, B, ".");
    for (i = 1; i <= 3; i++) {
      ai = A[i] + 0; bi = B[i] + 0;
      if (ai < bi) exit 0;
      if (ai > bi) exit 1;
    }
    exit 0;
  }'
}

version_lt() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    split(a, A, "."); split(b, B, ".");
    for (i = 1; i <= 3; i++) {
      ai = A[i] + 0; bi = B[i] + 0;
      if (ai < bi) exit 0;
      if (ai > bi) exit 1;
    }
    exit 1;
  }'
}

if [[ ! -e "$SRC/.git" ]]; then
  red "$SRC missing. Init submodule first: git submodule update --init llama.cpp"
  exit 1
fi

# ── Pick target triple + cmake flags ────────────────────────────────────────
# Triple names match electron-builder's `${os}-${arch}` expansion so the
# packager can drop our bin/<triple>/ straight into extraResources.
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)
    TARGET="mac-arm64"
    ACCEL="${LLAMA_ACCEL:-metal}"
    ;;
  Darwin-x86_64)
    TARGET="mac-x64"
    ACCEL="${LLAMA_ACCEL:-cpu}"
    ;;
  Linux-x86_64)
    TARGET="linux-x64"
    ACCEL="${LLAMA_ACCEL:-vulkan}"
    ;;
  Linux-aarch64)
    TARGET="linux-arm64"
    ACCEL="${LLAMA_ACCEL:-cpu}"
    ;;
  *)
    red "Unsupported host: $(uname -s) $(uname -m). For Windows use build-llama.ps1."
    exit 1
    ;;
esac

CMAKE_FLAGS=(
  -DBUILD_SHARED_LIBS=OFF
  -DLLAMA_BUILD_TESTS=OFF
  -DLLAMA_BUILD_EXAMPLES=OFF
  -DLLAMA_BUILD_TOOLS=ON
  -DLLAMA_CURL=OFF
  # cpp-httplib (vendored in llama.cpp) auto-links OpenSSL when
  # find_package(OpenSSL) succeeds, producing a binary that depends on
  # libcrypto/libssl. We don't need HTTPS for the 127.0.0.1-only sidecar.
  #
  # Failure modes if this flag is missing:
  #   - Windows: vcpkg's libcrypto-3-x64.dll isn't bundled into bin/win-x64/,
  #     so the user gets STATUS_DLL_NOT_FOUND at first launch.
  #   - macOS: Homebrew's openssl@3 dylib gets embedded by absolute path
  #     (`/opt/homebrew/opt/openssl@3/lib/lib{ssl,crypto}.3.dylib`). End
  #     users without Homebrew (or on Intel Mac, or with openssl at a
  #     different prefix) hit dyld "Library not loaded" and the sidecar
  #     reports "Llama Server is not running".
  # Hard-disable discovery for parity across platforms.
  -DCMAKE_DISABLE_FIND_PACKAGE_OpenSSL=ON
)

MACOS_DEPLOYMENT_TARGET=""
if [[ "$(uname -s)" == "Darwin" ]]; then
  # The app README promises macOS 14.0+. Newer Xcode/macOS hosts otherwise
  # default to their own SDK as the minimum runtime (for example 26.0), which
  # strongly links newer Metal symbols such as MTLResidencySetDescriptor and
  # makes llama-server fail to load on macOS 14 before @available checks run.
  MACOS_DEPLOYMENT_TARGET="${MINICPM_MACOSX_DEPLOYMENT_TARGET:-14.0}"
  export MACOSX_DEPLOYMENT_TARGET="$MACOS_DEPLOYMENT_TARGET"
  CMAKE_FLAGS+=(
    "-DCMAKE_OSX_DEPLOYMENT_TARGET=$MACOS_DEPLOYMENT_TARGET"
    "-DGGML_METAL_MACOSX_VERSION_MIN=$MACOS_DEPLOYMENT_TARGET"
  )
fi

case "$ACCEL" in
  metal)
    CMAKE_FLAGS+=( -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON )
    ;;
  vulkan)
    CMAKE_FLAGS+=( -DGGML_VULKAN=ON )
    ;;
  cuda)
    CMAKE_FLAGS+=( -DGGML_CUDA=ON )
    ;;
  cpu)
    CMAKE_FLAGS+=( -DGGML_METAL=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF )
    ;;
  *)
    red "Unknown LLAMA_ACCEL=$ACCEL (expected metal/vulkan/cuda/cpu)"
    exit 1
    ;;
esac

# Vulkan backend needs the SPIRV-Headers CMake config. The LunarG SDK places it under
# $VULKAN_SDK/share/cmake/ or a subdir; add it to CMAKE_PREFIX_PATH as a fallback.
if [[ "$ACCEL" == "vulkan" && -n "${VULKAN_SDK:-}" ]]; then
  cyan "==> Using VULKAN_SDK: $VULKAN_SDK"
  CMAKE_FLAGS+=( "-DCMAKE_PREFIX_PATH=$VULKAN_SDK" )
fi

cyan "==> Target: $TARGET   Accel: $ACCEL"
cyan "==> Source: $SRC"
if [[ -n "$MACOS_DEPLOYMENT_TARGET" ]]; then
  cyan "==> macOS deployment target: $MACOS_DEPLOYMENT_TARGET"
fi

if ! command -v cmake >/dev/null 2>&1; then
  red "cmake not found. Install first: brew install cmake / apt install cmake"
  exit 1
fi

mkdir -p "$BUILD"
# Drop any stale CMake cache so flag toggles (e.g. -DCMAKE_DISABLE_FIND_PACKAGE_OpenSSL)
# definitely take effect. Without this, a previously-configured tree that
# found OpenSSL keeps the libssl/libcrypto link edges forever and the
# resulting binary still hardcodes /opt/homebrew/opt/openssl@3/... .
if [[ -f "$BUILD/CMakeCache.txt" ]]; then
  cyan "==> Cleaning CMake cache (so OpenSSL opt-out flags take effect)"
  rm -f "$BUILD/CMakeCache.txt"
  rm -rf "$BUILD/CMakeFiles"
fi
cyan "==> cmake configure"
cmake -S "$SRC" -B "$BUILD" "${CMAKE_FLAGS[@]}"

JOBS="${LLAMA_JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"
if [[ -n "${CI:-}" && "$JOBS" -gt 4 ]]; then
  JOBS=4
fi
cyan "==> cmake build llama-server (-j$JOBS)"
cmake --build "$BUILD" --target llama-server --config Release -j"$JOBS"

# llama.cpp puts the binary somewhere predictable; try both layouts.
SERVER=""
for cand in \
  "$BUILD/bin/llama-server" \
  "$BUILD/tools/server/llama-server" \
  "$BUILD/llama-server" \
; do
  [[ -x "$cand" ]] && SERVER="$cand" && break
done

if [[ -z "$SERVER" ]]; then
  red "Build seemed OK but llama-server not found. Check $BUILD/bin"
  exit 1
fi

OUT="$ROOT/bin/$TARGET"
mkdir -p "$OUT"
cp -f "$SERVER" "$OUT/"
# Also copy any sibling .dylib/.so that the static-with-shared-lib build
# might emit (Metal kernels can land as a separate .metallib).
for ext in dylib so metallib; do
  find "$BUILD" -maxdepth 3 -name "*.$ext" -exec cp -f {} "$OUT/" \; 2>/dev/null || true
done

# ── Anti-regression: verify the binary has no absolute-path Homebrew /
#    vcpkg deps that won't exist on the user's machine. The recurring
#    failure mode is cpp-httplib sneaking OpenSSL back in, producing a
#    binary that depends on /opt/homebrew/opt/openssl@3/lib/lib{ssl,crypto}.3.dylib
#    and crashing at first launch on any user without that exact prefix.
cyan "==> Verifying llama-server linkage (no OpenSSL / Homebrew absolute-path regression)"
case "$(uname -s)" in
  Darwin)
    if ! command -v otool >/dev/null 2>&1; then
      red "otool missing, cannot verify linkage. Install Xcode Command Line Tools."
      exit 1
    fi
    DEPS="$(otool -L "$OUT/llama-server" | tail -n +2 || true)"
    BAD="$(printf '%s\n' "$DEPS" | grep -Ei '/(opt/homebrew|usr/local/opt|usr/local/Cellar|opt/local)/' || true)"
    if [[ -n "$BAD" ]]; then
      red "==> Built binary still depends on host Homebrew/MacPorts paths, will fail on user machines:"
      printf '%s\n' "$BAD" >&2
      red "Confirm opt-out flags like -DCMAKE_DISABLE_FIND_PACKAGE_OpenSSL=ON took effect,"
      red "then delete $BUILD and rebuild clean."
      exit 1
    fi
    SSL_BAD="$(printf '%s\n' "$DEPS" | grep -Ei 'libssl|libcrypto|openssl' || true)"
    if [[ -n "$SSL_BAD" ]]; then
      red "==> Built binary still links OpenSSL, likely missing on user machines:"
      printf '%s\n' "$SSL_BAD" >&2
      exit 1
    fi
    if [[ -n "$MACOS_DEPLOYMENT_TARGET" ]]; then
      MINOS="$(
        otool -l "$OUT/llama-server" |
          awk '/LC_BUILD_VERSION/{in_build=1; next} in_build && /minos/{print $2; exit}'
      )"
      if [[ -z "$MINOS" ]]; then
        MINOS="$(
          otool -l "$OUT/llama-server" |
            awk '/LC_VERSION_MIN_MACOSX/{in_min=1; next} in_min && /version/{print $2; exit}'
        )"
      fi
      if [[ -z "$MINOS" ]]; then
        red "==> Cannot read llama-server macOS minimum runtime (LC_BUILD_VERSION/LC_VERSION_MIN_MACOSX)."
        exit 1
      fi
      if ! version_le "$MINOS" "$MACOS_DEPLOYMENT_TARGET"; then
        red "==> llama-server minimum macOS runtime is $MINOS, expected <= $MACOS_DEPLOYMENT_TARGET."
        red "    On older macOS this shows dyld Symbol not found / built for newer OS."
        exit 1
      fi
      if command -v nm >/dev/null 2>&1 && version_lt "$MACOS_DEPLOYMENT_TARGET" "15.0"; then
        RESIDENCY_SYMBOL="$(nm -m "$OUT/llama-server" 2>/dev/null | grep '_OBJC_CLASS_\$_MTLResidencySetDescriptor' || true)"
        if [[ -n "$RESIDENCY_SYMBOL" && "$RESIDENCY_SYMBOL" != *"weak external"* ]]; then
          red "==> MTLResidencySetDescriptor is strongly linked; macOS 14 will fail before runtime availability checks:"
          printf '%s\n' "$RESIDENCY_SYMBOL" >&2
          exit 1
        fi
      fi
      cyan "==> macOS minimum runtime OK: $MINOS"
    fi
    ;;
  Linux)
    if command -v ldd >/dev/null 2>&1; then
      SSL_BAD="$(ldd "$OUT/llama-server" 2>/dev/null | grep -Ei 'libssl|libcrypto' || true)"
      if [[ -n "$SSL_BAD" ]]; then
        red "==> Built binary still links OpenSSL:"
        printf '%s\n' "$SSL_BAD" >&2
        exit 1
      fi
    fi
    ;;
esac

green "==> OK -> $OUT/$(basename "$SERVER")"
green "    smoke test: $OUT/llama-server --version"
