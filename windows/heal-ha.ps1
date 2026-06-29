#requires -RunAsAdministrator
<#
.SYNOPSIS
  Detect and repair a stale Home Assistant portproxy after a WSL2 IP change.

.DESCRIPTION
  HA is reached on the LAN via a netsh portproxy (0.0.0.0:<ListenPort> -> <wsl-ip>:<NodePort>)
  set up by portproxy-ha.ps1. WSL2's IP changes on reboot, so that mapping goes stale and HA
  becomes unreachable even though all pods are Running. This is the #1 "HA is down after a
  reboot" cause (see README-windows.md).

  This script is idempotent and self-healing:
    1. Reads the live WSL2 IP for the distro.
    2. Reads the current portproxy connectaddress for <ListenPort>.
    3. If they match, reports PASS and changes nothing.
    4. If they differ (or no proxy exists), re-invokes portproxy-ha.ps1 to fix it, then
       re-verifies and reports PASS/FAIL.

  Safe to run anytime and a good candidate for a periodic scheduled task. Exit code 0 on
  PASS, 1 on FAIL.

.NOTES
  Run elevated. Lives next to portproxy-ha.ps1 and calls it for the actual repair.
#>

param(
  [int]$ListenPort = 8123,     # port clients use on the LAN / Tailscale
  [int]$NodePort   = 30123,    # must match home-assistant Service nodePort
  [string]$Distribution = "Dragonfly"
)

$ErrorActionPreference = "Stop"

function Get-WslIp {
  param([string]$Distro)
  $ip = (wsl -d $Distro -- hostname -I).Trim().Split(" ")[0]
  if (-not $ip) { throw "Could not determine WSL2 IP for '$Distro'. Is the distro running?" }
  return $ip
}

function Get-ProxyTarget {
  # Returns the connectaddress currently mapped for $ListenPort, or $null if none.
  param([int]$Port)
  $lines = netsh interface portproxy show v4tov4
  foreach ($line in $lines) {
    # Columns: Address  Port  Address  Port  (listenaddr listenport connectaddr connectport)
    $cols = ($line.Trim() -split '\s+')
    if ($cols.Count -ge 4 -and $cols[1] -eq "$Port" -and $cols[2] -match '^\d+\.\d+\.\d+\.\d+$') {
      return $cols[2]
    }
  }
  return $null
}

$wslIp = Get-WslIp -Distro $Distribution
$target = Get-ProxyTarget -Port $ListenPort
Write-Host "Live WSL2 ($Distribution) IP : $wslIp"
Write-Host "Portproxy $ListenPort -> target : $(if ($target) { $target } else { '<none>' })"

if ($target -eq $wslIp) {
  Write-Host "PASS: portproxy already points at the live WSL2 IP. No change."
  exit 0
}

Write-Warning "Mismatch (or missing proxy). Re-creating the mapping..."
& "$PSScriptRoot\portproxy-ha.ps1" -ListenPort $ListenPort -NodePort $NodePort -Distribution $Distribution

# Re-verify after the repair.
$wslIp = Get-WslIp -Distro $Distribution
$target = Get-ProxyTarget -Port $ListenPort
if ($target -eq $wslIp) {
  Write-Host "PASS: portproxy now points at $wslIp."
  exit 0
} else {
  Write-Error "FAIL: portproxy target is '$target' but live WSL2 IP is '$wslIp'."
  exit 1
}
