<#
.SYNOPSIS
  One-shot ELEVATED finalizer for the Home Assistant / Z-Wave reboot-recovery chain under
  WSL mirrored networking. Run once (Run as administrator).

.DESCRIPTION
  Does the two things that require admin and can't be done from an unelevated session:

    1. Removes any stale netsh portproxy on 0.0.0.0:8123. Under mirrored networking WSL shares
       the host port space, so a leftover NAT-era portproxy squatting on :8123 blocks socat
       (ha-forwarder.service) from binding -> `bind(0.0.0.0:8123): Address already in use`,
       which silently breaks host/LAN/Tailscale HA access. Diagnosed 2026-07-06 (socat had
       failed to bind 67 times). Then restarts + verifies the forwarder and host reachability.

    2. Registers the logon recovery tasks (keepalive + ZWA-2 attach) via
       register-recovery-tasks.ps1, so a reboot brings everything back with no manual steps.

  Idempotent: safe to re-run.
#>
$ErrorActionPreference = 'Stop'
$dir = 'C:\code\hawksnest-automation\windows'

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) { Write-Error "Run this from an ELEVATED PowerShell (Run as administrator)."; exit 1 }

# 1. Free :8123 for the socat forwarder.
Write-Host "Removing any stale netsh portproxy on 0.0.0.0:8123 ..."
netsh interface portproxy delete v4tov4 listenport=8123 listenaddress=0.0.0.0 2>$null | Out-Null

Write-Host "Restarting ha-forwarder.service ..."
wsl.exe -d Dragonfly -u root -e systemctl restart ha-forwarder.service
Start-Sleep -Seconds 3
$state = (wsl.exe -d Dragonfly -u root -e systemctl is-active ha-forwarder.service).Trim()
Write-Host "  ha-forwarder.service is now: $state"

# Verify HA is reachable on the host via the forwarder.
try {
  $code = (Invoke-WebRequest 'http://127.0.0.1:8123' -TimeoutSec 8 -UseBasicParsing).StatusCode
  Write-Host "  http://127.0.0.1:8123 -> HTTP $code  (host HA access OK)"
} catch {
  Write-Warning "  http://127.0.0.1:8123 not answering yet: $($_.Exception.Message)"
  Write-Warning "  (If HA's pod is still starting, give it a minute and re-test.)"
}

# 2. Register the reboot-recovery logon tasks.
Write-Host ""
Write-Host "Registering logon recovery tasks ..."
& (Join-Path $dir 'register-recovery-tasks.ps1')

Write-Host ""
Write-Host "Finalized. Reboot-recovery is now automatic. Quick verification:"
Write-Host "  Get-ScheduledTask HA-KeepWSL-Alive,Attach-ZWA2-WSL | Format-Table TaskName,State"
Write-Host "  Invoke-WebRequest http://127.0.0.1:8123 -UseBasicParsing | Select StatusCode"
