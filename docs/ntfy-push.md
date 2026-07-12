# ntfy push for Hawksnest (doorbell + alarm)

Self-hosted [ntfy](https://ntfy.sh) is the suite's push transport (decided 2026-07-12,
Hawksnest `V1.md` Gate 3): no Google/FCM dependency, and it fits the tailnet-only,
local-first posture — ntfy is reachable **only over Tailscale**, never the public internet.

```
HA automation ──POST http://ntfy──▶ ntfy (K3s, ClusterIP :80)
                                      │
Phone (Hawksnest app / ntfy app) ◀── Tailscale Serve https://<host>.ts.net:8444
                                      └─ NodePort 30081 ◀─ host forwarder
```

## What this repo ships

- **`kustomize/base/ntfy/`** — Deployment + Service (NodePort **30081** in prod; the staging
  overlay patches it to ClusterIP so it can't collide on the shared cluster) + `ntfy-config`
  ConfigMap (`server.yml`). Cache on a node-local `ntfy-cache` PVC.
- **HA config** (`home-assistant/configmap.yaml` seed) — a generic `rest_command.ntfy_publish`
  and two automations (`hawksnest_push_doorbell`, `hawksnest_push_alarm`) that publish to the
  `hawksnest-alerts` topic. They're **entity-ID-free** (they filter the global `state_changed`
  event) so they work without knowing how the Ring/alarm entities are named here.

The seed only reaches a **fresh** HA install (the initContainer won't clobber the live PVC), so
the two live-apply steps below are required to light this up on the running instance.

## Step 1 — deploy ntfy (staging first, then prod)

```bash
# Staging smoke test (own namespace, ntfy on ClusterIP):
kubectl kustomize kustomize/overlays/staging | kubectl apply -f -
kubectl -n home-automation-staging rollout status deploy/ntfy
kubectl -n home-automation-staging exec deploy/home-assistant -- \
  wget -qO- --post-data='staging test' --header='Title: hi' http://ntfy/hawksnest-alerts

# Prod (normally via the deploy.yml workflow on push to main):
kubectl -n home-automation rollout status deploy/ntfy
```

## Step 2 — apply the push config to the LIVE HA

The live `ha-config` PVC predates this change, so add the config by hand (or copy from the seed
in `home-assistant/configmap.yaml`):

1. **`configuration.yaml`** — add the `rest_command.ntfy_publish` block (verbatim from the seed).
2. **`automations.yaml`** — append `hawksnest_push_doorbell` and `hawksnest_push_alarm` (verbatim),
   **or** recreate them in the UI (Settings → Automations).
3. **Developer Tools → YAML → Check Configuration**, then **Reload** *REST Commands* and
   *Automations* (no full restart needed).
4. Test end to end:
   ```bash
   kubectl -n home-automation exec deploy/home-assistant -- \
     wget -qO- --post-data="Someone's at Front Door" \
       --header='Title: Doorbell' --header='Tags: bell' http://ntfy/hawksnest-alerts
   ```
   The Hawksnest app (or the ntfy app subscribed to `hawksnest-alerts`) should buzz.

> **Efficiency note:** the automations watch the global `state_changed` event and short-circuit
> in a template condition — fine for a home instance. Once you've confirmed the real doorbell
> `binary_sensor.*_ding` and `alarm_control_panel.*` entity_ids, you may convert them to specific
> `platform: state` triggers.

## Step 3 — expose ntfy to phones (Tailscale Serve :8444)

ntfy is fronted the same way Hawksnest's web app is (`https://<host>.ts.net:8443`): a host-side
forwarder to the NodePort plus a Tailscale Serve mapping — on **:8444** for ntfy. This is wired in
the Hawksnest repo's `deploy/windows/hawksnest-serve.ps1` (and its logon Scheduled Task), which
brings up both fronts at boot. Manual bring-up mirrors Hawksnest's:

```powershell
# In WSL Dragonfly: socat 8391 -> ntfy NodePort 30081 (transient systemd unit)
# Then on the host:  tailscale serve --bg --https=8444 http://127.0.0.1:8391
```

The phone's ntfy base URL is then `https://<host>.ts.net:8444`, topic `hawksnest-alerts`.

## Access control

`server.yml` runs ntfy with **no auth** — open read-write. Deliberate: it's tailnet-only, so the
trust boundary is the tailnet. To lock it down later, add an `auth-file` (SQLite), set
`auth-default-access: deny-all`, and mint per-topic tokens for HA (publish) and the phones
(subscribe). The topic name (`hawksnest-alerts`) is the only obscurity today.
