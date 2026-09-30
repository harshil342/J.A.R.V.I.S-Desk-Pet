#Requires -Version 5.1
<#
.SYNOPSIS
  Assert that a PE file is built for a specific machine architecture.

.DESCRIPTION
  Renaming an x64 binary to arm64 is silent. Nothing fails: the file exists, it
  launches (under emulation), and the installer ships the wrong architecture.
  That is not hypothetical - release.yml did exactly this with

      Copy-Item -Force bin\win-x64\minicpm-sidecar.exe bin\win-arm64\minicpm-sidecar.exe

  and then "verified" the result with Test-Path, which cannot tell the
  difference. This reads the PE header instead, which can.

  PE layout: an MZ DOS header, then at e_lfanew (offset 0x3C) the "PE\0\0"
  signature, then the COFF file header whose first field is Machine.

.EXAMPLE
  .\scripts\assert-pe-arch.ps1 -Path bin\win-x64\llama-server.exe -Expect x64
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Path,
  [Parameter(Mandatory = $true)][ValidateSet('x64', 'arm64', 'x86')][string]$Expect
)

$ErrorActionPreference = 'Stop'

# IMAGE_FILE_MACHINE_* from winnt.h. Note the name: PowerShell variables are
# case-insensitive, so a later $machine scalar silently replaces $MACHINES.
$MACHINES = @{
  'x64'   = 0x8664  # IMAGE_FILE_MACHINE_AMD64
  'arm64' = 0xAA64  # IMAGE_FILE_MACHINE_ARM64
  'x86'   = 0x014C  # IMAGE_FILE_MACHINE_I386
}

if (-not (Test-Path -LiteralPath $Path)) {
  throw "not found: $Path"
}

$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path))
if ($bytes.Length -lt 64 -or $bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) {
  throw "not a PE file (missing MZ signature): $Path"
}

$peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
if ($peOffset -lt 0 -or ($peOffset + 6) -ge $bytes.Length) {
  throw "corrupt PE header offset in $Path"
}
if ($bytes[$peOffset] -ne 0x50 -or $bytes[$peOffset + 1] -ne 0x45 -or
    $bytes[$peOffset + 2] -ne 0x00 -or $bytes[$peOffset + 3] -ne 0x00) {
  throw "missing PE signature in $Path"
}

$machineValue = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
$want = $MACHINES[$Expect]

if ($machineValue -ne $want) {
  # Reverse lookup, only on the failure path.
  $actual = 'unknown'
  foreach ($key in $MACHINES.Keys) {
    if ($MACHINES[$key] -eq $machineValue) { $actual = $key; break }
  }
  $names = $MACHINES.GetEnumerator() | Sort-Object Value |
    ForEach-Object { "$($_.Name)=0x$('{0:X4}' -f $_.Value)" }
  throw ("arch mismatch in {0}: expected {1} (0x{2:X4}) but the PE header says {3} (0x{4:X4}). Known: {5}" -f `
      $Path, $Expect, $want, $actual, $machineValue, ($names -join ', '))
}

Write-Host ("  ok  {0} is {1} (0x{2:X4})" -f $Path, $Expect, $want) -ForegroundColor DarkGreen