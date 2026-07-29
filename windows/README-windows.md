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

---

## ⚠️ Mirrored networking (current setup — supersedes the portproxy steps above)

WSL2 here runs `networkingMode=mirrored` (set deliberately to reduce remote-control / WSL
crashes). That **breaks the netsh portproxy + `heal-ha.ps1` approach above** — there is no
NAT-era `172.x` WSL IP for the proxy to target anymore. Use the following instead.

> **Do NOT switch back to NAT mode to "fix" host access.** Mirrored is deliberate; the socat
> forwarder handles exposure. Reverting requires a `wsl --shutdown` that bounces the entire
> Docker Desktop backend (the whole app suite + media stack), and it is unnecessary.

### Reboot recovery — the load-bearing piece is the WSL keepalive (added 2026-07-06)

After a host reboot the whole chain (k3s → HA → Z-Wave, **and** `ha-forwarder.service`) only
works while the **Dragonfly WSL2 instance stays running** — and it does **not** stay up on its
own: the instance tears down ~15s after the last `wsl.exe` process exits, even with
`systemd=true` and k3s enabled. (This was previously masked by the WSL Actions runner holding
the instance open; after a reboot nothing did, so everything looked dead — pods, forwarder, and
Z-Wave all gone at once.) `keep-wsl-alive.ps1` holds it up 24/7 and is registered as a logon
task by `register-recovery-tasks.ps1`.

**One-command setup (run once, elevated):**
```powershell
& 'C:\code\hawksnest-automation\windows\finalize-mirrored-recovery.ps1'
```
This (1) removes any stale netsh portproxy on `:8123`, (2) restarts + verifies
`ha-forwarder.service`, and (3) registers the logon recovery tasks — `HA-KeepWSL-Alive`
(keepalive, no time limit) and `Attach-ZWA2-WSL` (USB attach + heal). It deliberately does
**not** register an `HA-PortProxy` task (dead under mirrored mode).

**Two gotchas this setup handles (both bit us 2026-07-06):**
- **netsh `:8123` vs socat.** Under mirrored mode WSL shares the host port space, so a leftover
  NAT-era `netsh portproxy` on `0.0.0.0:8123` makes socat fail with
  `bind(:8123): Address already in use` and HA host access silently breaks (socat had retried
  67×). The finalizer deletes it. Don't re-create it; don't run `portproxy-ha.ps1` under
  mirrored mode.
- **`cdc_acm` not auto-loaded.** On a fresh WSL kernel the ZWA-2's CDC-ACM driver isn't loaded,
  so the stick attaches (usbipd shows `Attached`) but never enumerates as `/dev/ttyACM0` and the
  `by-id` symlink can't be built. `/etc/modules-load.d/cdc-acm.conf` loads it at boot;
  `zwave-attach-heal.sh` also `modprobe`s it defensively.

**HA host/LAN reachability — `ha-forwarder.service` (socat).** Mirrored mode only forwards the
Windows host to *real listening sockets* in WSL; a K3s NodePort is nft DNAT with no socket, so
the host can't reach `:30123`. A socat real-socket forwarder bridges it:

```bash
# in the Dragonfly distro
sudo apt-get update && sudo apt-get install -y socat
sudo cp windows/ha-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ha-forwarder.service
```

LAN/Tailscale access additionally needs a one-time (elevated) Hyper-V firewall allow for 8123:

```powershell
New-NetFirewallHyperVRule -Name 'HomeAssistant-8123' -DisplayName 'HomeAssistant-8123' `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 8123 -Action Allow
```

**go2rtc WebRTC media — `go2rtc-forwarder.service` (socat).** Same pattern for the two-way
"talk" / low-latency live media port: socat `:8555` → the `go2rtc-webrtc` NodePort `30855`
(the ICE candidate go2rtc advertises is `GO2RTC_HOST_IP:8555`). Install identically:

```bash
sudo cp windows/go2rtc-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now go2rtc-forwarder.service
```

```powershell
New-NetFirewallHyperVRule -Name 'Go2rtc-8555' -DisplayName 'Go2rtc-8555' `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 8555 -Action Allow
```

> **Note (2026-07-29): the `Go2rtc-8555` rule above does not actually exist** on this host —
> `Get-NetFirewallHyperVRule` lists only `HomeAssistant-8123` (plus the WSL defaults and the two
> `WSL-*` mirrored rules). go2rtc works anyway via the socat forwarder. Create it if you need
> LAN/Tailscale WebRTC; just don't assume it's already there.

**LM Studio for Frigate — `lmstudio-fwd.service` (socat, and it runs the OTHER way).** The two
forwarders above expose a *pod* to the Windows host. This one exposes a *Windows service to the
pods*, for Frigate's GenAI event descriptions. It exists because under mirrored networking there
is otherwise **no path at all**, and the reason is worth reading before you debug it:

- The Dragonfly VM **owns** the host address. Inside the distro, `ip route get 192.168.4.34`
  returns `local … dev lo`, so a pod dialing `192.168.4.34` reaches the VM, not Windows — and
  nothing listens on 1234 there.
- **This is not a firewall problem.** Several firewall rules were tried first (a scoped
  `New-NetFirewallRule`, then a `New-NetFirewallHyperVRule` on the WSL `VMCreatorId`) and none of
  them could have worked, because the packets never leave the VM. If you find those rules lying
  around, they're inert; that's why.
