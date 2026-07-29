#!/usr/bin/env python3
"""Invariant checks for the Hawksnest K3s manifests, per overlay.

This validates the *rendered* output of each kustomize overlay (``overlays/prod``
and ``overlays/staging``) — not the raw files — because the overlays apply patches
(namespace, Z-Wave replicas, HA Service type/NodePort, PVC storageClass, NFS PV
deletion) that only exist in the built result. Schema validation (kubeconform) can't
see the cross-resource wiring this checks:

  * every PVC/Secret/ConfigMap a workload references actually exists,
  * prod keeps NFS on v3 (the DS214 is v3-only), real (non-placeholder) server/path,
    and the Z-Wave controller device path is the committed by-id path (not a REPLACE
    stub that would crash-loop zwave-js-ui and take the locks offline),
  * staging never touches the things it must not — no NFS PVs, all storage on
    node-local local-path, Z-Wave parked at replicas:0, HA on ClusterIP (so it can't
    collide with prod's cluster-unique NodePort 30123),
  * the documented NodePort / websocket ports don't drift.

Usage:
  python3 tests/validate_manifests.py                 # build & validate BOTH overlays
                                                      # (needs `kustomize` or `kubectl`)
  python3 tests/validate_manifests.py OVERLAY FILE    # validate a pre-built manifest
                                                      # file as OVERLAY (prod|staging)

CI builds each overlay once (for kubeconform) and passes the built file here, so the
build isn't repeated. Exit code is non-zero (and a summary prints) if any invariant
fails for any overlay.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - guidance only
    sys.exit("PyYAML is required: pip install pyyaml")

REPO = Path(__file__).resolve().parent.parent
KUSTOMIZE = REPO / "kustomize"

# Per-overlay expectations. The shared invariants (PVC/Secret wiring, required PVC
# names, mariadb-data on local-path, zwave/ring ports, Service selectors) hold for
# both; only these fields differ between environments.
OVERLAYS = {
    "prod": {
        "namespace": "home-automation",
        "ha_service_type": "NodePort",
        "ha_nodeport": 30123,        # the Windows portproxy target — must not drift
        "ntfy_service_type": "NodePort",
        "ntfy_nodeport": 30081,      # the Tailscale Serve :8444 forwarder target
        "go2rtc_webrtc_service_type": "NodePort",
        "go2rtc_webrtc_nodeport": 30855,  # the :8555 socat forwarder target
        "require_nfs": True,         # 4 NFS PVs present, v3, real server/path
        "zwave_device_real": True,   # privileged + real /dev by-id path, running
    },
    "staging": {
        "namespace": "home-automation-staging",
        "ha_service_type": "ClusterIP",
        "ha_nodeport": None,         # ClusterIP: no NodePort at all
        "ntfy_service_type": "ClusterIP",
        "ntfy_nodeport": None,       # ClusterIP: no NodePort (no 30081 collision)
        "go2rtc_webrtc_service_type": "ClusterIP",
        "go2rtc_webrtc_nodeport": None,  # ClusterIP: no NodePort (no 30855 collision)
        "require_nfs": False,        # NFS PVs deleted; PVCs on local-path
        "zwave_device_real": False,  # parked at replicas:0 (never claims the stick)
    },
}

# The NFS-backed PVCs in prod (mariadb-data is always local-path, so excluded).
NFS_PVCS = ("ha-config", "zwavejs-config", "mosquitto-data", "ring-mqtt-data")


def by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def name(doc: dict) -> str:
    return doc.get("metadata", {}).get("name", "")


def pod_spec(deploy: dict) -> dict:
    return deploy["spec"]["template"]["spec"]


def build_overlay(overlay: str) -> str:
    """Render an overlay to YAML using kustomize (or `kubectl kustomize`).

    Mirrors CI's behaviour of staging the *.example secret templates so the
    secretGenerator can resolve when the real (gitignored) files are absent.
    """
    overlay_dir = KUSTOMIZE / "overlays" / overlay
    secrets_dir = overlay_dir / "secrets"
    for example in secrets_dir.glob("*.example"):
        real = example.with_suffix("")  # drop the .example suffix
        if not real.exists():
            real.write_bytes(example.read_bytes())

    if shutil.which("kustomize"):
        cmd = ["kustomize", "build", str(overlay_dir)]
    elif shutil.which("kubectl"):
        cmd = ["kubectl", "kustomize", str(overlay_dir)]
    else:
        sys.exit(
            "Need `kustomize` or `kubectl` on PATH to build overlays. "
            "Alternatively run: validate_manifests.py OVERLAY <pre-built-file>"
        )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"failed to build overlay '{overlay}':\n{result.stderr}")
    return result.stdout


def validate(overlay: str, docs: list[dict], expected: dict) -> list[str]:
    """Return a list of invariant-failure messages for one rendered overlay."""
    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(f"[{overlay}] {message}")

    docs = [d for d in docs if isinstance(d, dict) and d.get("kind")]
    deployments = by_kind(docs, "Deployment")
    services = by_kind(docs, "Service")
    pvcs = {name(d): d for d in by_kind(docs, "PersistentVolumeClaim")}
    pvs = {name(d): d for d in by_kind(docs, "PersistentVolume")}
    configmaps = {name(d) for d in by_kind(docs, "ConfigMap")}
    # In rendered output the generated Secrets are real objects (stable names via
    # disableNameSuffixHash), so a workload's secretRef must resolve to one of them.
    known_secrets = {name(d) for d in by_kind(docs, "Secret")}
    ns = expected["namespace"]

    # 1. Every resource is well-formed.
    for d in docs:
        check(bool(d.get("apiVersion")), f"{d.get('kind')}/{name(d)}: missing apiVersion")
        check(bool(name(d)), f"{d.get('kind')}: missing metadata.name")

    # 2. The Namespace object exists and every namespaced resource lands in it.
    check(
        any(name(n) == ns for n in by_kind(docs, "Namespace")),
        f"Namespace '{ns}' is not defined",
    )
    cluster_scoped = {"Namespace", "PersistentVolume", "StorageClass",
                      "ClusterRole", "ClusterRoleBinding"}
    for d in docs:
        if d.get("kind") in cluster_scoped:
            continue
        rns = d.get("metadata", {}).get("namespace")
        check(rns == ns, f"{d.get('kind')}/{name(d)} namespace is '{rns}', expected '{ns}'")

    # 3. Every PVC / Secret / ConfigMap a Deployment mounts actually exists.
    for dep in deployments:
        spec = pod_spec(dep)
        dn = name(dep)
        for vol in spec.get("volumes", []):
            if "persistentVolumeClaim" in vol:
                claim = vol["persistentVolumeClaim"]["claimName"]
                check(claim in pvcs, f"{dn}: references unknown PVC '{claim}'")
            if "secret" in vol:
                sn = vol["secret"]["secretName"]
                check(sn in known_secrets, f"{dn}: references unknown Secret '{sn}'")
            if "configMap" in vol:
                cm = vol["configMap"]["name"]
                check(cm in configmaps, f"{dn}: references unknown ConfigMap '{cm}'")
        containers = spec.get("containers", []) + spec.get("initContainers", [])
        for c in containers:
            for ef in c.get("envFrom", []):
                if "secretRef" in ef:
                    sn = ef["secretRef"]["name"]
                    check(sn in known_secrets, f"{dn}: envFrom unknown Secret '{sn}'")

    # 4. The must-back-up PVCs exist (losing zwavejs-config = re-pair everything;
    #    losing ring-mqtt-data = re-authenticate the Ring account with 2FA).
    for required in ("ha-config", "zwavejs-config", "mosquitto-data",
                     "mariadb-data", "ring-mqtt-data",
                     "frigate-config", "frigate-media"):
        check(required in pvcs, f"required PVC '{required}' is missing")
    # Datadirs that must stay node-local, never NFS — both envs. mariadb because a
    # database server's datadir over NFS reintroduces the file-locking risk we moved
    # off SQLite to avoid; frigate-config for the same reason (it IS a SQLite index,
    # written on every detection); frigate-media because 24/7 video is the most
    # write-heavy thing in the cluster and the DS214 is an aging NFSv3-only box.
    for local_only, why in (
        ("mariadb-data", "a database datadir over NFS reintroduces file-locking risk"),
        ("frigate-config", "Frigate's SQLite event index must not live on NFS"),
        ("frigate-media", "24/7 recordings must not be written over NFS to the DS214"),
    ):
        if local_only in pvcs:
            check(
                pvcs[local_only]["spec"].get("storageClassName") == "local-path",
                f"{local_only} must use the node-local 'local-path' StorageClass, "
                f"not NFS ({why})",
            )

    # 5. Storage shape depends on the environment.
    if expected["require_nfs"]:
        # prod: NFS PVCs bind to a real PV by volumeName; PVs are v3 with real paths.
        for pvc_name in NFS_PVCS:
            pvc = pvcs.get(pvc_name)
            if pvc is None:
                continue
            check(pvc["spec"].get("storageClassName") == "nfs-manual",
                  f"PVC '{pvc_name}' should be on nfs-manual in prod")
            vol = pvc["spec"].get("volumeName")
            check(vol in pvs, f"PVC '{pvc_name}' has no matching PV volumeName '{vol}'")
        for pv_name, pv in pvs.items():
            opts = pv["spec"].get("mountOptions", [])
            nfs = pv["spec"].get("nfs", {})
            check(any(o.startswith("nfsvers=3") for o in opts),
                  f"PV '{pv_name}' must mount NFS v3 (the DS214 is v3-only)")
            server, path = str(nfs.get("server", "")), str(nfs.get("path", ""))
            check("REPLACE" not in server and bool(server),
                  f"PV '{pv_name}' nfs.server is unset/placeholder: '{server}'")
            check("REPLACE" not in path and bool(path),
                  f"PV '{pv_name}' nfs.path is unset/placeholder: '{path}'")
    else:
        # staging: NO NFS PVs, and every formerly-NFS PVC repointed to local-path so
        # it can never reach the Synology.
        check(not pvs, f"staging must define no PersistentVolumes (found {sorted(pvs)})")
        for pvc_name in NFS_PVCS:
            pvc = pvcs.get(pvc_name)
            if pvc is None:
                continue
            sc = pvc["spec"].get("storageClassName")
            check(sc == "local-path",
                  f"staging PVC '{pvc_name}' must be local-path, not '{sc}'")
            check("volumeName" not in pvc["spec"],
                  f"staging PVC '{pvc_name}' must not bind a (deleted) NFS PV by volumeName")

    # 5b. go2rtc: the MAIN container must mount the config PVC at /config. It once
    #     shipped without any volumeMounts — the seed init wrote streams to the PVC
    #     while go2rtc read its own empty image /config, so every stream change
    #     silently no-opped (/api/streams == {}). Never again.
    g2 = next((d for d in deployments if name(d) == "go2rtc"), None)
    if g2:
        main = next((c for c in pod_spec(g2)["containers"] if c["name"] == "go2rtc"), None)
        check(main is not None, "go2rtc Deployment has no 'go2rtc' container")
        if main:
            mounts = {(m["name"], m["mountPath"]) for m in main.get("volumeMounts", [])}
            check(("config", "/config") in mounts,
                  "go2rtc main container must mount the 'config' volume at /config "
                  "(without it, seeded streams never reach go2rtc)")

    # 5c. Frigate: three invariants that each guard a failure we can't see at deploy
    #     time — a leaked snapshot, a wedged pod, and an evicted neighbour.
    fr = next((d for d in deployments if name(d) == "frigate"), None)
    if fr:
        main = next((c for c in pod_spec(fr)["containers"] if c["name"] == "frigate"), None)
        check(main is not None, "frigate Deployment has no 'frigate' container")
        if main:
            # (a) The GenAI endpoint MUST be pinned by env. Frigate 0.17 ignores
            #     `genai.base_url` for the openai provider, so a config that looks
            #     local silently posts camera snapshots to api.openai.com. These are
            #     indoor cameras and a bedroom one is planned for a later phase; this
            #     is a privacy invariant, not a tidiness one.
            env_names = {e["name"] for e in main.get("env", [])}
            check("OPENAI_BASE_URL" in env_names,
                  "frigate must set OPENAI_BASE_URL explicitly — Frigate 0.17 ignores "
                  "genai.base_url and would send camera snapshots to api.openai.com")
            # (b) Pinned image. The config schema moves between minor releases (0.17
            #     removed record.retain), so a floating tag rolls forward on any pod
            #     recreate and wedges Frigate on a config it can no longer parse.
            image = main.get("image", "")
            check(not image.endswith((":stable", ":latest")) and ":" in image,
                  f"frigate image must be pinned to an exact version, got '{image}' "
                  "(the config schema is version-specific)")
            # (c) A memory limit, to protect ring-mqtt. It sits at a 1Gi limit with no
            #     liveness probe on this same single node; an unbounded Frigate
            #     (detector + ffmpeg + CLIP embeddings) evicting it drops every Ring
            #     camera at once.
            check("memory" in main.get("resources", {}).get("limits", {}),
                  "frigate must declare a memory limit so it cannot evict ring-mqtt")
        # (d) Frigate's SQLite index and 24/7 video must never reach the Synology.
        claims = {v["persistentVolumeClaim"]["claimName"]
                  for v in pod_spec(fr).get("volumes", [])
                  if "persistentVolumeClaim" in v}
        for needed in ("frigate-config", "frigate-media"):
            check(needed in claims, f"frigate must mount the '{needed}' PVC")

    # 6. zwave-js-ui: always privileged; prod runs against a real device, staging parks.
    zwave = next((d for d in deployments if name(d) == "zwave-js-ui"), None)
    check(zwave is not None, "zwave-js-ui Deployment is missing")
    if zwave:
        spec = pod_spec(zwave)
        container = spec["containers"][0]
        check(container.get("securityContext", {}).get("privileged") is True,
              "zwave-js-ui must be privileged to reach the serial device")
        dev = next((v["hostPath"]["path"] for v in spec["volumes"] if "hostPath" in v), "")
        check(dev.startswith("/dev/"), f"zwave-js-ui device hostPath is not under /dev: '{dev}'")
        if expected["zwave_device_real"]:
            check("REPLACE" not in dev,
                  "zwave-js-ui device path is still a REPLACE placeholder — the controller "
                  "isn't wired in, so a deploy would park/crash-loop it and the locks go offline")
        else:
            check(zwave["spec"].get("replicas") == 0,
                  "staging zwave-js-ui must be parked at replicas:0 (it must never claim the stick)")

    # 7. Documented ports don't drift; HA Service type matches the environment.
    ha_svc = next((s for s in services if name(s) == "home-assistant"), None)
    if ha_svc:
        check(ha_svc["spec"].get("type") == expected["ha_service_type"],
              f"home-assistant Service must be {expected['ha_service_type']}")
        node_ports = [p.get("nodePort") for p in ha_svc["spec"]["ports"]]
        if expected["ha_nodeport"] is None:
            check(all(np is None for np in node_ports),
                  "staging home-assistant Service must not set a nodePort (ClusterIP)")
        else:
            check(expected["ha_nodeport"] in node_ports,
                  f"home-assistant NodePort must stay {expected['ha_nodeport']} "
                  "(the Windows portproxy target)")
    ntfy_svc = next((s for s in services if name(s) == "ntfy"), None)
    check(ntfy_svc is not None, "ntfy Service is missing (push transport)")
    if ntfy_svc:
        check(ntfy_svc["spec"].get("type") == expected["ntfy_service_type"],
              f"ntfy Service must be {expected['ntfy_service_type']}")
        ntfy_node_ports = [p.get("nodePort") for p in ntfy_svc["spec"]["ports"]]
        if expected["ntfy_nodeport"] is None:
            check(all(np is None for np in ntfy_node_ports),
                  "staging ntfy Service must not set a nodePort (ClusterIP)")
        else:
            check(expected["ntfy_nodeport"] in ntfy_node_ports,
                  f"ntfy NodePort must stay {expected['ntfy_nodeport']} "
                  "(the Tailscale Serve :8444 forwarder target)")
    g2w_svc = next((s for s in services if name(s) == "go2rtc-webrtc"), None)
    check(g2w_svc is not None, "go2rtc-webrtc Service is missing (WebRTC media path)")
    if g2w_svc:
        check(g2w_svc["spec"].get("type") == expected["go2rtc_webrtc_service_type"],
              f"go2rtc-webrtc Service must be {expected['go2rtc_webrtc_service_type']}")
        g2w_node_ports = [p.get("nodePort") for p in g2w_svc["spec"]["ports"]]
        if expected["go2rtc_webrtc_nodeport"] is None:
            check(all(np is None for np in g2w_node_ports),
                  "staging go2rtc-webrtc Service must not set a nodePort (ClusterIP)")
        else:
            check(expected["go2rtc_webrtc_nodeport"] in g2w_node_ports,
                  f"go2rtc-webrtc NodePort must stay {expected['go2rtc_webrtc_nodeport']} "
                  "(the :8555 socat forwarder target)")
    zwave_svc = next((s for s in services if name(s) == "zwave-js-ui"), None)
    if zwave_svc:
        zports = {p["port"] for p in zwave_svc["spec"]["ports"]}
        check(3000 in zports, "zwave-js-ui must expose port 3000 (HA connects to the WS here)")
    ring_svc = next((s for s in services if name(s) == "ring-mqtt"), None)
    if ring_svc:
        rports = {p["port"] for p in ring_svc["spec"]["ports"]}
        check(8554 in rports, "ring-mqtt must expose port 8554 (HA pulls the camera RTSP stream)")

    # 8. Every Service selects a Deployment that exists (no dangling selectors).
    dep_labels = {name(d): d["spec"]["template"]["metadata"]["labels"] for d in deployments}
    for svc in services:
        sel = svc["spec"].get("selector", {})
        matched = any(all(labels.get(k) == v for k, v in sel.items())
                      for labels in dep_labels.values())
        check(matched, f"Service '{name(svc)}' selector {sel} matches no Deployment")

    return errors


def main(argv: list[str]) -> int:
    if len(argv) >= 1:
        overlay = argv[0]
        if overlay not in OVERLAYS:
            sys.exit(f"unknown overlay '{overlay}' (expected one of {sorted(OVERLAYS)})")
        if len(argv) >= 2:
            rendered = Path(argv[1]).read_text()
        else:
            rendered = build_overlay(overlay)
        targets = {overlay: rendered}
    else:
        targets = {ov: build_overlay(ov) for ov in OVERLAYS}

    all_errors: list[str] = []
    total_docs = 0
    for overlay, rendered in targets.items():
        docs = [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]
        total_docs += len(docs)
        all_errors.extend(validate(overlay, docs, OVERLAYS[overlay]))

    if all_errors:
        print(f"FAIL — {len(all_errors)} invariant(s) failed:\n")
        for e in all_errors:
            print(f"  ✗ {e}")
        return 1
    print(f"OK — invariants hold for {', '.join(targets)} "
          f"({total_docs} rendered resources).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
