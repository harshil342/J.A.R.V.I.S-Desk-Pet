#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Remove the DeskPet signing certificate from the current user's trust stores.

.DESCRIPTION
  The installer adds CN=Deskpet to CurrentUser\Root and
  CurrentUser\TrustedPublisher so electron-updater can verify a signed build.
  This undoes exactly that, and only that.

  Matched by subject name rather than thumbprint on purpose: reissuing the
  certificate changes the thumbprint (see make-dev-cert.ps1 -Force), and a
  thumbprint baked into the installer would then leave a root certificate
  behind that nothing can ever remove.

  CurrentUser only, matching the installer. Nothing here needs administrator
  rights, and nothing touches the machine-wide store.

  A failure here is not worth blocking an uninstall over, so every store is
  closed in a finally and the script exits 0 regardless. A leftover dev
  certificate is untidy; a failed uninstall is worse.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File untrust-cert.ps1
#>
[CmdletBinding()]
param(
  [string]$Subject = "CN=Deskpet"
)

$ErrorActionPreference = 'Stop'

foreach ($name in 'Root', 'TrustedPublisher') {
  $store = [System.Security.Cryptography.X509Certificates.X509Store]::new($name, 'CurrentUser')
  try {
    $store.Open('ReadWrite')
    $stale = $store.Certificates | Where-Object { $_.Subject -eq $Subject }
    foreach ($cert in $stale) {
      $store.Remove($cert)
      Write-Host "removed $Subject from CurrentUser\$name"
    }
  } catch {
    Write-Warning "could not clean CurrentUser\${name}: $($_.Exception.Message)"
  } finally {
    $store.Close()
  }
}

exit 0
