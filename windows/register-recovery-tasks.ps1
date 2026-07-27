<#
.SYNOPSIS
  Register the full Home Assistant / Z-Wave reboot-recovery chain as headless-capable (S4U)
  scheduled tasks, so a host reboot brings HA + the Schlage locks back with zero manual steps
  EVEN on a headless crash-reboot where nobody logs in.

.DESCRIPTION
  Registers two tasks (current user, S4U) that recover the stack after a reboot under the
  deliberate mirrored-networking design:

    1. HA-KeepWSL-Alive  (no time limit)  -- keep-wsl-alive.ps1
         Holds the Dragonfly WSL2 instance up 24/7. THE load-bearing piece: without it the
         instance tears down ~15s after any wsl.exe process exits and k3s/HA/Z-Wave (and the
         in-distro ha-forwarder.service that gives the host HA access) die with it. Everything
         else depends on the distro being up, so this must exist. Triggers: logon + startup.

    2. Attach-ZWA2-WSL   (5-min limit, 3-min watchdog) -- attach-zwa2-logon.ps1
         Re-attaches the ZWA-2 USB stick into WSL (usbipd attachments don't survive reboot)
         and heals the zwave-js-ui device mount. Triggers: logon + startup + a 3-min repetition
         watchdog, so a first post-boot attempt that races WSL/k3s startup (or a mid-session USB
         drop) self-heals without a human.

  WHY S4U (changed 2026-07-20): the old registration ran as the current user with the DEFAULT
  Interactive logon type, so every task only fired on an INTERACTIVE logon. The RAM-fault
  shutdowns on this host are *headless* reboots where nobody logs in, so recovery never ran and
  the Schlage locks sat dead until a human intervened (41h, 2026-07-20). S4U ("run whether
  logged on or not", no stored password) fires on a headless boot while still running in the
  user's context, so `wsl -d Dragonfly` targets the user's instance -- not a stray SYSTEM one,
  which is why SYSTEM-context attach is unreliable.

  NOT registered: HA-PortProxy / portproxy-ha.ps1. Under mirrored networking the netsh portproxy
  approach is dead (mirrored can't surface k3s NodePorts); HA host/LAN/Tailscale access is handled
  inside the distro by ha-forwarder.service (socat :8123 -> :30123) plus the Hyper-V firewall rule
  'HomeAssistant-8123', both already installed.

  Re-run any time to update (uses -Force). Remove with:
    'HA-KeepWSL-Alive','Attach-ZWA2-WSL' |
      ForEach-Object { Unregister-ScheduledTask -TaskName $_ -Confirm:$false }
#>
$ErrorActionPreference = 'Stop'
$dir = 'C:\code\hawksnest-automation\windows'

# Registering S4U scheduled tasks in the Task Scheduler root folder requires an ELEVATED session
# (S4U also needs the "Log on as a batch job" right, which elevation grants). Fail early with a
# clear message rather than emitting a misleading "Access is denied" per task.
$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
  Write-Error "This script must be run from an ELEVATED PowerShell (Run as administrator). Nothing was registered."
  exit 1
}

function New-PsAction([string]$script) {
  $arg = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $dir $script) + '"'
  New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
}

# Run as the owner without an interactive logon. Registered elevated as the owner, so
# USERDOMAIN\USERNAME is the right identity (this is a single-owner host: DRAGONFLY\Sonic).
$userId    = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited

# Fire on a real logon AND a headless boot.
$tLogon = New-ScheduledTaskTrigger -AtLogOn
$tBoot  = New-ScheduledTaskTrigger -AtStartup
# Attach watchdog: additionally retry every 3 min, indefinitely. Idempotent, so harmless when
# there is nothing to do (attach = no-op if already attached; heal = no-op if the device is clean).
# Built via the CIM repetition pattern (Interval only, no Duration = "repeat indefinitely");
# New-ScheduledTaskTrigger's -RepetitionDuration rejects TimeSpan::MaxValue as out-of-range.
$tWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date)
$tWatchdog.Repetition = New-CimInstance -ClassName MSFT_TaskRepetitionPattern `
  -Namespace Root/Microsoft/Windows/TaskScheduler `
  -Property @{ Interval = 'PT3M'; StopAtDurationEnd = $false } -ClientOnly

$shortSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
# Keepalive runs forever: no execution time limit, restart if it ever exits, one instance.
$keepSet  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
              -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
              -MultipleInstances IgnoreNew

$tasks = @(
  @{ Name = 'HA-KeepWSL-Alive'; Script = 'keep-wsl-alive.ps1'; Settings = $keepSet;
     Triggers = @($tLogon, $tBoot);
     Desc = 'Hold the Dragonfly WSL2 instance up 24/7 (headless-capable, S4U) so k3s/HA/Z-Wave stay running' }
  @{ Name = 'Attach-ZWA2-WSL';  Script = 'attach-zwa2-logon.ps1'; Settings = $shortSet;
     Triggers = @($tLogon, $tBoot, $tWatchdog);
     Desc = 'Attach the ZWA-2 stick into WSL + heal zwave-js-ui; headless watchdog (S4U; logon+startup+3min)' }
)

foreach ($t in $tasks) {
  Register-ScheduledTask -TaskName $t.Name -Description $t.Desc `
    -Action (New-PsAction $t.Script) -Trigger $t.Triggers -Settings $t.Settings `
    -Principal $principal -Force | Out-Null
  Write-Host ("Registered '{0}' (S4U; {1} trigger(s))." -f $t.Name, $t.Triggers.Count)
}

Write-Host ""
Write-Host "Done. Reboot recovery is now automatic at BOOT (headless-capable) + a 3-min watchdog. Verify with:"
Write-Host "  Get-ScheduledTask HA-KeepWSL-Alive,Attach-ZWA2-WSL | Format-Table TaskName,State"
