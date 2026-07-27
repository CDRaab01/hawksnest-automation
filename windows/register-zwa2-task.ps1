# Register JUST the ZWA-2 re-attach as a headless watchdog scheduled task. Prefer
# register-recovery-tasks.ps1 (registers this + the WSL keepalive together); this standalone
# variant is kept for re-registering the attach task alone.
#
# Runs S4U ("run whether logged on or not", no stored password) so it fires on a HEADLESS
# crash-reboot where nobody logs in -- the failure that left the Schlage locks dead for 41h
# on 2026-07-20. Needs an ELEVATED session (S4U registration requires admin). Re-run to update.
# Remove with:  Unregister-ScheduledTask -TaskName 'Attach-ZWA2-WSL' -Confirm:$false
$ErrorActionPreference = 'Stop'

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
  Write-Error "This script must be run from an ELEVATED PowerShell (Run as administrator). Nothing was registered."
  exit 1
}

$script = 'C:\code\hawksnest-automation\windows\attach-zwa2-logon.ps1'
$arg    = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

# logon + headless startup + a 3-min watchdog (idempotent scripts, so retrying is harmless).
$tLogon    = New-ScheduledTaskTrigger -AtLogOn
$tBoot     = New-ScheduledTaskTrigger -AtStartup
# Interval only, no Duration = repeat indefinitely (TimeSpan::MaxValue is rejected as out-of-range).
$tWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date)
$tWatchdog.Repetition = New-CimInstance -ClassName MSFT_TaskRepetitionPattern `
  -Namespace Root/Microsoft/Windows/TaskScheduler `
  -Property @{ Interval = 'PT3M'; StopAtDurationEnd = $false } -ClientOnly

$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
         -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName 'Attach-ZWA2-WSL' `
  -Description 'Attach the ZWA-2 stick into WSL + heal zwave-js-ui; headless watchdog (S4U; logon+startup+3min)' `
  -Action $action -Trigger $tLogon,$tBoot,$tWatchdog -Settings $set -Principal $principal -Force | Out-Null
Write-Host "Registered headless watchdog task 'Attach-ZWA2-WSL' (S4U; logon+startup+3-min)."
