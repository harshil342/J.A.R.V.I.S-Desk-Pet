#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Verify the Deskpet signing certificate can actually sign, and that a signed
  file verifies afterwards.

.DESCRIPTION
  A certificate that exists is not a certificate that works. This signs a real
  PE executable and reads the signature back, so a broken key, a wrong Enhanced
  Key Usage, or a trust-store problem shows up here rather than after a
  fifteen-minute electron-builder run.

  Uses Set-AuthenticodeSignature rather than signtool.exe, so no Windows SDK is
  required. electron-builder uses its own bundled signtool, but the underlying
  .NET signing stack is the same and this keeps the check dependency-free.
#>
[CmdletBinding()]
param(
  [string]$Subject = "Deskpet",
  [string]$ProbeSource,
  [switch]$KeepArtifact
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not populated while param defaults are evaluated, so resolve
# the probe path here rather than in the default.
if (-not $ProbeSource) {
  $ProbeSource = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot '..\..\minicpm-sidecar\bin\win-x64\llama-server.exe'))
}

$cert = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
  Where-Object { $_.Subject -eq "CN=$Subject" -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
  Select-Object -First 1
if (-not $cert) { throw "no usable signing cert for CN=$Subject. Run scripts/make-dev-cert.ps1 first." }

Write-Host "cert        $($cert.Thumbprint)"
Write-Host "signature   $($cert.SignatureAlgorithm.FriendlyName)"
Write-Host "eku         $(($cert.EnhancedKeyUsageList | ForEach-Object { $_.FriendlyName }) -join ', ')"
Write-Host "expires     $($cert.NotAfter.ToString('yyyy-MM-dd'))"
Write-Host "trusted     $(@('Root','TrustedPublisher') | ForEach-Object { $s = "Cert:\CurrentUser\$_"; (Test-Path $s) -and [bool](Get-ChildItem $s -ErrorAction SilentlyContinue | Where-Object Thumbprint -eq $cert.Thumbprint) } | Where-Object { $_ } | Measure-Object | Select-Object -ExpandProperty Count)/2 stores"
Write-Host ""

  # A real PE, and specifically an UNSIGNED one. Authenticode permits exactly
  # one signature per file, so signing something that already carries one is a
  # silent no-op: the new signature is dropped and verification happily reports
  # the original signer. That is a real trap - signing a copy of notepad.exe
  # "verifies" as CN=Microsoft Windows. Hence the subject assertion below.
  $probe = Join-Path ([System.IO.Path]::GetTempPath()) "deskpet-signprobe-$([guid]::NewGuid().ToString('N').Substring(0,8)).exe"
  $source = $ProbeSource
  if (-not (Test-Path $source)) { throw "probe source not found: $source" }
  $existing = Get-AuthenticodeSignature $source -ErrorAction SilentlyContinue
  if ($existing.Status -ne 'NotSigned') {
    throw "probe source is already signed ($($existing.Status)). Pick an unsigned PE."
  }
  Copy-Item $source $probe -Force
  try {
  $sig = Set-AuthenticodeSignature -FilePath $probe -Certificate $cert `
    -HashAlgorithm SHA256 -TimestampServer '' -ErrorAction Stop
  Write-Host "sign        $($sig.Status)"
  if ($sig.Status -ne 'Valid') { throw "signing failed: $($sig.StatusMessage)" }

  $check = Get-AuthenticodeSignature -FilePath $probe
  Write-Host "verify      $($check.Status)  subject=$($check.SignerCertificate.Subject)"

  # Without this the probe can pass while signing nothing at all.
  if ($check.SignerCertificate.Subject -ne $cert.Subject) {
    throw "signature is NOT ours: expected $($cert.Subject), got $($check.SignerCertificate.Subject). The file was probably already signed."
  }
  Write-Host "subject     ours, as expected"

  $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
  $chain.ChainPolicy.RevocationMode = 'NoCheck'
  $ok = $chain.Build($check.SignerCertificate)
  Write-Host "chain       $(if ($ok) { 'valid' } else { 'UNTRUSTED: ' + (($chain.ChainStatus | ForEach-Object { $_.StatusInformation.Trim() }) -join '; ') })"

  # A self-signed cert is only ever going to be trusted on machines that were
  # told to trust it, so say so plainly instead of calling this "valid".
  $trusted = @('Root', 'TrustedPublisher') | Where-Object {
    $s = "Cert:\CurrentUser\$_"
    (Test-Path $s) -and [bool](Get-ChildItem $s -ErrorAction SilentlyContinue | Where-Object Thumbprint -eq $cert.Thumbprint)
  }
  if ($trusted.Count -eq 2) {
    Write-Host ""
    Write-Host "OK - signed, verified, and trusted on this machine."
  } else {
    Write-Host ""
    Write-Host "Signed and verified, but only in $($trusted -join ', '). Run make-dev-cert.ps1 -Trust to trust it fully."
  }
} finally {
  if (-not $KeepArtifact -and (Test-Path $probe)) { Remove-Item $probe -Force }
}
