#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Build the Windows installer signed with the local development certificate.

.DESCRIPTION
  Signing is not optional for DeskPet: electron-updater verifies the
  Authenticode signature of the downloaded .exe, so an unsigned release is
  rejected at install time. Users would get an app that installs once and then
  silently stops receiving updates.

  This wires the certificate into electron-builder without adding a dependency.
  Windows-only by design (decision D3), so the env vars are set from PowerShell
  rather than pulling in cross-env for a platform we no longer build.

  Regenerates the certificate if it is missing, so a fresh clone can produce a
  signed build in one command.
#>
[CmdletBinding()]
param(
  [ValidateSet('mvp', 'x64', 'arm64', 'all')]
  [string]$Target = 'mvp',
  [switch]$SkipCert,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$appDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

$pfx = Join-Path $appDir 'build\deskpet-selfsigned.pfx'

if (-not $SkipCert) {
  $haveCert = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq 'CN=Deskpet' -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) }
  if (-not $haveCert -or -not (Test-Path $pfx)) {
    Write-Host "No usable signing cert; creating one." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'make-dev-cert.ps1') -Trust | Out-Host
  }
}

if (-not (Test-Path $pfx)) {
  throw "no certificate at $pfx - run scripts/make-dev-cert.ps1 first, or pass -SkipCert to build unsigned."
}

# Verify before spending fifteen minutes on a build that would ship unsigned.
Write-Host "Verifying the certificate can sign..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'verify-signing.ps1') | Out-Host

$pfxUrl = 'file:///' + ($pfx -replace '\\', '/')
$env:CSC_LINK = $pfxUrl
$env:WIN_CSC_LINK = $pfxUrl
$env:CSC_KEY_PASSWORD = if ($env:DESKPET_CERT_PASSWORD) { $env:DESKPET_CERT_PASSWORD } else { 'deskpet-dev' }
# A self-signed cert has no timestamp authority, and asking for one makes
# signtool fail rather than fall back. Real CA certs should set this.
$env:WIN_CSC_TIMESTAMP = if ($env:DESKPET_CERT_TIMESTAMP) { $env:DESKPET_CERT_TIMESTAMP } else { '' }

$script = switch ($Target) {
  'mvp' { 'build:win:mvp' }
  'x64' { 'build:win:x64' }
  'arm64' { 'build:win:arm64' }
  'all' { 'build:win:all' }
}

if ($DryRun) {
  Write-Host ""
  Write-Host "[dry] would run: npm run $script   (CSC_LINK=$pfxUrl)"
  exit 0
}

Write-Host ""
Write-Host "Running: npm run $script" -ForegroundColor Green
& npm.cmd run $script
exit $LASTEXITCODE
