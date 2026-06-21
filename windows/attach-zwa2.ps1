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
  # Default matches Silicon Labs CP210x / ZWA-2. Confirm with `usbipd list` and override
  # if your stick reports a different VID:PID.
  [string]$HardwareId = "10c4:ea60",
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

# Bind is persistent and only needs to happen once, but calling it again is harmless.
if ($line -notmatch "Shared") {
  Write-Host "Binding $busId (one-time share) ..."
  usbipd bind --busid $busId
}

Write-Host "Attaching $busId to WSL distro '$Distribution' ..."
usbipd attach --wsl --busid $busId --distribution $Distribution

Write-Host "Done. Inside WSL, verify with:  ls -l /dev/serial/by-id/"
