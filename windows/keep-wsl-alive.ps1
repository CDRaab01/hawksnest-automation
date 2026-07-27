<#
.SYNOPSIS
  Hold the Dragonfly WSL2 instance up 24/7 so K3s -> Home Assistant -> Z-Wave (the Schlage
  door locks) stay running continuously.

.DESCRIPTION
  On this host the Dragonfly instance tears down ~15s after the last wsl.exe process exits,
  even though /etc/wsl.conf sets systemd=true and k3s.service is enabled+running (verified
  2026-07-06: after a one-shot boot with nothing holding it, the distro was Stopped within
  15s and k3s cold-started on every subsequent access). systemd/k3s do NOT keep the instance
  alive on their own here.

  Fix: keep a single blocking process (`sleep infinity`) running inside the distro. A live
  wsl.exe-launched process is enough to keep the instance (and thus k3s/HA/Z-Wave) up. This
  script holds that process and re-establishes it if it ever drops (host `wsl --shutdown`,
  VM restart, crash).

  Runs UNELEVATED (WSL runs in the user's context; `wsl -u root` is passwordless). Register
  as a logon scheduled task with NO execution time limit -- see register-recovery-tasks.ps1.

.NOTES
  This is the load-bearing piece of the reboot-recovery chain: without it, attach-zwa2 and
  portproxy-ha both target a distro that dies seconds later.
#>
param([string]$Distribution = 'Dragonfly')
$ErrorActionPreference = 'Continue'

Write-Host "keep-wsl-alive: holding '$Distribution' up (Ctrl-C or task-stop to release)."
while ($true) {
  # A single live wsl-launched process keeps the instance up. Kept deliberately minimal:
  # `sleep infinity` as the default user avoids the degraded root systemd-user session and
  # any /bin/sh path-translation quirks. Blocks until the hold drops (wsl --shutdown, VM
  # restart, crash), then re-establishes it after a short backoff.
  wsl.exe -d $Distribution -e sleep infinity
  Write-Host "keep-wsl-alive: hold dropped for '$Distribution'; re-establishing in 5s ..."
  Start-Sleep -Seconds 5
}
