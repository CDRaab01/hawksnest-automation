# everyday — one second a day

Each day it picks about 1.5 s of Frigate footage for a long-running "one second every day"
video of the family. Hawksnest shows the day's top 5 so you can swap the pick.

## How a day is chosen

1. **21:00, provisional:** scan today, so there's a pick to review before bed.
2. **00:20, final:** rescan yesterday with the evening included, then rebuild the yearly reel.
   **A manual pick is never overwritten.**
3. **Scoring** (`picker.py`): label (person > dog/cat > bird > car), recognised face
   (`sub_label`), several people on one camera at once, sensible duration, detector
   confidence, daylight, a per-camera weight, and a small penalty for repeating yesterday's
   camera. The top 12 are kept, with at most 2 per camera spaced ≥10 min apart.
4. **GenAI:** each of those 12 goes to the **same local LM Studio endpoint Frigate uses**
   (`OPENAI_BASE_URL` from `frigate-credentials`) with its thumbnail and Frigate's
   description, and gets a 1–10 rating that is blended 50/50 with the heuristic. This fails
   closed: with no URL set, or with a public one, it runs on heuristics alone.
5. **Clip:** a window around the event's peak is cut from Frigate's continuous recordings and
   normalised to 1920×1080 letterboxed, 30 fps h264, **no audio**. Every clip shares the
   same parameters, so the reel concatenates by stream copy.

**Timing is load-bearing:** continuous retention is 1–3 days per camera, so a day has to be
scanned within ~24 h. On startup the service backfills the last `BACKFILL_DAYS` (2).

## API (ClusterIP :8080, reached via Hawksnest nginx `/everyday/`)

| | |
|---|---|
| `GET /api/days?limit=30` | summaries, newest first |
| `GET /api/days/YYYY-MM-DD` | full candidates + pick |
| `POST /api/days/YYYY-MM-DD/pick` `{"eventId"}` | manual pick (must be a candidate) |
| `POST /api/days/YYYY-MM-DD/scan` | rescan (async, 202) |
| `GET /api/reel` | yearly reels |
| `GET /media/...` | clips/thumbs/reels, with Range support |

## Storage — `everyday-media` (NFS, `/volume3/home-automation/everyday`) ⭐

**Create that folder in DSM before the first deploy**, or the pod sits in ContainerCreating.
`picks/` and `reel/` are the irreplaceable part, because the source footage is gone within
days. Candidate clips are pruned after 14 days. Expect about 1 MB per day.

## Knobs (env on the Deployment)

`EXCLUDE_CAMERAS` (`bedroom`), `CAMERA_WEIGHTS` (`garage=0.4,front=0.6`; a weight of 0 also
excludes), `CLIP_SECONDS` (1.5), `CANDIDATES` (5), `AI_SHORTLIST` (12), `PROVISIONAL_AT`,
`FINAL_AT`, `OUTPUT_SIZE` (1920x1080), `KEEP_CANDIDATE_DAYS` (14), `GENAI_MODEL`.

## Tests

`python3 -m unittest discover -s everyday/tests` covers the scoring units and runs an
end-to-end scan against a fake Frigate and a fake LM Studio. It needs ffmpeg and runs in CI.
