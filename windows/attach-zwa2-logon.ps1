# Re-attach the Home Assistant Connect ZWA-2 Z-Wave stick into WSL at logon and heal the
# device mount, so the Schlage locks come back automatically after a reboot. This is the
# KNOWN FRAGILE LINK: usbipd attachments do not survive a host reboot.
#
# Runs UNELEVATED: `usbipd attach` needs no admin (the stick is persistently bound), and
# `wsl -u root` is passwordless. Register with register-zwa2-task.ps1 (or schtasks).
$ErrorActionPreference = 'Continue'
$HardwareId = '303a:4001'        # ZWA-2 USB VID:PID (stable; bus id changes across reboots)
$Distro     = 'Dragonfly'

# usbipd attach requires a RUNNING distro. At logon after a cold reboot the distro is
# Stopped, so the attach below silently failed (2>$null) and the locks never came back.
# Boot it first and wait until it reports running; a short detached keepalive bridges the
# window before k3s/systemd holds it up. No-op if already running.
function Ensure-DistroRunning {
  param([string]$Distro)
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
  Write-Warning "Distro '$Distro' did not report running within ~20s; attaching anyway."
}

# 1. Find the stick by VID:PID and attach it to WSL.
#
# The attach is preceded by a DETACH, and that is load-bearing after a
# `wsl --shutdown` — do not "optimise" it away as redundant.
#
# Why: `usbipd attach` is a no-op when usbipd already believes the device is
# attached, and a `wsl --shutdown` tears down the WSL side WITHOUT usbipd
# noticing. usbipd keeps reporting STATE=Attached while the distro has no
# /dev/ttyACM0 at all, so a plain attach silently does nothing and the stick
# never comes back. The stale /dev/serial/by-id symlink left pointing at the
# missing ttyACM0 then makes containerd fail the pod with:
#   failed to generate spec: failed to mkdir ".../usb-Nabu_Casa_ZWA-2_...-if00":
#   file exists
# and zwave-js-ui sits in CreateContainerError with the locks offline.
#
# Observed 2026-07-29: this task ran on its 3-minute schedule and exited 0
# repeatedly while the locks stayed down, precisely because attach was a no-op.
# A detach first forces usbipd to rebuild the binding.
$line = usbipd list | Select-String $HardwareId | Select-Object -First 1
if ($line) {
  $busId = ($line.ToString().Trim() -split '\s+')[0]
  Ensure-DistroRunning -Distro $Distro
  # Detach is best-effort: it fails harmlessly when nothing is attached.
  Write-Host "Detaching ZWA-2 at bus id $busId (clears any stale binding) ..."
  usbipd detach --busid $busId 2>$null
  Start-Sleep -Seconds 2
  Write-Host "Attaching ZWA-2 at bus id $busId to '$Distro' ..."
  usbipd attach --busid $busId --wsl $Distro 2>$null
} else {
  Write-Host "ZWA-2 ($HardwareId) not found in 'usbipd list' (is it plugged in?)."
}

# 2. Heal the device mount and (re)start zwave-js-ui (the script waits for K3s itself).
wsl -d $Distro -u root -- bash /mnt/c/code/hawksnest-automation/windows/zwave-attach-heal.sh
