#!/usr/bin/env bash
#
# ha-config-drift-check.sh — compare the LIVE Home Assistant config against the repo seed.
#
# The sibling of frigate-drift-check.sh, pointed at the config that actually matters. The HA
# seed ConfigMap (kustomize/base/home-assistant/configmap.yaml) is copied to the ha-config PVC
# on FIRST BOOT ONLY; after that it is decorative and every real change is a dual write.
#
# Nothing enforced that, and it showed: measured 2026-08-02, live automations.yaml held 21
# automations against the seed's 6. The extra 15 — the ZEN32 scene controllers, both WLED
# LED-bar stacks, All Lock, Garage Door Unarm — are most of the real automation in the house,
# and they existed in exactly one place: an NFS volume. Not in git, and never seen by CI's
# ha-config-check job, which validates the seed only.
#
# Also checks for DUPLICATE automation ids, because that bug was live at the same time: two
# entries both claiming `hawksnest_push_doorbell`, so HA logged "does not generate unique IDs
# ... ignoring automation.doorbell" and silently dropped one. A duplicate id is not drift, but
# it is the same failure mode — config that does not do what it looks like it does — and this
# is the script already reading the file.
#
# Exit codes: 0 clean, 1 drift or duplicate ids, 2 cannot run.
# Requires: kubectl, python3 with PyYAML.
set -euo pipefail

NS="${NS:-home-automation}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_FILE="${SCRIPT_DIR}/../kustomize/base/home-assistant/configmap.yaml"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 2; }
python3 -c 'import yaml' 2>/dev/null || {
  echo "python3-yaml missing on this host — install it (apt install python3-yaml)" >&2
  exit 2
}

# The files the seed owns. scenes.yaml is included even though both sides are empty today:
# the point is to notice the first time it stops being empty.
FILES=(automations.yaml scripts.yaml scenes.yaml configuration.yaml)

livedir="$(mktemp -d)"
trap 'rm -rf "${livedir}"' EXIT

for f in "${FILES[@]}"; do
  if ! kubectl exec -n "${NS}" deploy/home-assistant -c home-assistant \
      -- cat "/config/${f}" > "${livedir}/${f}" 2>/dev/null; then
    echo "Could not read /config/${f} (HA not running in ${NS}?) — nothing to check."
    exit 0
  fi
done

python3 - "$SEED_FILE" "$livedir" "${FILES[@]}" <<'PY'
import sys, yaml

seed_cm, livedir, *files = sys.argv[1:]

# HA's YAML uses local tags PyYAML does not know (!include, !secret, !include_dir_merge_list,
# !env_var …). Represent each as a stable marker so the two sides stay comparable instead of
# blowing up the parse — an unresolved !include compares equal to an unresolved !include.
class HaLoader(yaml.SafeLoader):
    pass

