"""Per-video pipeline: decide whether work is needed, run the phases, merge, write."""
import gzip
import json
import logging
import os
import time

from . import media, moments, sidecar
from .walker import SPRITES_JPG_SUFFIX, SPRITES_VTT_SUFFIX, companion_path, sidecar_path

log = logging.getLogger(__name__)

# Phase registry. Each phase owns the marker sources it replaces on re-run (§5).
PHASES = {
    "a": {"sources": {"cut"}, "implemented": True},        # shot cuts (ffmpeg)
    "b": {"sources": {"heuristic"}, "implemented": False},  # coarse states (planned)
    "c": {"sources": {"ml"}, "implemented": True},          # AI key moments
}


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _cut_times(doc):
    markers = doc.get("markers") if isinstance(doc, dict) else None
    if not isinstance(markers, list):
        return []
    return sorted(float(m["t"]) for m in markers
                  if isinstance(m, dict) and m.get("source") == "cut" and sidecar._num(m.get("t")))


def _drop_user_rejected(found, doc, window=10.0):
    """Don't re-suggest AI moments the user already dealt with in the reader.

    Vault records a deleted AI marker in the sidecar's top-level ``rejected`` list, and keeps
    the original of a moved/relabeled AI marker under that (now manual) marker's ``was``.
    A new moment with the same label starting within ``window`` seconds of either is skipped.
    """
    seen = []
    if isinstance(doc, dict):
        if isinstance(doc.get("rejected"), list):
            seen += [r for r in doc["rejected"] if isinstance(r, dict)]
        if isinstance(doc.get("markers"), list):
            seen += [m["was"] for m in doc["markers"] if isinstance(m, dict) and isinstance(m.get("was"), dict)]
    seen = [(s.get("label"), float(s["t"])) for s in seen if sidecar._num(s.get("t"))]
    if not seen:
        return found
    return [m for m in found
            if not any(label == m["label"] and abs(t - m["t"]) <= window for label, t in seen)]


def sprites_fresh(video, video_mtime):
    for suffix in (SPRITES_JPG_SUFFIX, SPRITES_VTT_SUFFIX):
        m = _mtime(companion_path(video, suffix))
        if m is None or m < video_mtime:
            return False
    return True


def needs_work(video, opts):
    """Return (bool, reason)."""
    video_mtime = os.stat(video).st_mtime
    if opts.since is not None and video_mtime < opts.since:
        return False, "older than --since"
    sc = sidecar_path(video)
    if getattr(opts, "skip_manual", False):
        # don't drop fresh AI suggestions into a movie you already reviewed by hand
        try:
            doc = sidecar.read(sc)
        except sidecar.SidecarError:
            doc = None
        if doc and any(isinstance(m, dict) and m.get("source") == "manual"
                       for m in (doc.get("markers") or [])):
            return False, "already has your markers"
    if opts.force:
        return True, "forced"
    sc_mtime = _mtime(sc)
    if sc_mtime is None:
        return True, "no sidecar"
    if sc_mtime < video_mtime:
        return True, "video newer than sidecar"
    try:
        doc = sidecar.read(sc)
    except sidecar.SidecarError:
        return True, "unreadable sidecar"
    missing = set(opts.phases) - sidecar.phases_done(doc)
    if missing:
        return True, "phase %s not done" % ",".join(sorted(missing))
    if "c" in opts.phases:
        engines = set(getattr(opts, "engines", ()) or ()) - sidecar.engines_done(doc)
        if engines:
            return True, "engine %s not done" % ",".join(sorted(engines))
    if opts.sprites and not sprites_fresh(video, video_mtime):
        return True, "sprites missing"
    return False, "up to date"


def _commit_sprites(video, tmp_jpg, plan, duration):
    jpg = companion_path(video, SPRITES_JPG_SUFFIX)
    vtt = companion_path(video, SPRITES_VTT_SUFFIX)
    if not os.path.isfile(tmp_jpg) or os.path.getsize(tmp_jpg) == 0:
        raise media.MediaError("ffmpeg produced no sprite sheet")
    vtt_data = media.sprites_vtt(plan, os.path.basename(jpg), duration).encode("utf-8")
    os.replace(tmp_jpg, jpg)
    try:
        sidecar.write_bytes_atomic(vtt, vtt_data)
    except BaseException:
        sidecar.discard(jpg)  # both or neither
        raise


SCORES_SUFFIX = ".scores.json.gz"


def scores_path(video, scores_dir=None):
    """Where --save-scores writes a video's per-window probability track: next to the video,
    or ``<scores_dir>/<video name>.scores.json.gz`` (keeps the library free of extra files)."""
    if scores_dir:
        return os.path.join(scores_dir, os.path.splitext(os.path.basename(video))[0] + SCORES_SUFFIX)
    return companion_path(video, SCORES_SUFFIX)


