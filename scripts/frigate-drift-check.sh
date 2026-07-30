#!/usr/bin/env bash
#
# frigate-drift-check.sh — compare the LIVE Frigate config against the repo seed.
#
# The seed ConfigMap (kustomize/base/frigate/configmap.yaml) is copied to the
# frigate-config PVC on FIRST BOOT ONLY, after which the ConfigMap is decorative:
# every real change is a dual write (repo seed + live /config/config.yml). Nothing
# enforced that discipline until this script — a forgotten half of a dual write is
# exactly the kind of drift that reads as "the repo says X" while prod does Y.
#
# Zones are EXPECTED to diverge (drawn in Frigate's UI, live-only by design — the
# backup CronJob owns preserving them) and are ignored. Everything else that
# differs is reported and exits 1, which is the signal — run this from the weekly
# workflow or by hand on the runner:  ./scripts/frigate-drift-check.sh
#
# Requires: kubectl (runner has it), python3 with PyYAML (the k3s runner's distro
# python has it; if not, the script says so and exits 2 rather than pretending).
set -euo pipefail

NS="${NS:-home-automation}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_FILE="${SCRIPT_DIR}/../kustomize/base/frigate/configmap.yaml"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 2; }
python3 -c 'import yaml' 2>/dev/null || {
  echo "python3-yaml missing on this host — install it (apt install python3-yaml)" >&2
  exit 2
}

live="$(mktemp)"
trap 'rm -f "${live}"' EXIT
if ! kubectl exec -n "${NS}" deploy/frigate -- cat /config/config.yml > "${live}" 2>/dev/null; then
  echo "Could not read the live config (Frigate not running in ${NS}?) — nothing to check."
  exit 0
fi

python3 - "$SEED_FILE" "$live" <<'PY'
import sys, yaml

seed_cm, live_path = sys.argv[1], sys.argv[2]
with open(seed_cm) as f:
    seed = yaml.safe_load(f)["data"]["config.yml"]
seed = yaml.safe_load(seed)
with open(live_path) as f:
    live = yaml.safe_load(f)

# Zones are live-only by design; drop them from both sides before comparing.
for cfg in (seed, live):
    for cam in (cfg.get("cameras") or {}).values():
        if isinstance(cam, dict):
            cam.pop("zones", None)

# Frigate writes these back into its live config on its own (measured on 0.17.2):
# a `version` stamp, and a normalized empty top-level `objects.genai` block. Both
# are expected live-only noise, not a forgotten dual write.
live.pop("version", None)
if live.get("objects") == {"genai": {}}:
    live.pop("objects")

def walk(a, b, path=""):
    """Yield (path, seed_value, live_value) for every leaf-level difference."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            yield from walk(a.get(k, "<absent>"), b.get(k, "<absent>"), f"{path}.{k}" if path else k)
    elif a != b:
        yield (path, a, b)

diffs = list(walk(seed, live))
if not diffs:
    print("OK — live Frigate config matches the seed (zones excluded).")
    sys.exit(0)

print(f"DRIFT — {len(diffs)} difference(s) between the seed and the LIVE config")
print("(seed = kustomize/base/frigate/configmap.yaml, live = /config/config.yml;")
print(" fix by completing the dual write in whichever direction was forgotten)\n")
for path, s, l in diffs:
    print(f"  {path}:")
    print(f"    seed: {s!r}")
    print(f"    live: {l!r}")
sys.exit(1)
PY
