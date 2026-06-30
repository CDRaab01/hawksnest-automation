# Register the ZWA-2 re-attach as a logon Task Scheduler task. Runs UNELEVATED (the task
# needs no admin), so no UAC. Re-run to update. Remove with:
#   Unregister-ScheduledTask -TaskName 'Attach-ZWA2-WSL' -Confirm:$false
$ErrorActionPreference = 'Stop'
$script = 'C:\code\hawksnest-automation\windows\attach-zwa2-logon.ps1'
$arg = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"'
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
$trigger = New-ScheduledTaskTrigger -AtLogOn
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'Attach-ZWA2-WSL' `
  -Description 'Attach the ZWA-2 Z-Wave stick into WSL and heal zwave-js-ui after a reboot' `
  -Action $action -Trigger $trigger -Settings $set -RunLevel Limited -Force | Out-Null
Write-Host "Registered logon task 'Attach-ZWA2-WSL'."
