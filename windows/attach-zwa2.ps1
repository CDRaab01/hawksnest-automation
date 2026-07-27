#requires -RunAsAdministrator
<#
.SYNOPSIS
  Bind and attach the Home Assistant Connect ZWA-2 USB stick into the WSL2 distro
  so K3s (and the zwave-js-ui pod) can reach it.

.DESCRIPTION
  Selects the device by its stable USB hardware ID (VID:PID), NOT by bus ID, because
  the bus ID can change across reboots/replugs. Idempotent: safe to run repeatedly.

  This is the KNOWN FRAGILE LINK in the chain. `usbipd attach` does not survive a
  host reboot or a physical replug, so register this script as a scheduled task at
  logon (see README-windows.md).

.NOTES
  Requires usbipd-win:  winget install usbipd
#>

param(
  # Confirmed VID:PID for this ZWA-2 (Espressif-based USB stack -> 303a:4001, enumerates
  # as a CDC ACM /dev/ttyACM0). NOTE: 303a is Espressif's vendor ID, which bare ESP32
  # dev boards also use. If you ever have an ESP32 plugged in at the same time, prefer
  # selecting by bus id, since a VID:PID-only match could grab the wrong device.
  [string]$HardwareId = "303a:4001",
  [string]$Distribution = "Dragonfly"
)

$ErrorActionPreference = "Stop"

# `usbipd attach` requires a RUNNING distro; on a cold reboot it is Stopped, which is why
# the attach fails after a restart. Boot it first and wait until WSL reports it running.
# A short detached keepalive holds the distro up across the boot->k3s/systemd handoff so it
# can't idle-shutdown in the window before the attach lands. Idempotent: no-op if running.
function Ensure-DistroRunning {
  param([string]$Distro)
  # WSL emits UTF-16 with embedded NULs under PS 5.1; strip them for a reliable match.
  $isRunning = { (wsl.exe --list --running --quiet) -replace "`0","" |
                   Where-Object { $_.Trim() -eq $Distro } }
  if (& $isRunning) { return }

  Write-Host "WSL distro '$Distro' is not running; starting it before attach ..."
  Start-Process -WindowStyle Hidden -FilePath 'wsl.exe' `
    -ArgumentList @('-d', $Distro, '-u', 'root', '-e', 'sleep', '90')

  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if (& $isRunning) { Write-Host "Distro '$Distro' is running."; return }
  }
  Write-Warning "Distro '$Distro' did not report running within ~20s; attempting attach anyway."
}

Write-Host "Looking for USB device with hardware id $HardwareId ..."
$line = (usbipd list) | Select-String $HardwareId | Select-Object -First 1
if (-not $line) {
  Write-Error "ZWA-2 ($HardwareId) not found in 'usbipd list'. Is it plugged in?"
  exit 1
}

# The bus id is the first whitespace-delimited token on the matching line (e.g. 2-4).
$busId = ($line.ToString().Trim() -split '\s+')[0]
Write-Host "Found ZWA-2 at bus id $busId"

# Bind is persistent and only needs to happen once; re-running it is harmless and
# idempotent, so we always call it rather than parsing the (version-dependent) STATE
# column. Requires admin (enforced by #requires above).
Write-Host "Binding $busId (idempotent) ..."
usbipd bind --busid $busId

# Make sure the target distro is up before attaching (see Ensure-DistroRunning above).
Ensure-DistroRunning -Distro $Distribution

# usbipd-win 5.x syntax: the distribution is the VALUE of --wsl (no --distribution flag).
Write-Host "Attaching $busId to WSL distro '$Distribution' ..."
usbipd attach --busid $busId --wsl $Distribution

Write-Host "Done. Inside WSL, verify with:  ls -l /dev/serial/by-id/"
