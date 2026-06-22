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

# usbipd-win 5.x syntax: the distribution is the VALUE of --wsl (no --distribution flag).
Write-Host "Attaching $busId to WSL distro '$Distribution' ..."
usbipd attach --busid $busId --wsl $Distribution

Write-Host "Done. Inside WSL, verify with:  ls -l /dev/serial/by-id/"
