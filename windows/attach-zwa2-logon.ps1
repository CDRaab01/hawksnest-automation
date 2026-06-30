# Re-attach the Home Assistant Connect ZWA-2 Z-Wave stick into WSL at logon and heal the
# device mount, so the Schlage locks come back automatically after a reboot. This is the
# KNOWN FRAGILE LINK: usbipd attachments do not survive a host reboot.
#
# Runs UNELEVATED: `usbipd attach` needs no admin (the stick is persistently bound), and
# `wsl -u root` is passwordless. Register with register-zwa2-task.ps1 (or schtasks).
$ErrorActionPreference = 'Continue'
$HardwareId = '303a:4001'        # ZWA-2 USB VID:PID (stable; bus id changes across reboots)
$Distro     = 'Dragonfly'

# 1. Find the stick by VID:PID and attach it to WSL (no-op if already attached).
$line = usbipd list | Select-String $HardwareId | Select-Object -First 1
if ($line) {
  $busId = ($line.ToString().Trim() -split '\s+')[0]
  Write-Host "Attaching ZWA-2 at bus id $busId to '$Distro' ..."
  usbipd attach --busid $busId --wsl $Distro 2>$null
} else {
  Write-Host "ZWA-2 ($HardwareId) not found in 'usbipd list' (is it plugged in?)."
}

# 2. Heal the device mount and (re)start zwave-js-ui (the script waits for K3s itself).
wsl -d $Distro -u root -- bash /mnt/c/code/hawksnest-automation/windows/zwave-attach-heal.sh
