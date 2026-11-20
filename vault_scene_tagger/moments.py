"""Phase C: reduce per-tag detection spans to a handful of key moments.

The AI server reports, per tag, when the tag was detected (sampled every
``frame_interval`` seconds). A movie easily yields hundreds of short spans; a player
wants ~10 meaningful ranges. Pipeline:

1. map server tag names to the SCENES_SPEC §4 vocabulary (unmapped tags are ignored
   unless ``keep_unknown``) — keeps the label set small and honest;
2. merge a label's spans separated by at most ``max_gap`` seconds into segments;
3. drop segments shorter than ``min_seconds`` (climax labels need only one frame)
   and segments where the label was present in too few sampled frames;
4. rank climax labels first, then by confidence x log(length); keep at most
   ``max_per_label`` per label and ``max_moments`` overall;
5. optionally snap each start to a nearby shot cut, so a jump lands on a clean edit.
"""
import json
import math
import re

from .sidecar import LABEL_RE

# normalize_tag(server tag) -> vault label. ``None`` means "ignore this tag".
# Includes the tag names of skier233's models (free "vivid galaxy": 69, Anal Fucking, Blowjob,
# Cumshot, Fingering, Handjob, Kissing, Pussy Licking, Pussy Rubbing, Vaginal Fucking).
DEFAULT_LABEL_MAP = {
    # dropped labels (kissing, foreplay, deepthroat, masturbation, facial) map to None or to the
    # label that absorbed them: facial -> cumshot
    "kissing": None, "kiss": None, "fondling": None, "foreplay": None,
    "fingering": "fingering", "pussyrubbing": "fingering",
    "handjob": None,
    "blowjob": "blowjob", "fellatio": "blowjob", "oral": "blowjob", "lickingpenis": "blowjob",
    "deepthroat": None, "deepthroat2": None,
    "titjob": "titjob", "titfuck": "titjob", "boobjob": "titjob", "paizuri": "titjob",
    "cunnilingus": None, "pussylicking": None, "eatingpussy": None,
    "pussylickingclearlyvisible": None,
    "69": None, "sixtynine": None,
    # generic intercourse tags are dropped on purpose: Vault uses the position labels instead
    "sex": None, "vaginalsex": None, "vaginalpenetration": None, "penetration": None,
    "fucking": None, "vaginalfucking": None,
    "anal": None, "analsex": None, "analpenetration": None, "analfucking": None,
    "masturbation": None, "masturbating": None,
    "toy": "toy", "toys": "toy", "sextoy": "toy", "dildo": "toy", "vibrator": "toy",
    "vibrating": "toy", "suckingtoydildo": "toy",
    # anal, cunnilingus, handjob and 69 were dropped from the label set (2026-09-25).
    # A detected "cumshot" is re-used as creampie: in this library the climax almost always is one,
    # and the detector is good at finding the moment even when it names it wrong. A facial or a
    # cum-in-mouth is NOT a creampie, so those stay dropped.
    "facial": None, "facialcumshot": None, "cuminmouth": None,
    "cumshot": "creampie", "cum": "creampie", "ejaculation": "creampie",
    "cumonperson": "creampie",
    "creampie": "creampie",
    "missionary": "missionary",
    "doggy": "doggy", "doggystyle": "doggy", "doggiestyle": "doggy",
    "cowgirl": "cowgirl",
    "reversecowgirl": "cowgirl",   # folded into cowgirl (2026-09-25)
    "standing": "standing", "standingsex": "standing",
    "spooning": None,
    "sideways": None, "sidefuck": None,
    # the-snake (prone bone) folded into doggy (2026-10-11): same entry, she is just lying
    # flat. Too fine a distinction to learn, and it had 11 examples in 10 movies.
    "thesnake": "doggy", "snake": "doggy", "pronebone": "doggy",   # P-HAR positions
    "scoopup": "scoop-up",
}

# Short but important: kept even when only a single frame was detected, and ranked higher.
CLIMAX_LABELS = frozenset({"creampie"})
# A creampie ends intercourse, so the last act before it is a position. When the last act the
# model saw is one of these instead, the "creampie" is usually a close-up of something else (a toy,
# a finger, a mouth). Opt-in (--creampie-after-position), for everyday tagging: the training rounds
# leave it off so the review measures the model itself. Round 1 (2026-11-01): it would have removed
# 18 of 29 wrong creampie suggestions and lost 1 of 30 right ones; no confidence threshold comes
# close (0.65 removes 19 but loses 3).
NOT_BEFORE_CLIMAX = frozenset({"toy", "fingering", "blowjob", "titjob"})
MIN_CONFIDENCE = 0.4


