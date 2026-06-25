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

Register it to run **at startup** with highest privileges. Use `-AtStartup`, **not**
`-AtLogOn`: if the PC reboots and nobody interactively logs in, an `-AtLogOn` task never
fires, so the portproxy/USB attach are never re-created and HA looks dead even though the
pods are healthy. Running at startup requires a principal that can run whether or not a user
is logged on (`SYSTEM`), so the task can re-create the portproxy and re-attach the stick on
a headless reboot:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\ha\boot.ps1"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "HomeAssistant-Boot" `
  -Action $action -Trigger $trigger -Principal $principal
```

> Note: `usbipd attach` from a `SYSTEM`-context task can be finicky depending on your
> usbipd-win version. If the Z-Wave stick doesn't attach on a headless boot, keep an
> additional `-AtLogOn` task (run as your user, `-RunLevel Highest`) for `attach-zwa2.ps1`
> specifically, and leave `portproxy-ha.ps1` on the `-AtStartup`/`SYSTEM` task — the
> network bridge is the part that must come up without a logon.

## "HA unreachable after a reboot" — troubleshooting

This is the single most common failure. Symptom: `http://<PC-LAN-IP>:8123` doesn't load
from the phone **or** the PC, but `kubectl get pods -n home-automation` shows everything
`Running`. That means the cluster is fine and the **Windows→WSL2 bridge is stale** — WSL2
got a new IP on reboot and the `netsh portproxy` still points at the old one.

Two tell-tale fingerprints of a recent reboot:
- Pods show recent `RESTARTS ... (Nh ago)`.
- The HA log spams `DNS server returned general failure` (e.g. for `api.ring.com`) —
  WSL2 regenerated its networking and CoreDNS's upstream resolver is stale.

Fix, in an **elevated** PowerShell:

```powershell
# 1. Re-create the portproxy against the live WSL2 IP (or just run heal-ha.ps1):
.\portproxy-ha.ps1

# 2. Verify the mapping now matches the live WSL2 IP:
wsl -d Dragonfly -- hostname -I          # current WSL2 IP
netsh interface portproxy show v4tov4    # connectaddress should equal the IP above

# 3. Re-attach the Z-Wave stick (the same reboot usually drops it):
.\attach-zwa2.ps1                        # then in WSL: ls -l /dev/serial/by-id/

# 4. Clear the stale in-pod DNS (fixes the Ring "DNS server returned general failure"):
wsl --shutdown                           # then let WSL/K3s/the boot task come back
#   …or, without bouncing WSL:
#   kubectl -n kube-system rollout restart deploy coredns
#   kubectl -n home-automation rollout restart deploy home-assistant
```

`heal-ha.ps1` automates steps 1–2: it compares the live WSL2 IP to the current portproxy
target and re-creates the mapping only if they differ. Safe to run anytime, and a good
candidate for a periodic scheduled task.

If the portproxy already matches the live IP but HA is still unreachable, check the firewall
rule (`HomeAssistant-8123`) and confirm the NodePort answers from inside WSL:
`curl -sI http://localhost:30123`.

## Reboot drill (acceptance check)

1. Reboot the Windows host.
2. Let the scheduled task run (or run `boot.ps1` manually).
3. In WSL: `ls -l /dev/serial/by-id/` shows the ZWA-2.
4. In Z-Wave JS UI, the controller is online and all locks report state — **no
   re-pairing required**.
5. HA is reachable at `http://<PC-LAN-IP>:8123` and over Tailscale.
