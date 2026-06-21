#requires -RunAsAdministrator
<#
.SYNOPSIS
  Expose the Home Assistant NodePort (running in K3s-in-WSL2) on the Windows host's
  LAN IP, so other devices on the network (and Tailscale) can reach HA.

.DESCRIPTION
  WSL2 gets a private, changing IP on a NAT'd virtual switch. This script:
    1. Discovers the current WSL2 IP.
    2. Creates a netsh portproxy from 0.0.0.0:<ListenPort> -> <wsl-ip>:<NodePort>.
    3. Opens a Windows Firewall rule for <ListenPort>.

  Because the WSL2 IP changes on reboot, this must be re-run at boot (it deletes and
  recreates the proxy each time). Register it as a scheduled task alongside
  attach-zwa2.ps1 (see README-windows.md).

  Tailscale: with this proxy in place, HA is reachable at http://<this-PC-tailscale-ip>:<ListenPort>.
  No ports are forwarded to the public internet.
#>

param(
  [int]$ListenPort = 8123,    # port clients use on the LAN / Tailscale
  [int]$NodePort   = 30123,   # must match home-assistant Service nodePort
  [string]$Distribution = "Dragonfly"
)

$ErrorActionPreference = "Stop"

# Resolve the current WSL2 IP (eth0 inside the distro).
$wslIp = (wsl -d $Distribution -- hostname -I).Trim().Split(" ")[0]
if (-not $wslIp) { Write-Error "Could not determine WSL2 IP for '$Distribution'."; exit 1 }
Write-Host "WSL2 ($Distribution) IP: $wslIp"

# Reset any prior mapping on this listen port, then (re)create it.
netsh interface portproxy delete v4tov4 listenport=$ListenPort listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 `
  listenport=$ListenPort listenaddress=0.0.0.0 `
  connectport=$NodePort connectaddress=$wslIp
Write-Host "portproxy: 0.0.0.0:$ListenPort -> ${wslIp}:$NodePort"

# Firewall rule (idempotent).
$ruleName = "HomeAssistant-$ListenPort"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $ListenPort | Out-Null
  Write-Host "Created firewall rule '$ruleName'."
}

Write-Host "Current portproxy table:"
netsh interface portproxy show v4tov4
