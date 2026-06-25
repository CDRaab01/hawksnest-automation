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

Use the committed **`boot.ps1`** — it orchestrates all three recovery steps in order,
each PowerShell step in its own process so one failure can't abort the rest:

1. `attach-zwa2.ps1` — re-attach the ZWA-2 (usbipd).
2. `portproxy-ha.ps1` — re-expose HA on the LAN/Tailscale.
3. `scripts/zwave-usb-recover.sh` (inside Dragonfly, as root) — wait for the stick +
   k3s, **clear any stale by-id directory squat**, recreate the udev symlink, and
   (re)start `zwave-js-ui` so it mounts the real device. This step is what stops the
   `is a directory, cannot open /dev/zwave (ZW0100)` failure after an unclean restart.

Copy the three `.ps1` files into a stable folder (e.g. `C:\ha\`) so `boot.ps1`'s
relative calls resolve; the bash recovery script stays in the repo checkout inside WSL:

```powershell
mkdir C:\ha -Force
copy <repo>\windows\attach-zwa2.ps1  C:\ha\
copy <repo>\windows\portproxy-ha.ps1 C:\ha\
copy <repo>\windows\boot.ps1         C:\ha\
# Sanity check it end-to-end once (elevated):
C:\ha\boot.ps1
# If your WSL username/repo path differ from the defaults:
#   C:\ha\boot.ps1 -Distribution Dragonfly -RepoPathWsl /home/<you>/hawksnest-automation
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
3. In WSL: `ls -l /dev/serial/by-id/` shows the ZWA-2 as a **symlink** → `../../ttyACM0`
   (a *directory* there means the squat happened — `boot.ps1` step 3 should have cleaned
   it; re-run `scripts/zwave-usb-recover.sh` if you ran the steps out of order).
4. The pod sees the real device:
   `kubectl -n home-automation exec deploy/zwave-js-ui -- ls -l /dev/zwave` → `crw-…166, 0`.
5. In Z-Wave JS UI, the controller is online and all locks report state — **no
   re-pairing required**.
6. HA is reachable at `http://<PC-LAN-IP>:8123` and over Tailscale.