def normalize_tag(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _kebab(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or None


def load_label_map(path):
    """JSON object {"Server Tag": "vault-label" | null}, merged over the defaults."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("label map must be a JSON object")
    merged = dict(DEFAULT_LABEL_MAP)
    for tag, label in data.items():
        if label is not None and (not isinstance(label, str) or not LABEL_RE.match(label)):
            raise ValueError("label map: %r -> %r must be lowercase-kebab or null" % (tag, label))
        merged[normalize_tag(tag)] = label
    return merged


def map_label(tag, label_map, keep_unknown=False):
    key = normalize_tag(tag)
    if key in label_map:
        return label_map[key]
    return _kebab(tag) if keep_unknown else None


def merge_engines(marker_lists, max_moments=10, same_label_window=20.0):
    """Combine the key moments of several engines into one capped, sorted list.

    Engines overlap (e.g. both find the same blowjob): keep the more confident of two moments with
    the same label starting within ``same_label_window`` seconds. Climax labels keep their priority.
    """
    merged = []
    for markers in marker_lists:
        for m in markers:
            dup = next((x for x in merged if x["label"] == m["label"]
                        and abs(x["t"] - m["t"]) <= same_label_window), None)
            if dup is None:
                merged.append(dict(m))
            elif m.get("confidence", 0) > dup.get("confidence", 0):
                merged[merged.index(dup)] = dict(m)
    merged.sort(key=lambda m: (m["label"] not in CLIMAX_LABELS, -m.get("confidence", 0), m["t"]))
    merged = merged[:max_moments]
    merged.sort(key=lambda m: (m["t"], m.get("end", m["t"]), m["label"]))
    return merged


def _snap(t, end, cuts, window):
    if not cuts or window <= 0:
        return t
    best = min(cuts, key=lambda c: abs(c - t))
    return best if abs(best - t) <= window and best < end else t


def _drop_climax_after_non_position(candidates):
    """Drop climax candidates whose last preceding act (latest start before it, any distance)
    is in NOT_BEFORE_CLIMAX. No preceding act at all: kept."""
    acts = sorted((c for c in candidates if not c["climax"]), key=lambda c: c["start"])
    keep = []
    for c in candidates:
        if c["climax"]:
            prev = [a for a in acts if a["start"] < c["start"]]
            if prev and prev[-1]["label"] in NOT_BEFORE_CLIMAX:
                continue
        keep.append(c)
    return keep


def key_moments(spans, duration, frame_interval=2.0, max_moments=10, min_seconds=15.0,
                max_gap=8.0, max_per_label=2, cuts=None, snap_window=5.0,
                label_map=None, keep_unknown=False, min_confidence=MIN_CONFIDENCE,
                climax_needs_position=False):
    """``spans``: [{"tag", "start", "end" (or None), "confidence" (or None)}] in seconds.

    Returns SCENES_SPEC markers (ranged, ``source: "ml"``, with ``confidence``), sorted by t.
    ``confidence`` is the share of sampled frames inside the moment where the label was
    detected, times the server's mean confidence when it reports one.
    """
    label_map = DEFAULT_LABEL_MAP if label_map is None else label_map
    frame_interval = float(frame_interval or 2.0)

    by_label = {}
    for s in spans:
        label = map_label(s.get("tag"), label_map, keep_unknown)
        if not label:
            continue
        try:
            start = max(0.0, float(s["start"]))
            end = float(s["end"]) if s.get("end") is not None else start
        except (KeyError, TypeError, ValueError):
            continue
        end += frame_interval               # a detection covers its sampling interval
        if duration:
            end = min(end, float(duration))
        if end <= start:
            continue
        by_label.setdefault(label, []).append((start, end, s.get("confidence")))

    candidates = []
    for label, items in by_label.items():
        items.sort(key=lambda x: (x[0], x[1]))
        segments = []
        for start, end, conf in items:
            if segments and start - segments[-1]["end"] <= max_gap:
                seg = segments[-1]
                overlap = max(0.0, seg["end"] - start)
                seg["covered"] += max(0.0, end - start - overlap)
                seg["end"] = max(seg["end"], end)
            else:
                seg = {"start": start, "end": end, "covered": end - start, "confs": []}
                segments.append(seg)
            if conf is not None:
                seg["confs"].append(float(conf))

        climax = label in CLIMAX_LABELS
        need = frame_interval if climax else float(min_seconds)
        for seg in segments:
            length = seg["end"] - seg["start"]
            if length + 1e-6 < need:
                continue
            density = min(1.0, seg["covered"] / length)
            mean_conf = sum(seg["confs"]) / len(seg["confs"]) if seg["confs"] else 1.0
            confidence = density * mean_conf
            if confidence < min_confidence:
                continue
            score = confidence * math.log1p(length)
            candidates.append({"label": label, "start": seg["start"], "end": seg["end"],
                               "confidence": confidence, "score": score, "climax": climax})

    if climax_needs_position:
        candidates = _drop_climax_after_non_position(candidates)

    # Climax moments are rare and the most-wanted jump targets: rank them first (still
    # capped by max_per_label), then everything else by confidence x log(length).
    candidates.sort(key=lambda c: (not c["climax"], -c["score"], c["start"]))
    chosen, per_label = [], {}
    for c in candidates:
        if len(chosen) >= max_moments:
            break
        if per_label.get(c["label"], 0) >= max_per_label:
            continue
        per_label[c["label"]] = per_label.get(c["label"], 0) + 1
        chosen.append(c)

    markers = []
    for c in chosen:
        t = round(_snap(c["start"], c["end"], cuts, snap_window), 1)
        end = round(c["end"], 1)
        if end <= t:
            end = round(t + frame_interval, 1)
        markers.append({"t": t, "end": end, "label": c["label"],
                        "confidence": round(min(1.0, max(0.01, c["confidence"])), 2),
                        "source": "ml"})
    markers.sort(key=lambda m: (m["t"], m["end"], m["label"]))
    return markers
