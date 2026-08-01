# Kickoff prompt — four-camera rollout

Paste the block below into a fresh Claude Code session in `C:\Code`. It is deliberately written to
make the agent *gather facts before editing*, because the four things most likely to go wrong are
all decided before any config is touched.

---

```
I'm adding four new Reolink cameras to the Frigate/go2rtc stack. This is production —
the same cluster runs Home Assistant and the door locks.

Start by reading C:\Code\hawksnest-automation\docs\adding-a-camera.md in full. It is a
tested runbook written 2026-07-31 against this exact cluster, and it already corrects
four things the older docs get wrong (ffprobe's location, the disk budget, a missing
recorder.exclude block, and the Tailscale route being replace-not-append). Follow it
rather than plan.md, which is a migration narrative, not instructions.

Before you edit anything, ask me for:
  - each camera's room / intended slug
  - each camera's IP (and whether it's a DHCP reservation or static)
  - each camera's model (E1 Zoom, E1 Pro, or something else)
  - whether any of them REPLACES a live Ring camera in that room, or runs alongside it
  - whether they use the existing shared Reolink credentials or new ones

Then, per camera, run the ffprobe recipe from the runbook (it runs in the go2rtc
container, not Frigate) and tell me the real geometry before writing any detect: block.
Do not copy geometry between cameras — the two deployed models genuinely differ
(640x360 vs 896x512) and a mismatch misplaces every bounding box without erroring.

Non-negotiables:
  - The Frigate seed is FIRST-BOOT-ONLY. Every Frigate config change is a dual write to
    the ConfigMap AND the live /config/config.yml. Stage to a temp file and verify it
    IN THE POD before replacing the live one. Run scripts/frigate-drift-check.sh after.
  - go2rtc is the opposite: it re-seeds every start. Edit the ConfigMap, restart the pod,
    never hand-edit its live file.
  - An invalid Frigate config puts ALL cameras into safe mode, including the three that
    currently work. Check the logs for "safe mode" after every restart.
  - `tailscale set --advertise-routes` REPLACES the list. Re-issue it with all seven /32s
    and re-approve in the console. Verify with PrimaryRoutes, not just the CLI — an
    unapproved route makes RTSP unreachable and the only symptom is "live view feels
    slower".
  - Don't put a pull_request trigger on anything that runs on a self-hosted runner.

Work in small verified steps and show me the evidence at each one (Frigate /api/stats
with a live pid and skipped_fps at 0, go2rtc listing both <name> and <name>_sub, the
drift check clean). Tell me plainly if something fails rather than working around it.

Two things in the runbook need my decision, so surface them rather than assuming:
  - whether to add the recorder.exclude block now (there is currently NO exclude block
    at all, and four cameras adds ~100-120 continuously-recorded entities)
  - whether to raise retention past 3 days once we've measured a full day of disk growth

Finally: update docs/adding-a-camera.md in the same PR with anything you learn that the
runbook got wrong or didn't cover. It's meant to get better each time it's used.
```

---

## Why the prompt is shaped this way

- **Facts before edits.** Slug collisions, model differences and Ring-replacement decisions are
  all cheap to settle up front and expensive to unpick afterwards.
- **It names the two silent failures explicitly.** The seed direction is the single likeliest way
  to lose an hour, and it is counter-intuitive because Frigate and go2rtc behave oppositely.
- **It asks for evidence, not assurances.** `skipped_fps` and `PrimaryRoutes` are the two checks
  whose failure modes are invisible in the app.
- **It leaves the two judgement calls to the human** — recorder exclusions and retention — because
  both trade something real (searchable history, disk) and neither has a right answer the agent
  can derive.