def _write_scores(path, track, video, duration):
    doc = dict(track, video=os.path.basename(video), duration=duration, generated_at=sidecar.utc_now())
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "." + os.path.basename(path) + ".tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def process(video, opts, stop_event=None):
    """Run the requested phases for one video. Returns {"markers", "seconds", "duration"}.

    Raises media.MediaError / sidecar.SidecarError / ai_client.AIServerError / OSError on
    failure; nothing is written in that case.
    """
    t0 = time.time()
    sc = sidecar_path(video)

    # Fail fast (before a long decode) if an existing sidecar can't be safely merged.
    existing = sidecar.read(sc)

    info = media.probe(video, ffprobe=opts.ffprobe)
    if not info["has_video"]:
        raise media.MediaError("no video stream")
    duration = info["duration"]
    if not duration:
        raise media.MediaError("unknown duration")

    # With AI key moments, raw cuts are only used internally (to align moment starts)
    # and removed from the sidecar, unless --keep-cuts.
    hide_cuts = "c" in opts.phases and not getattr(opts, "keep_cuts", False)

    plan = tmp_jpg = None
    if opts.sprites and "a" in opts.phases:
        plan = media.plan_sprites(duration, opts.sprite_interval)
        tmp_jpg = sidecar.temp_path(companion_path(video, SPRITES_JPG_SUFFIX), ".jpg")

    new_markers = []
    owned = set()
    params = {}
    try:
        cuts = _cut_times(existing)
        if "a" in opts.phases:
            cuts = media.detect_cuts(
                video, threshold=opts.scene_threshold, min_gap=opts.min_gap,
                duration=duration, ffmpeg=opts.ffmpeg, hwaccel=opts.hwaccel,
                fast_decode=opts.fast_decode, sprite_plan=plan, sprite_out=tmp_jpg,
                stop_event=stop_event)
            params["a"] = {"scene_threshold": opts.scene_threshold, "min_gap": opts.min_gap}
            if not hide_cuts:
                new_markers += [{"t": t, "label": "scene", "source": "cut"} for t in cuts]
                owned |= PHASES["a"]["sources"]

        if "c" in opts.phases:
            runs = []
            if "ai-server" in opts.engines:
                runs.append(("ai-server", opts.ai_client.process_video(
                    video, frame_interval=opts.ai_frame_interval, threshold=opts.ai_threshold)))
            if "phar" in opts.engines:
                # "phar" = P-HAR's original weights, "phar:vault-r1" = a fine-tuned round
                runs.append((getattr(opts.phar, "name", "phar"),
                             opts.phar.analyze(video, stop_event=stop_event)))
            if stop_event is not None and stop_event.is_set():
                raise media.MediaError("interrupted")
            for name, result in runs:
                if result.get("scores") and getattr(opts, "save_scores", False):
                    sp = scores_path(video, getattr(opts, "scores_dir", None))
                    _write_scores(sp, result["scores"], video, duration)
                    log.debug("%s: %d window scores -> %s", video, len(result["scores"]["t"]), sp)
            per_engine, detections = [], 0
            for name, result in runs:
                detections += len(result["spans"])
                ms = moments.key_moments(
                    result["spans"], duration,
                    frame_interval=result["frame_interval"] or opts.ai_frame_interval,
                    max_moments=opts.max_moments, min_seconds=opts.min_moment, cuts=cuts,
                    max_per_label=getattr(opts, "max_per_label", 2),
                    climax_needs_position=getattr(opts, "creampie_after_position", False),
                    label_map=opts.label_map, keep_unknown=opts.keep_unknown_tags)
                for m in ms:
                    m["engine"] = name        # extra field: which engine proposed it (readers ignore)
                per_engine.append(ms)
                log.debug("%s: %s -> %d detections, %d moments", video, name, len(result["spans"]), len(ms))
            found = moments.merge_engines(per_engine, max_moments=opts.max_moments)
            found = _drop_user_rejected(found, existing)
            new_markers += found
            owned |= PHASES["c"]["sources"]
            params["c"] = {"engines": sorted(opts.engines), "max_moments": opts.max_moments,
                           "max_per_label": getattr(opts, "max_per_label", 2),
                           "creampie_after_position": getattr(opts, "creampie_after_position", False),
                           "min_moment": opts.min_moment, "detections": detections,
                           "frame_interval": opts.ai_frame_interval, "threshold": opts.ai_threshold}
            if "phar" in opts.engines:
                params["c"]["phar_model"] = getattr(opts.phar, "name", "phar")
            log.debug("%s: %d detections -> %d key moments", video, detections, len(found))
        if hide_cuts:
            owned |= PHASES["a"]["sources"]

        # Re-read right before writing: the reader may have added manual markers while the
        # analysis was running, and those must survive.
        existing = sidecar.read(sc)
        ran = [p for p in opts.phases if not (p == "a" and hide_cuts)]
        doc = sidecar.merge(existing, new_markers, owned, os.path.basename(video),
                            duration, ran, params, drop_phases=("a",) if hide_cuts else ())
        problems = sidecar.validate(doc)
        if problems:
            log.warning("%s: sidecar has schema problems (kept as-is): %s",
                        video, "; ".join(problems[:5]))
        if plan:
            _commit_sprites(video, tmp_jpg, plan, duration)
        sidecar.write_atomic(sc, doc)
    finally:
        if tmp_jpg:
            sidecar.discard(tmp_jpg)

    return {"markers": len(new_markers), "seconds": time.time() - t0, "duration": duration}