- The VM *does* reach Windows, on `127.0.0.1` (mirrored loopback). A pod can't use that, because
  its `127.0.0.1` is the pod. socat is the only bridge.

```
pod -> 10.42.0.1:21234 -> lmstudio-fwd -> 127.0.0.1:1234 -> LM Studio on Windows
```

```bash
# in the Dragonfly distro
sudo cp windows/lmstudio-fwd.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lmstudio-fwd.service
```

Two constraints that look arbitrary and are not:

- **Port 21234, not 1234.** Mirrored networking mirrors every Windows *listening* socket into the
  VM, so `bind()` on 1234 fails with `Address already in use` while `ss` shows nothing bound.
  11434 collides the same way (Ollama on Windows). A replacement port must be free **on Windows**.
- **`bind=10.42.0.1` (cni0) is the access control.** The LM Studio API is unauthenticated;
  widening this to `0.0.0.0` would publish a free GPU to the whole LAN.

No Hyper-V firewall rule is needed — the traffic never crosses that boundary.

Requires LM Studio bound to all interfaces on the Windows side:

```powershell
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" server start --bind 0.0.0.0 --port 1234
```

That persists in `~/.lmstudio/.internal/http-server-config.json` as `"networkInterface"`. Don't go
looking for the GUI toggle — it's easily confused with **"Enable Local LLM Service (headless)"**
in App Settings, which is a different setting and does nothing for binding.


**Frigate admin UI — `frigate-forwarder.service` (socat) + Tailscale Serve `:8447`.** Motion masks,
zones and object filters are drawn on a live frame in Frigate's own UI, and an untuned camera fires
on a TV or a ceiling fan — every false alert is another junk CLIP embedding in the semantic search
index. Same forwarder pattern as HA and go2rtc:

```
Tailscale Serve https://dragonfly.tail2ce561.ts.net:8447
  -> 127.0.0.1:8971   (frigate-forwarder.service, socat)
    -> NodePort 30897 (Service frigate-ui)
```

```bash
# in the Dragonfly distro
sudo cp windows/frigate-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now frigate-forwarder.service
```

```powershell
# one-time, and RUN `tailscale serve status` FIRST — see the warning below
tailscale serve --bg --https=8447 http://127.0.0.1:8971
```

No Hyper-V firewall rule is needed for tailnet access; Tailscale Serve terminates on the host.

> **⚠️ Check `tailscale serve status` before adding any Serve mapping.** Serve ports are a shared
> namespace across the whole suite and `tailscale serve --https=<port>` **silently overwrites** an
> existing mapping — this is what broke Hawksnest's HA path when Remnant took `:8443`. The map as
> measured 2026-07-29 was `:443`→Magpie(8005), `:8443`→Hawksnest(8090), `:8444`→ntfy(8391),
> `:8445`→Remnant(8006), `:8446`→(8007, **nothing listening — dangling**). `:8447` was verified free
> before being claimed. Note the root `CLAUDE.md` map was already stale when checked: it did not
> list `:8446` at all. Trust `tailscale serve status`, not the doc.

> **8971 is the AUTHENTICATED UI. Never repoint the forwarder at 5000.** Frigate's `:5000` API has
> no authentication at all, and this forwarder is reachable from the whole tailnet. That is why
> `frigate-ui` is a second Service rather than a nodePort bolted onto the existing `frigate` one,
> which also carries `:5000`. `tests/validate_manifests.py` enforces that `frigate-ui` exposes
> exactly `{8971}` and rejects `5000` outright; both invariants were negative-tested.

Interim access without any of the above: `kubectl -n home-automation port-forward svc/frigate 8971:8971`.


**Z-Wave stick re-attach — headless watchdog task.** `usbipd attach` doesn't survive a reboot,
and if zwave-js-ui starts before the stick is attached, containerd masks the device path with a
directory (`/dev/zwave: Is a directory`) and the locks drop to Unavailable. Register the task once
(elevated): `& 'C:\code\hawksnest-automation\windows\register-zwa2-task.ps1'` (or the combined
`register-recovery-tasks.ps1`). It runs `attach-zwa2-logon.ps1` (attach by VID:PID) then
`zwave-attach-heal.sh` (clears any directory mask and bounces zwave-js-ui).

> **Headless-capable (changed 2026-07-20).** The task runs **S4U** ("run whether logged on or
> not") and triggers on **logon + startup + every 3 min**, not logon-only. It used to be a plain
> `-AtLogon`/Interactive one-shot, so the host's *headless* RAM-fault reboots (nobody logs in)
> never re-attached the stick and the Schlage locks sat dead until a human ran the script — once
> for 41h (2026-07-20). The 3-min repetition also covers a first post-boot attempt that races
> WSL/k3s startup, and a mid-session USB drop. `HA-KeepWSL-Alive` was likewise moved to S4U +
> logon/startup so the WSL instance itself comes up without an interactive logon.

**HA on NFS:** the `ha-config` PV needs `nolock` in its mountOptions (already in
`kustomize/base/storage/nfs-pv.yaml`) — without it HA's `flock()` fails `ENOLCK` and crash-loops.
