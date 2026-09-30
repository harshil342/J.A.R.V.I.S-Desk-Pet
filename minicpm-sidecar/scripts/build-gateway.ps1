# Build the gateway into a single-file PyInstaller binary on Windows.
#
# Usage:
#   .\build-gateway.ps1                    # default: win-x64
#   .\build-gateway.ps1 -Target win-arm64  # ARM64
#
# Output:
#   bin\<Target>\minicpm-sidecar.exe

param(
  [string] $Target = "win-x64"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here "..")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv not found. Install it with: irm https://astral.sh/uv/install.ps1 | iex"
}

Write-Host "==> Gateway target: $Target" -ForegroundColor Cyan

Push-Location $root
try {
  uv sync
  uv pip install "pyinstaller>=6.0"

  Remove-Item -Recurse -Force "build\build", "build\dist" -ErrorAction SilentlyContinue

  Push-Location "build"
  try {
    & "..\.venv\Scripts\pyinstaller.exe" gateway.spec `
      --distpath "..\build\dist" `
      --workpath "..\build\build" `
      --clean `
      --noconfirm
  } finally {
    Pop-Location
  }

  $out = Join-Path $root "bin\$Target"
  New-Item -ItemType Directory -Force -Path $out | Out-Null

  $src = Join-Path $root "build\dist\minicpm-sidecar.exe"
  if (-not (Test-Path $src)) {
    throw "PyInstaller output not found: $src"
  }

  # B7: a declared dependency PyInstaller could not bundle fails the build here
  # rather than silently at runtime. winotify shipped declared in pyproject.toml,
  # was absent from the frozen exe, and every reminder toast died with
  # "No module named 'winotify'" in a crash dump. docs/development.md predicted
  # this exact failure; it happened anyway, twice.
  #
  # PyInstaller already wrote the answer: warn-gateway.txt lists every module its
  # analysis could not find. Matching must be exact, because the file is mostly
  # benign optional/conditional imports and PyInstaller reports things like
  # "missing module named pydantic.BaseModel" (a class, not a module). A loose
  # match reports pydantic as missing and is wrong.
  $warnFile = Join-Path $root "build\build\gateway\warn-gateway.txt"
  if (-not (Test-Path $warnFile)) {
    Write-Warning "No warn-gateway.txt found; skipping the frozen-import check."
  } else {
    $warn = [System.IO.File]::ReadAllText($warnFile)
    $declared = [regex]::Matches(
      [System.IO.File]::ReadAllText((Join-Path $root "pyproject.toml")),
      '(?m)^\s+"([A-Za-z0-9_.-]+)[>=~]'
    ) | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique

    $missing = @()
    foreach ($dep in $declared) {
      # "missing module named X" where X is exactly the dependency: the
      # terminator must be end-of-line, not a dot.
      if ($warn -match "(?m)^missing module named $([regex]::Escape($dep))\s*$") { $missing += $dep }
    }

    Write-Host "==> Checked $($declared.Count) declared dependencies against warn-gateway.txt" -ForegroundColor Cyan
    if ($missing.Count -gt 0) {
      throw ("Frozen binary is missing declared dependencies: " + ($missing -join ", ") +
        "  -> add them to build/gateway.spec hiddenimports")
    }
  }

  Copy-Item -Force $src (Join-Path $out "minicpm-sidecar.exe")
  Write-Host "==> OK -> $out\minicpm-sidecar.exe" -ForegroundColor Green
} finally {
  Pop-Location
}
