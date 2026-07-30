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
  and the automations that publish to the `hawksnest-alerts` topic: `hawksnest_push_doorbell`,
  `hawksnest_push_alarm`, `hawksnest_push_camera_object`, `hawksnest_frigate_detection_watchdog`.
  All are **entity-ID-free** — the first two filter the global `state_changed` event, the camera
  one reads Frigate's own MQTT payload — so they work without knowing how entities are named here.

The seed only reaches a **fresh** HA install (the initContainer won't clobber the live PVC), so
the two live-apply steps below are required to light this up on the running instance.

### Rich doorbell payload (2026-07-13)

The doorbell automation + `rest_command` gained two things (pairs with the Hawksnest app's
rich-push release — a tap deep-links to the camera and shows its snapshot):

- **`rest_command.ntfy_publish`** grew an `Attach:` header (`{{ attach | default('') }}`) — empty
  for alarm/other publishes, so they're unaffected.
- **`hawksnest_push_doorbell`** derives the camera base from the `_ding` sensor and sets
  `click: …/?camera=camera.<base>` (the app's `PushRoute.cameraOf` opens that camera) and
  `attach:` = the camera's `entity_picture` resolved against the TLS front (the snapshot image).

**To apply to a running instance:** add the `Attach:` line to the live `configuration.yaml`
`rest_command`, and replace the live `hawksnest_push_doorbell` automation with the seed version;
then Reload *REST Commands* + *Automations*. Until then, doorbell push still works — it just lands
on Home with no photo instead of the specific camera.

### Camera object alerts (2026-07-30)

`hawksnest_push_camera_object` — "There is a person at your Kitchen", with a picture. The repo's
**first MQTT-triggered automation**: it subscribes to `frigate/events` rather than filtering the
global `state_changed` firehose.

Design points worth not re-deriving:

- **Fires on `type == 'new'` only.** Frigate publishes `{type, before, after}`; `new` is emitted
  once per tracked object, `update` repeats as score/zone change, and `end` arrives after the
  person has left. Templates therefore read `trigger.payload_json.after.*`.
- **Armed-only.** These are indoor cameras; with the house occupied Frigate produced ~10 person
  events in a few minutes, so alerting while disarmed is unusable. The condition is written
  entity-ID-free (`states.alarm_control_panel | selectattr('state', 'in', [...])`) and lists only
  `armed_home`/`armed_away` because the panel reports `supported_features=3` — night/vacation
  cannot occur here.
- **`attach` is the camera's signed `entity_picture`, NOT Frigate's
  `/api/frigate/notifications/<id>/thumbnail.jpg`.** Frigate writes both the thumbnail and the
  snapshot to disk in `end()`, so at `type: new` neither file reliably exists and the attach would
  usually 404 — which silently degrades the push to text-only. `entity_picture` is always present,
  is the wide room view, and self-authenticates via its signed token (the app fetches notification
  images with **no** auth headers). It is a live frame, so it can lag the event slightly.
- **Two `input_boolean` toggles** gate it: `hawksnest_alert_person` and `hawksnest_alert_pets`
  (person **fails open** if the helper is missing — a renamed helper must not silently disable a
  security alert; pets **fail closed**). They're server-side so the push is never generated for a
  device that doesn't want it. Hawksnest's Settings screen writes them.
- **`click` already carries `&event=<id>`** even though no client reads it yet — the live-apply
  ritual below is the expensive step, so the follow-up stays a pure client change.

**To apply to a running instance:** add the `input_boolean:` block to the live
`configuration.yaml` and append the automation to the live `automations.yaml`, then Reload
*Input Booleans* + *Automations*. **Then turn `input_boolean.hawksnest_alert_person` ON** — the
helpers deliberately carry no `initial:` (which would reset them on every restart and override the
user's choice), so they start `off` on first creation.

Prove it without waiting for a real person — publish a synthetic event:

```bash
kubectl -n home-automation exec deploy/mosquitto -- \
  mosquitto_pub -h localhost -u frigate -P '<pw>' -t frigate/events -m \
  '{"type":"new","before":{},"after":{"id":"test-1","camera":"kitchen","label":"person"}}'
```
With the alarm armed and person alerts on, that should land a push. Disarm and repeat: nothing.

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
