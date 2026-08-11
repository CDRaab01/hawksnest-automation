# WLED presets — the state that isn't in this repo

Two WLED strips are wired into the scene pads. Their **presets live on the ESP32, not in git**,
and the ZEN32 automations select them **by name**. That makes preset names a silent coupling: if
a name an automation asks for does not exist on that strip, the key press fails with
`ServiceValidationError: Option <name> is not valid` and the pad looks dead, with nothing wrong
anywhere in this repo.

That is not hypothetical — it is what happened. See the incident below.

| | Master Bedroom | Cooper's Bedroom |
|---|---|---|
| IP | 192.168.4.65 | 192.168.4.45 |
| MAC | `0070077d2f2c` | `f024f9565ff0` |
| LEDs | 748 (segment spans 0–744) | 237 |
| Type | SK6812 **RGBW** (`rgbw: true`) | RGB |
| Firmware | 0.15.1 | 0.14.4 |
| Boot preset | 5 = Amber Night | 1 = Startup |
| Mains supply | **the ZEN32's relay** — see `1785540000008` | always powered |
| Selected by | `1785540000001` (keys 1–4) | `1785527100001` |

The master strip's mains being switched by its own pad is why it needs a WLED-entry watchdog and
Cooper's does not: the strip is unreachable whenever the big key is off, which is a normal state.

## Presets

**Master Bedroom** — 1–5 pre-existed; **6–9 were created 2026-08-11** (see incident).

| # | Name | fx | pal | bri | Used by |
|---|---|---|---|---|---|
| 1 | Startup | 0 Solid | 3 | 187 | `1785540000003` (strip came back during the day) |
| 2 | Blue Relax | 96 | 18 | 8 | — |
| 3 | Noisemeter | 136 | 2 | 8 | — |
| 4 | Rainbow | 87 | 0 | 42 | — |
| 5 | Amber Night | 0 Solid | 0 | 26 | boot preset (`bootps`) |
| 6 | Nebula | 97 Plasma | 40 Magenta | 130 | **key 1** (magenta LED) |
| 7 | Ocean | 101 Pacifica | 9 Ocean | 140 | **key 2** (cyan LED) |
| 8 | Warm White | 0 Solid | 0 | 160 | **key 3** (white LED) |
| 9 | Campfire | 66 Fire 2012 | 35 Fire | 170 | **key 4** (yellow LED) |

Each key's ZEN32 LED colour is chosen to match its preset's palette — that mapping is written
from `1785540000007` and is the reason the pad "says what the keys do".

**Cooper's Bedroom**: 1 Startup, 2 Music, 3 Relax, 4 Sleep, 5 Rainbow 1, 6 New Sleep,
7 Rainbow 2, 8 Campfire, 9 Ocean, 10 Nebula, 11 Starry Night, 12 Rocket.

## Incident, 2026-08-11 — the master pad pointed at Cooper's presets

The 2026-08-04 rewire mapped the master bedroom's keys 1–4 to
`Nebula / Ocean / Warm White / Campfire`. Nebula, Ocean and Campfire existed **only on Cooper's
strip**; Warm White existed on neither. So from 2026-08-04 every single-tap and double-tap of
keys 1–4 in the master bedroom failed, and the strip stayed on whatever it had.

It stayed invisible for a week because the failure needs two things to be seen at once: the
select call only reaches validation when the WLED entity is *available*, and for most of that
window the entity was `unavailable` for an unrelated reason (the config-entry problem fixed in
the same change). Until then HA logged the blander `Referenced entities
select.master_bedroom_preset are missing or not currently available`, which reads as a
connectivity problem, not a naming one.

Fixed by creating presets 6–9 on the master strip so the automation's documented design works as
written — Nebula/Ocean/Campfire copied from Cooper's (effect and palette IDs verified identical
across 0.14.4 and 0.15.1: 187 effects, 71 palettes, same names at the same indices), and Warm
White authored here to drive the SK6812's dedicated white channel, which is the one thing this
RGBW strip does that Cooper's RGB strip cannot.

## Recreating them (also: the `ib`/`sb` trap)

If the master strip is reflashed or its presets are lost, recreate 6–9 with the payloads below.
`start`/`stop` are `0`/`744` to match the strip's existing segment — **not** 748, which is the LED
count.

**A plain `psave` silently stores neither brightness nor segment bounds.** You must pass
`"ib": true` (include brightness) and `"sb": true` (save segment bounds). Without them the preset
saves its effect and palette only, and then inherits whatever brightness happens to be set when
it is applied — so pressing a key at night could hand you a full-brightness room. Presets 6–9
were saved twice before this was spotted; compare against 1–5, which do carry `on`, `bri`,
`start` and `stop`.

Apply the state first, then save it:

```bash
# 1. apply  — POST to http://192.168.4.65/json/state
{"on":true,"bri":130,"transition":7,
 "seg":[{"id":0,"start":0,"stop":744,"grp":1,"spc":0,"of":0,"on":true,"frz":false,
         "bri":255,"cct":127,"set":0,"n":"","c1":128,"c2":128,"c3":16,"sel":true,
         "rev":false,"mi":false,"o1":false,"o2":false,"o3":false,"si":0,"m12":0,
         "fx":97,"sx":90,"ix":140,"pal":40,
         "col":[[54,195,255,0],[0,0,0,0],[0,0,0,0]]}]}

# 2. save   — POST to the same endpoint
{"psave":6,"n":"Nebula","ib":true,"sb":true}
```

Per-preset values (everything else as above):

| # | Name | bri | fx | sx | ix | pal | col[0] |
|---|---|---|---|---|---|---|---|
| 6 | Nebula | 130 | 97 | 90 | 140 | 40 | `[54,195,255,0]` |
| 7 | Ocean | 140 | 101 | 80 | 128 | 9 | `[54,195,255,0]` |
| 8 | Warm White | 160 | 0 | 128 | 128 | 0 | `[0,0,0,255]` ← W channel |
| 9 | Campfire | 170 | 66 | 110 | 30 | 35 | `[54,195,255,0]` |

Pace the writes. Each `psave` commits to flash, and four in quick succession made the ESP32's
HTTP server refuse connections for ~30 s. It recovers on its own — `uptime` in `/json/info` will
show it never rebooted — but a script that does not retry will look like it bricked the strip.

Rapid preset changes *through Home Assistant* can also drop the integration's connection
(`WLEDConnectionError`, and the related `No PONG received after 15.0 seconds`). The strip is
fine when this happens; it is HA's link that dies, and `1785540000008` heals it within one
10-minute tick. Verified 2026-08-11: four preset changes in 20 s dropped the connection at
15:17:19, and the watchdog restored it at 15:20:00 unattended. The same preset applied directly
to the device took 0.08 s at 30 fps.

## Checking

```bash
curl -s http://192.168.4.65/presets.json | python3 -m json.tool   # what the strip has
```
In HA, `select.master_bedroom_preset`'s `options` attribute is the authoritative list of what the
automations can ask for — if a name is missing there, the corresponding key is dead.
