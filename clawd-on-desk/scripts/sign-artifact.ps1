#Requires -Version 5.1
<#
.SYNOPSIS
  Sign a built Windows installer with the local self-signed certificate.

.DESCRIPTION
  Signs in place with Set-AuthenticodeSignature rather than signtool.exe, so no
  Windows SDK is needed, and the certificate never leaves this machine: it is
  read from the current user's store.

  Why signing happens here rather than in CI: electron-builder's
  verifyUpdateCodeSignature defaults to true, so an unsigned installed app makes
  autoUpdater.checkForUpdates() throw. That is why the release is created as a
  draft and why this runs before the release is published. CI builds; this
  signs; release.mjs then uploads and publishes.

  The publisher name matters more than the trust chain. electron-updater
  compares the publisher of the downloaded build against the installed one, and
  CN=Deskpet is stable across releases, so a self-signed signature satisfies it.

.EXAMPLE
  .\scripts\sign-artifact.ps1 -Path ..\dist\Deskpet-0.12.0-x64.exe
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Path
)

$ErrorActionPreference = 'Stop'

$resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
$file = Get-Item -LiteralPath $resolved

if ($file.Extension -ne '.exe') {
  throw "expected a .exe, got $($file.Extension): $Path"
}

$existing = Get-AuthenticodeSignature -LiteralPath $resolved -ErrorAction SilentlyContinue
if ($existing.Status -ne 'NotSigned') {
  # Re-signing an already-signed file with our own cert is harmless, but it is
  # almost always a mistake - most often finalising twice.
  Write-Host "already signed ($($existing.Status)); leaving it alone." -ForegroundColor Yellow
  exit 0
}

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
  Sort-Object NotAfter -Descending |
  Select-Object -First 1
if (-not $cert) {
  throw "no code-signing certificate in Cert:\CurrentUser\My. Run 'npm run sign:dev' first."
}

Write-Host "signing    $($file.Name)  ($([math]::Round($file.Length/1MB,1)) MB)" -ForegroundColor Cyan
Write-Host "subject    $($cert.Subject)"
Write-Host "thumbprint $($cert.Thumbprint)"
Write-Host "expires    $($cert.NotAfter)"

$sig = Set-AuthenticodeSignature -FilePath $resolved -Certificate $cert `
  -HashAlgorithm SHA256 -TimestampServer '' -ErrorAction Stop

if ($sig.Status -ne 'Valid') {
  throw "signing failed: $($sig.StatusMessage)"
}

# Verify rather than trust the return value. Without this, a file that was
# already signed by something else reports success and we ship that instead.
$check = Get-AuthenticodeSignature -LiteralPath $resolved
if ($check.Status -ne 'Valid') {
  throw "post-sign verification failed: $($check.Status)"
}
if ($check.SignerCertificate.Subject -ne $cert.Subject) {
  throw ("signature is NOT ours: expected {0}, got {1}. The file was probably already signed." -f `
      $cert.Subject, $check.SignerCertificate.Subject)
}

Write-Host "verified   $($check.Status)  $($check.SignerCertificate.Subject)" -ForegroundColor Green
Write-Host "OK $Path"