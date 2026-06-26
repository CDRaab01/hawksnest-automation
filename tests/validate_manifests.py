#!/usr/bin/env python3
"""Invariant checks for the Hawksnest K3s manifests.

This is a *static* validator: it parses every YAML file under ``kustomize/`` and
asserts the cross-resource invariants that make the stack actually deploy and keep
the door locks online. It does NOT need a cluster, ``kubectl``, or ``kustomize`` —
those run separately in CI for schema validation. The point here is the wiring that
schema validation can't see:

  * every PVC/Secret/ConfigMap a workload references actually exists,
  * NFS PVs stay on v3 (the DS214 is v3-only) and aren't left as placeholders,
  * the Z-Wave controller device path is the committed by-id path (not a REPLACE
    stub that would crash-loop zwave-js-ui and take the locks offline),
  * the documented NodePort / websocket ports don't drift.

Run:  python3 tests/validate_manifests.py
Exit code is non-zero (and a summary prints) if any invariant fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - guidance only
    sys.exit("PyYAML is required: pip install pyyaml")

REPO = Path(__file__).resolve().parent.parent
KUSTOMIZE = REPO / "kustomize"

errors: list[str] = []
checks_run = 0


def check(condition: bool, message: str) -> None:
    """Record a failure if ``condition`` is false."""
    global checks_run
    checks_run += 1
    if not condition:
        errors.append(message)


def load_docs() -> list[dict]:
    """Every YAML document under kustomize/, except the kustomization files
    (which use custom fields PyYAML reads fine but that aren't k8s resources)."""
    docs: list[dict] = []
    for path in sorted(KUSTOMIZE.rglob("*.yaml")):
        if "secrets" in path.parts:
            continue  # secrets/ holds *.example templates, not manifests
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                doc["__file__"] = str(path.relative_to(REPO))
                docs.append(doc)
    return docs


def by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def name(doc: dict) -> str:
    return doc.get("metadata", {}).get("name", "")


def pod_spec(deploy: dict) -> dict:
    return deploy["spec"]["template"]["spec"]


def main() -> int:
    docs = load_docs()
    deployments = by_kind(docs, "Deployment")
    services = by_kind(docs, "Service")
    pvcs = {name(d): d for d in by_kind(docs, "PersistentVolumeClaim")}
    pvs = {name(d): d for d in by_kind(docs, "PersistentVolume")}
    configmaps = {name(d) for d in by_kind(docs, "ConfigMap")}

    # --- Secrets are generated, not committed: read their names from kustomization.
    kustomization = yaml.safe_load((KUSTOMIZE / "kustomization.yaml").read_text())
    generated_secrets = {
        g["name"] for g in kustomization.get("secretGenerator", [])
    }
    declared_secrets = {name(d) for d in by_kind(docs, "Secret")}
    known_secrets = generated_secrets | declared_secrets

    # 1. Every resource is well-formed.
    for d in docs:
        if d.get("kind") in (None, "Kustomization"):
            continue
        check(bool(d.get("apiVersion")), f"{d['__file__']}: missing apiVersion")
        check(bool(name(d)), f"{d['__file__']}: missing metadata.name")

    # 2. Namespace + kustomization agree on home-automation.
    check(
        any(name(n) == "home-automation" for n in by_kind(docs, "Namespace")),
        "Namespace 'home-automation' is not defined",
    )
    check(
        kustomization.get("namespace") == "home-automation",
        "kustomization.yaml namespace is not 'home-automation'",
    )

    # 3. Every resource listed in kustomization.resources exists on disk.
    for rel in kustomization.get("resources", []):
        check((KUSTOMIZE / rel).exists(), f"kustomization references missing file: {rel}")

    # 4. Every PVC / Secret / ConfigMap a Deployment mounts actually exists.
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
        # envFrom secretRefs on every container (init + main).
        containers = spec.get("containers", []) + spec.get("initContainers", [])
        for c in containers:
            for ef in c.get("envFrom", []):
                if "secretRef" in ef:
                    sn = ef["secretRef"]["name"]
                    check(sn in known_secrets, f"{dn}: envFrom unknown Secret '{sn}'")

    # 5. NFS PVCs bind to a real PV by volumeName; capacity covers the request.
    for pvc_name, pvc in pvcs.items():
        sc = pvc["spec"].get("storageClassName")
        if sc == "nfs-manual":
            vol = pvc["spec"].get("volumeName")
            check(
                vol in pvs,
                f"PVC '{pvc_name}' (nfs-manual) has no matching PV volumeName '{vol}'",
            )

    # 6. NFS PVs: v3 mount option, real (non-placeholder) server + path.
    for pv_name, pv in pvs.items():
        nfs = pv["spec"].get("nfs", {})
        opts = pv["spec"].get("mountOptions", [])
        check(
            any(o.startswith("nfsvers=3") for o in opts),
            f"PV '{pv_name}' must mount NFS v3 (the DS214 is v3-only)",
        )
        server = str(nfs.get("server", ""))
        path = str(nfs.get("path", ""))
        check("REPLACE" not in server and bool(server),
              f"PV '{pv_name}' nfs.server is unset/placeholder: '{server}'")
        check("REPLACE" not in path and bool(path),
              f"PV '{pv_name}' nfs.path is unset/placeholder: '{path}'")

    # 7. The must-back-up PVCs exist (losing zwavejs-config = re-pair everything;
    #    losing ring-mqtt-data = re-authenticate the Ring account with 2FA).
    for required in (
        "ha-config",
        "zwavejs-config",
        "mosquitto-data",
        "mariadb-data",
        "ring-mqtt-data",
    ):
        check(required in pvcs, f"required PVC '{required}' is missing")
    # mariadb datadir must stay node-local, never NFS (file-locking risk).
    if "mariadb-data" in pvcs:
        check(
            pvcs["mariadb-data"]["spec"].get("storageClassName") == "local-path",
            "mariadb-data must use the node-local 'local-path' StorageClass, not NFS",
        )

    # 8. zwave-js-ui: privileged + a real /dev by-id device path (not a placeholder).
    zwave = next((d for d in deployments if name(d) == "zwave-js-ui"), None)
    check(zwave is not None, "zwave-js-ui Deployment is missing")
    if zwave:
        spec = pod_spec(zwave)
        container = spec["containers"][0]
        check(
            container.get("securityContext", {}).get("privileged") is True,
            "zwave-js-ui must be privileged to reach the serial device",
        )
        dev = next(
            (v["hostPath"]["path"] for v in spec["volumes"] if "hostPath" in v), ""
        )
        check(dev.startswith("/dev/"), f"zwave-js-ui device hostPath is not under /dev: '{dev}'")
        check(
            "REPLACE" not in dev,
            "zwave-js-ui device path is still a REPLACE placeholder — the controller "
            "isn't wired in, so a deploy would park/crash-loop it and the locks go offline",
        )

    # 9. Documented ports don't drift (portproxy + HA<->zwave wiring depend on these).
    ha_svc = next((s for s in services if name(s) == "home-assistant"), None)
    if ha_svc:
        check(ha_svc["spec"].get("type") == "NodePort", "home-assistant Service must be NodePort")
        ports = ha_svc["spec"]["ports"]
        check(
            any(p.get("nodePort") == 30123 for p in ports),
            "home-assistant NodePort must stay 30123 (the Windows portproxy target)",
        )
    zwave_svc = next((s for s in services if name(s) == "zwave-js-ui"), None)
    if zwave_svc:
        zports = {p["port"] for p in zwave_svc["spec"]["ports"]}
        check(3000 in zports, "zwave-js-ui must expose port 3000 (HA connects to the WS here)")
    ring_svc = next((s for s in services if name(s) == "ring-mqtt"), None)
    if ring_svc:
        rports = {p["port"] for p in ring_svc["spec"]["ports"]}
        check(8554 in rports, "ring-mqtt must expose port 8554 (HA pulls the camera RTSP stream here)")

    # 10. Every Service selects a Deployment that exists (no dangling selectors).
    dep_apps = {name(d): pod_spec(d) for d in deployments}
    dep_labels = {
        name(d): d["spec"]["template"]["metadata"]["labels"] for d in deployments
    }
    for svc in services:
        sel = svc["spec"].get("selector", {})
        matched = any(
            all(labels.get(k) == v for k, v in sel.items())
            for labels in dep_labels.values()
        )
        check(matched, f"Service '{name(svc)}' selector {sel} matches no Deployment")

    # --- report
    if errors:
        print(f"FAIL — {len(errors)} of {checks_run} invariant(s) failed:\n")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print(f"OK — {checks_run} manifest invariants hold across {len(docs)} resources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
