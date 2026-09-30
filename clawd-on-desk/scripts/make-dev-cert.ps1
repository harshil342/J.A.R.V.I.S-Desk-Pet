#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Create (or reuse) the self-signed Authenticode certificate DeskPet signs with.

.DESCRIPTION
  A self-signed certificate cannot buy SmartScreen reputation, but it is the
  cheapest legitimate way to get two things that matter:

    1. A real publisher name instead of "Unknown publisher".
    2. A working auto-updater. electron-updater verifies the Authenticode
       signature of the downloaded .exe, so an unsigned build is rejected at
       install time. Signing is a functional requirement, not a trust badge.

  Built with System.Security.Cryptography rather than
  New-SelfSignedCertificate. The cmdlet goes through the CertEnroll COM layer,
  which returns NTE_PERM (access denied) in restricted and non-interactive
  contexts - which is exactly where a build agent runs. The .NET path needs no
  COM, works everywhere, and lets every extension be stated explicitly.

  The certificate is deliberately built the way Windows wants a code-signing
  cert to be, because the defaults from a quick "self-signed cert" wizard are
  usually wrong:
    - EnhancedKeyUsage Code Signing (1.3.6.1.5.5.7.3.3). Without it some
      tooling treats the cert as unfit for signing and refuses the file.
    - KeyUsage DigitalSignature only. KeyEncipherment would imply RSA key
      exchange, which a code-signing key must never be used for.
    - SHA-256. SHA-1 signing is rejected outright by modern Windows.
    - Ten-year validity. A short one silently breaks the updater years later,
      and a broken updater is far harder to notice than a missing certificate.

.PARAMETER Force
  Reissue. The thumbprint changes, so Windows treats it as a different
  publisher and every previously-trusted copy needs -Trust again.

.PARAMETER Trust
  Also import the public certificate into the current user's Root and
  TrustedPublisher stores, which is what removes the publisher warning.
  Deliberately CurrentUser: no administrator rights, and a dev cert has no
  business in the machine-wide store.

.EXAMPLE
  ./scripts/make-dev-cert.ps1
  ./scripts/make-dev-cert.ps1 -Trust -Force
#>
[CmdletBinding()]
param(
  [switch]$Force,
  [switch]$Trust,
  [string]$Subject = "Deskpet",
  [int]$ValidYears = 10
)

$ErrorActionPreference = 'Stop'

# The password never lives in the repo. It comes from the environment, with a
# documented default so a fresh clone can sign a local build with no setup.
$envName = 'DESKPET_CERT_PASSWORD'
$fromEnv = [Environment]::GetEnvironmentVariable($envName)   # $env:$name is not valid
$password = if ($fromEnv) { $fromEnv } else { 'deskpet-dev' }
if (-not $fromEnv) {
  Write-Warning "$envName is unset, using the local-dev default. Set it before any real release."
}

$pfxPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\build\deskpet-selfsigned.pfx'))

function Test-Managed {
  param([string]$Name)
  foreach ($store in 'Root', 'TrustedPublisher', 'My') {
    $target = "Cert:\CurrentUser\$store"
    if (-not (Test-Path $target)) { continue }
    if (Get-ChildItem $target -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq "CN=$Name" -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) }) {
      return $true
    }
  }
  return $false
}

if ((Test-Managed $Subject) -and -not $Force) {
  Write-Host "A valid signing cert for CN=$Subject already exists. Pass -Force to reissue." -ForegroundColor Yellow
} else {
  $rsa = [System.Security.Cryptography.RSA]::Create(3072)
  $req = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    "CN=$Subject",
    $rsa,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
  )

  # Not a CA: a code-signing leaf must not be able to sign other certificates.
  $req.CertificateExtensions.Add(
    [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
  $req.CertificateExtensions.Add(
    [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
      [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature, $true))
  $eku = [System.Security.Cryptography.OidCollection]::new()
  # 1.3.6.1.5.5.7.3.3 = id-kp-codeSigning
  $eku.Add([System.Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
  $req.CertificateExtensions.Add(
    [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($eku, $false))
  # Thumbprint is a SHA-1 over the cert by definition, but Windows wants the
  # *signature* algorithm to be explicit about SHA-256.
  $req.CertificateExtensions.Add(
    [System.Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension]::new($req.PublicKey, $false))

  $notAfter = (Get-Date).AddYears($ValidYears)
  $cert = $req.CreateSelfSigned((Get-Date).AddMinutes(-5), $notAfter)

  # Export exactly once. Re-exporting a PFX that was itself loaded from a PFX
  # trips .NET's PBKDF iteration ceiling ("PKCS12 without a supplied password
  # has exceeded maximum allowed iterations"), so the bytes are kept and reused
  # rather than round-tripped.
  $pfxBytes = $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $password)
  [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($pfxPath)) | Out-Null
  [System.IO.File]::WriteAllBytes($pfxPath, $pfxBytes)

  $forStore = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    $pfxBytes, $password,
    [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable)
  $my = [System.Security.Cryptography.X509Certificates.X509Store]::new('My', 'CurrentUser')
  $my.Open('ReadWrite'); $my.Add($forStore); $my.Close()
  Write-Host "Created $($forStore.Thumbprint), valid until $($forStore.NotAfter)" -ForegroundColor Green
}

if (-not (Test-Path $pfxPath)) {
  $leaf = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq "CN=$Subject" -and $_.HasPrivateKey } | Select-Object -First 1
  if (-not $leaf) { throw "no cert found for CN=$Subject and no existing pfx" }
  $bytes = $leaf.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $password)
  [System.IO.File]::WriteAllBytes($pfxPath, $bytes)
}
Write-Host "Exported $pfxPath" -ForegroundColor Green

if ($Trust) {
  $leaf = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq "CN=$Subject" -and $_.HasPrivateKey } | Select-Object -First 1
  foreach ($name in 'Root', 'TrustedPublisher') {
    $store = [System.Security.Cryptography.X509Certificates.X509Store]::new($name, 'CurrentUser')
    $store.Open('ReadWrite')
    try {
      # Add() throws on a duplicate, which is the normal case on a re-run.
      if (-not ($store.Certificates | Where-Object Thumbprint -eq $leaf.Thumbprint)) { $store.Add($leaf) }
    } finally { $store.Close() }
  }
  Write-Host "Trusted in CurrentUser\Root and CurrentUser\TrustedPublisher" -ForegroundColor Green
}

$pfxUrl = 'file:///' + ($pfxPath -replace '\\', '/')
Write-Host ""
Write-Host "Next:"
Write-Host "  `$env:CSC_LINK = '$pfxUrl'"
Write-Host "  `$env:CSC_KEY_PASSWORD = '$envName'"
Write-Host "  npm run build:win:mvp"
Write-Host ""
Write-Host "The .pfx is gitignored. Never commit it."
