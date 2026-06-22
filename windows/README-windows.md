# Windows host setup (USB passthrough + HA network exposure)

Two things must happen on the Windows 11 host on every boot, because neither survives
a reboot/replug on its own:

1. **Attach the ZWA-2 USB stick into WSL2** (`attach-zwa2.ps1`).
2. **Proxy the HA NodePort onto the LAN/Tailscale** (`portproxy-ha.ps1`).

> ⚠️ **This is the fragile link in the whole system.** If Z-Wave "goes dead" or HA is
> unreachable after a reboot, 90% of the time it's because one of these two steps did
> not run. Check here first.

## One-time prerequisites

```powershell
winget install usbipd            # USB/IP for Windows
usbipd list                      # confirm the ZWA-2 appears (note its VID:PID)
```

Confirm the WSL distro name is `Dragonfly` (`wsl -l -v`). If different, pass
`-Distribution <name>` to both scripts.

## Run manually (as Administrator)

```powershell
# from this folder, in an elevated PowerShell
.\attach-zwa2.ps1
.\portproxy-ha.ps1
```

Then inside WSL verify the device symlink:

```bash
ls -l /dev/serial/by-id/
```

Copy the `usb-...-if00` path into `kustomize/zwave-js-ui/deployment.yaml`
(the `hostPath.path` placeholder).

## Make it survive reboots (scheduled task)

Create a combined boot script, e.g. `C:\ha\boot.ps1`. Run each script in its **own**
`powershell -File` process so that if one fails (e.g. `attach-zwa2.ps1` when the stick
isn't plugged in yet, which exits non-zero), it does **not** abort the rest — the
portproxy still gets set up:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\ha\attach-zwa2.ps1"
Start-Sleep -Seconds 5
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\ha\portproxy-ha.ps1"
```

Register it to run at logon with highest privileges:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\ha\boot.ps1"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -RunLevel Highest
Register-ScheduledTask -TaskName "HomeAssistant-Boot" `
  -Action $action -Trigger $trigger -Principal $principal
```

## Reboot drill (acceptance check)

1. Reboot the Windows host.
2. Let the scheduled task run (or run `boot.ps1` manually).
3. In WSL: `ls -l /dev/serial/by-id/` shows the ZWA-2.
4. In Z-Wave JS UI, the controller is online and all locks report state — **no
   re-pairing required**.
5. HA is reachable at `http://<PC-LAN-IP>:8123` and over Tailscale.
