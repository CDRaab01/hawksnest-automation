#requires -RunAsAdministrator
<#
.SYNOPSIS
  Boot/logon orchestrator for the Hawksnest stack on the Windows host.

.DESCRIPTION
  Re-establishes everything that does NOT survive a reboot/replug, in order:
    1. attach-zwa2.ps1   — attach the ZWA-2 USB stick into WSL2 (usbipd).
    2. portproxy-ha.ps1  — expose Home Assistant on the LAN/Tailscale.
    3. zwave-usb-recover.sh (inside Dragonfly, as root) — wait for the stick + k3s,
       clear any stale by-id directory "squat", recreate the udev symlink, and
       (re)start zwave-js-ui so it mounts the REAL device (not an empty dir).

  Steps 1 and 2 each run in their OWN powershell process so that if one fails
  (e.g. the stick isn't plugged in yet, which makes attach-zwa2.ps1 exit non-zero)
  it does NOT abort the rest. Step 3 is what makes Z-Wave reliable after a reboot.

  Register this at logon with Task Scheduler (see README-windows.md).

.NOTES
  Put the three .ps1 files together (e.g. C:\ha\) so the $here-relative calls resolve.
  The bash recovery script stays in the repo checkout inside WSL (RepoPathWsl).
#>
param(
  [string]$Distribution = "Dragonfly",
  # Path to THIS repo's checkout INSIDE the WSL distro.
  [string]$RepoPathWsl  = "/home/sonic/hawksnest-automation"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== [1/3] Attaching ZWA-2 (usbipd) ==="
$attach = Join-Path $here "attach-zwa2.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File $attach

Write-Host "=== [2/3] Exposing Home Assistant (portproxy) ==="
$proxy = Join-Path $here "portproxy-ha.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File $proxy

Write-Host "=== [3/3] Recovering Z-Wave device inside $Distribution ==="
# Run as root so rm/udevadm need no password prompt; the script itself waits for
# the stick and the k3s API before acting, so ordering vs. K3s startup is handled.
wsl -d $Distribution -u root -- bash -lc "$RepoPathWsl/scripts/zwave-usb-recover.sh"

Write-Host "=== boot.ps1 complete ==="
