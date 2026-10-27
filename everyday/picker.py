"""
Pure scoring/selection logic for `everyday` — no I/O, so it is unit-testable without
Frigate, ffmpeg or a model. server.py does the fetching and calls into this.

The question this answers: of the ~hundreds of Frigate events a day, which handful are
worth one and a half seconds of a family video? Frigate's events are mostly "person
walked through the kitchen". The heuristic prefers several people at once, pets,
recognised faces (sub_label), daylight, and a duration that suggests something happened
rather than a pass-through or someone parked on the couch. A local model then rates the
shortlist; its rating and the heuristic are blended so a model hiccup can never pick a
garage clip of the car over the kids.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import re

LABEL_WEIGHTS = {
    "person": 1.0,
    "dog": 0.75,
    "cat": 0.7,
    "bird": 0.45,
    "car": 0.12,
}
DEFAULT_LABEL_WEIGHT = 0.2


@dataclass
class Candidate:
    event_id: str
    camera: str
    label: str
    sub_label: str | None
    start: float
    end: float
    top_score: float
    description: str
    heuristic: float
    reasons: list[str] = field(default_factory=list)
    ai_score: float | None = None
    final: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def peak_time(self) -> float:
        """Where in the event to take the clip. Not the start: the first second is usually
        someone entering frame. ~30% in, between 1 s and 6 s from the start."""
        return self.start + min(max(self.duration * 0.3, 1.0), 6.0)

    def to_json(self) -> dict:
        return {
            "eventId": self.event_id,
            "camera": self.camera,
            "label": self.label,
            "subLabel": self.sub_label,
            "start": self.start,
            "end": self.end,
            "peak": self.peak_time(),
            "topScore": round(self.top_score, 3),
            "description": self.description,
            "heuristic": round(self.heuristic, 3),
            "aiScore": self.ai_score,
            "final": round(self.final, 3),
            "reasons": self.reasons,
        }


def parse_weights(spec: str) -> dict[str, float]:
    """`"garage=0.4,front=0.6"` -> {"garage": 0.4, "front": 0.6}. Bad entries are skipped
    rather than raised: a typo in an env var must not stop the nightly run."""
    out: dict[str, float] = {}
    for part in (spec or "").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out


def parse_list(spec: str) -> set[str]:
    return {p.strip() for p in (spec or "").split(",") if p.strip()}


def day_bounds(date: str, tz: tzinfo) -> tuple[float, float]:
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def _overlapping_people(ev: dict, people_by_camera: dict[str, list[tuple[float, float]]]) -> int:
    s, e = ev["start_time"], ev["end_time"]
    return sum(
        1
        for (ps, pe) in people_by_camera.get(ev["camera"], [])
        if ps < e and pe > s and (ps, pe) != (s, e)
    )


def score_events(
    events: list[dict],
    *,
    tz: tzinfo,
    exclude_cameras: set[str],
    camera_weights: dict[str, float],
    previous_camera: str | None = None,
) -> list[Candidate]:
    """Heuristic score for every usable event. Returns candidates sorted best-first."""
    usable = [
        e
        for e in events
        if e.get("end_time")
        and not e.get("false_positive")
        and e.get("camera") not in exclude_cameras
        and camera_weights.get(e.get("camera"), 1.0) > 0
    ]
    people_by_camera: dict[str, list[tuple[float, float]]] = {}
    for e in usable:
        if e.get("label") == "person":
            people_by_camera.setdefault(e["camera"], []).append((e["start_time"], e["end_time"]))

    out: list[Candidate] = []
    for e in usable:
        data = e.get("data") or {}
        label = e.get("label") or "?"
        sub = e.get("sub_label")
        if isinstance(sub, list):  # Frigate has returned [name, score] in some versions
            sub = sub[0] if sub else None
        top = float(data.get("top_score") or e.get("top_score") or data.get("score") or 0.5)
        dur = float(e["end_time"]) - float(e["start_time"])
        reasons: list[str] = []

        h = LABEL_WEIGHTS.get(label, DEFAULT_LABEL_WEIGHT)
        reasons.append(f"{label}")
        if sub and label == "person":
            h += 0.3
            reasons.append(f"recognised {sub}")
        elif sub:
            h += 0.05

        others = _overlapping_people(e, people_by_camera) if label in ("person", "dog", "cat") else 0
        if others:
            h += min(others, 3) * 0.25
            reasons.append(f"+{others} people together")

        if dur < 2:
            h *= 0.3
            reasons.append("too brief")
        elif dur > 600:
            h *= 0.7
            reasons.append("long/static")

        h *= 0.5 + 0.5 * max(0.0, min(top, 1.0))

        hour = datetime.fromtimestamp(e["start_time"], tz).hour
        if 7 <= hour < 21:
            h += 0.1
        else:
            reasons.append("night")

        w = camera_weights.get(e["camera"], 1.0)
        if w != 1.0:
            h *= w
            reasons.append(f"camera x{w:g}")
        if previous_camera and e["camera"] == previous_camera:
            h *= 0.85
            reasons.append("same camera as yesterday")

        out.append(
            Candidate(
                event_id=str(e["id"]),
                camera=e["camera"],
                label=label,
                sub_label=sub,
                start=float(e["start_time"]),
                end=float(e["end_time"]),
                top_score=top,
                description=str(data.get("description") or e.get("description") or ""),
                heuristic=h,
                reasons=reasons,
            )
        )
    out.sort(key=lambda c: c.heuristic, reverse=True)
    return out


def shortlist(cands: list[Candidate], n: int, *, per_camera: int = 2, gap_s: float = 600) -> list[Candidate]:
    """Best-first, but varied: at most `per_camera` from one camera, and never two from the
    same camera within `gap_s` of each other (one moment shouldn't fill the whole top 5)."""
    picked: list[Candidate] = []
    for c in cands:
        same = [p for p in picked if p.camera == c.camera]
        if len(same) >= per_camera:
            continue
        if any(abs(p.start - c.start) < gap_s for p in same):
            continue
        picked.append(c)
        if len(picked) >= n:
            break
    return picked


def parse_ai_score(text: str) -> float | None:
    """Pull a 1-10 rating out of a model reply. Prefers `"score": N`; falls back to the
    LAST bare number (reasoning models think out loud before answering)."""
    if not text:
        return None
    m = re.search(r'"?score"?\s*[:=]\s*(\d+(?:\.\d+)?)', text, re.I)
    nums = [m.group(1)] if m else re.findall(r"\b(\d+(?:\.\d+)?)\b", text)
    for raw in reversed(nums):
        v = float(raw)
        if 0 <= v <= 10:
            return v
    return None


def finalize(cands: list[Candidate]) -> list[Candidate]:
    """Blend heuristic (normalised to the day's best) with the AI rating. A candidate the
    model didn't rate keeps its heuristic alone, so a model outage degrades to heuristics."""
    if not cands:
        return cands
    top = max(c.heuristic for c in cands) or 1.0
    for c in cands:
        norm = c.heuristic / top
        c.final = norm if c.ai_score is None else 0.5 * norm + 0.5 * (c.ai_score / 10)
    cands.sort(key=lambda c: c.final, reverse=True)
    return cands


AI_PROMPT = (
    "You are choosing one and a half seconds per day from a family's home cameras for a "
    "'one second every day' video of their life. Rate how memorable and warm this moment "
    "would be in that video, from 1 (empty room, someone walking past, a car) to 10 "
    "(kids playing, family together, a pet doing something funny, a milestone). "
    'Reply with JSON only: {"score": N}.'
)