def _tagged(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return f"<{tag_suffix} {loader.construct_scalar(node)}>"
    if isinstance(node, yaml.SequenceNode):
        return f"<{tag_suffix} {loader.construct_sequence(node)}>"
    return f"<{tag_suffix}>"

HaLoader.add_multi_constructor("!", _tagged)

def load(text):
    return yaml.load(text, Loader=HaLoader)

# HA 2024.10 renamed the automation keys (trigger→triggers, condition→conditions,
# action→actions) and the service call key (service→action), and the UI editor rewrites any
# automation it touches into the new spelling. Both spellings still work, so an automation
# edited in the UI is not drift — but without normalizing it reports as a total rewrite, and a
# checker that cries wolf on every UI edit is one nobody reads. Normalize to the new names.
KEY_ALIASES = {"trigger": "triggers", "condition": "conditions", "action": "actions"}

def normalize(node, in_step=False):
    if isinstance(node, list):
        return [normalize(x, in_step) for x in node]
    if not isinstance(node, dict):
        # Templates get reflowed by the UI editor (newline-and-indent collapsed to spaces).
        # Jinja treats those the same, so compare on collapsed whitespace.
        return " ".join(node.split()) if isinstance(node, str) else node
    out = {}
    for k, v in node.items():
        # `action:` is overloaded: at automation top level it means the step list (old name for
        # `actions`), but inside a step it names the service to call (new name for `service`).
        if k == "action" and in_step:
            out["service"] = normalize(v, True)
            continue
        nk = KEY_ALIASES.get(k, k) if not in_step else k
        # Children of triggers/conditions/actions are steps.
        out[nk] = normalize(v, in_step or nk in ("triggers", "conditions", "actions",
                                                 "sequence", "default", "choose", "else"))
    return out

with open(seed_cm) as f:
    seed_data = yaml.safe_load(f)["data"]

problems = 0

def walk(a, b, path=""):
    """Yield (path, seed_value, live_value) for every leaf-level difference."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            yield from walk(a.get(k, "<absent>"), b.get(k, "<absent>"),
                            f"{path}.{k}" if path else k)
    elif a != b:
        yield (path, a, b)

def report(title, diffs):
    global problems
    if not diffs:
        return
    problems += len(diffs)
    print(f"\nDRIFT — {title}: {len(diffs)} difference(s)")
    for path, s, l in diffs:
        print(f"  {path}:")
        print(f"    seed: {s!r}")
        print(f"    live: {l!r}")

for fname in files:
    seed_text = seed_data.get(fname)
    if seed_text is None:
        print(f"note: {fname} is not in the seed ConfigMap — skipping.")
        continue
    seed = normalize(load(seed_text))
    with open(f"{livedir}/{fname}") as f:
        live = normalize(load(f.read()))

    # automations.yaml and scenes.yaml are LISTS keyed by `id`. A positional diff on those is
    # noise — reordering in the UI would read as a total rewrite. Compare by id instead.
    if isinstance(seed, list) or isinstance(live, list):
        seed_l = seed or []
        live_l = live or []

        # Duplicate ids first: HA keeps the first and silently ignores the rest, so a
        # duplicate is a rule that looks configured and never runs.
        seen, dupes = set(), []
        for item in live_l:
            if not isinstance(item, dict):
                continue
            i = item.get("id")
            if i in seen:
                dupes.append(i)
            seen.add(i)
        if dupes:
            problems += len(dupes)
            print(f"\nDUPLICATE IDS — live {fname}: {', '.join(map(str, dupes))}")
            print("  HA keeps the first entry with each id and ignores the rest")
            print('  ("Platform automation does not generate unique IDs ... ignoring ...").')
            print("  Whichever one you edited most recently may be the one doing nothing.")

        def by_id(lst):
            # FIRST occurrence wins, matching HA: on a duplicate id it keeps the first and
            # ignores the rest. Last-wins would diff against the entry HA is discarding, which
            # is precisely the wrong half to show someone chasing a duplicate.
            out = {}
            for n, x in enumerate(lst):
                if isinstance(x, dict):
                    out.setdefault(x.get("id", f"<no id #{n}>"), x)
            return out

        s_map, l_map = by_id(seed_l), by_id(live_l)

        only_live = sorted(set(l_map) - set(s_map))
        only_seed = sorted(set(s_map) - set(l_map))
        if only_live:
            problems += len(only_live)
            print(f"\nLIVE-ONLY — {fname}: {len(only_live)} entr(ies) exist only on the PVC")
            print("  These are one disk failure or one re-seed from gone. Copy them into")
            print(f"  the seed ConfigMap ({seed_cm}).")
            for i in only_live:
                print(f"    {i}   ({l_map[i].get('alias', '')})")
        if only_seed:
            problems += len(only_seed)
            print(f"\nSEED-ONLY — {fname}: {len(only_seed)} entr(ies) are in git but not live")
            for i in only_seed:
                print(f"    {i}   ({s_map[i].get('alias', '')})")

        for i in sorted(set(s_map) & set(l_map)):
            report(f"{fname} [{i}]", list(walk(s_map[i], l_map[i])))
        continue

    report(fname, list(walk(seed or {}, live or {})))

if problems:
    print(f"\n{problems} problem(s). Fix by completing the dual write in whichever direction")
    print("was forgotten — the seed is the side that survives a rebuild.")
    sys.exit(1)

print("OK — live HA config matches the seed.")
PY
