"""ffprobe / ffmpeg wrappers: duration, shot-cut detection, optional scrub sprites."""
import collections
import json
import math
import re
import subprocess
import threading
from urllib.parse import quote

SPRITE_W = 160
SPRITE_H = 90
SPRITE_COLS = 10
MAX_SPRITE_TILES = 1000     # keeps the sheet well under JPEG's 65500px limit
ANALYSIS_WIDTH = 320        # scene scores are computed on downscaled frames (much cheaper)

_SHOWINFO_RE = re.compile(
    r"\[Parsed_showinfo_\d+ @ [^\]]*\].*?\bpts_time:\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")

_procs = set()
_procs_lock = threading.Lock()


class MediaError(Exception):
    pass


def terminate_all():
    """Stop every running ffmpeg child (used on Ctrl+C / SIGTERM).

    SIGKILL, not SIGTERM: ffmpeg treats SIGTERM as "finish up gracefully" and can keep
    decoding a long file for minutes, and its only outputs here are temp files we discard.
    """
    with _procs_lock:
        procs = list(_procs)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def probe(path, ffprobe="ffprobe"):
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_entries", "format=duration:stream=codec_type,duration:stream_disposition=attached_pic",
           path]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        raise MediaError("ffprobe timed out")
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError("ffprobe failed: %s" % (msg[-1] if msg else "exit %d" % r.returncode))
    try:
        info = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise MediaError("ffprobe returned unparseable output")

    streams = info.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]

    def _f(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and v > 0 else None

    duration = _f((info.get("format") or {}).get("duration"))
    if duration is None:
        durations = [d for d in (_f(s.get("duration")) for s in videos) if d]
        duration = max(durations) if durations else None
    return {"duration": duration, "has_video": bool(videos)}


def plan_sprites(duration, interval):
    interval = max(float(interval), math.ceil(duration / MAX_SPRITE_TILES))
    count = max(1, int(math.ceil(duration / interval - 1e-6)))
    cols = min(SPRITE_COLS, count)
    rows = int(math.ceil(count / float(cols)))
    return {"interval": interval, "count": count, "cols": cols, "rows": rows,
            "w": SPRITE_W, "h": SPRITE_H}


def _vtt_ts(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


def sprites_vtt(plan, jpg_name, duration):
    url = quote(jpg_name)
    out = ["WEBVTT", ""]
    for i in range(plan["count"]):
        start = i * plan["interval"]
        end = min((i + 1) * plan["interval"], duration)
        if end <= start:
            break
        x = (i % plan["cols"]) * plan["w"]
        y = (i // plan["cols"]) * plan["h"]
        out.append("%s --> %s" % (_vtt_ts(start), _vtt_ts(end)))
        out.append("%s#xywh=%d,%d,%d,%d" % (url, x, y, plan["w"], plan["h"]))
        out.append("")
    return "\n".join(out)


def detect_cuts(path, threshold=0.3, min_gap=1.0, duration=None, ffmpeg="ffmpeg",
                hwaccel=None, fast_decode=False, sprite_plan=None, sprite_out=None,
                stop_event=None):
    """Run one ffmpeg decode pass; return sorted cut timestamps (seconds, file-relative).

    If ``sprite_plan``/``sprite_out`` are given, the same pass also renders the sprite
    sheet to ``sprite_out`` (so the video is read over the network only once).
    """
    detect = ("scale=w='min(%d,iw)':h=-2,select='gt(scene,%s)',showinfo"
              % (ANALYSIS_WIDTH, repr(float(threshold))))
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel]
    if fast_decode:
        cmd += ["-skip_frame", "noref"]
    cmd += ["-i", path]

    if sprite_plan and sprite_out:
        p = sprite_plan
        graph = (
            "[0:V:0]setpts=PTS-STARTPTS,split=2[d][s];"
            "[d]%s[dout];"
            "[s]fps=1/%s,trim=end_frame=%d,"
            "scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,tile=%dx%d[sp]"
            % (detect, repr(float(p["interval"])), p["count"], p["w"], p["h"],
               p["w"], p["h"], p["cols"], p["rows"]))
        cmd += ["-filter_complex", graph,
                "-map", "[dout]", "-f", "null", "-",
                "-map", "[sp]", "-c:v", "mjpeg", "-q:v", "5", "-f", "image2", "-update", "1",
                "-y", sprite_out]
    else:
        cmd += ["-map", "0:V:0", "-an", "-sn", "-dn",
                "-vf", "setpts=PTS-STARTPTS," + detect, "-f", "null", "-"]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    except OSError as e:
        raise MediaError("cannot run ffmpeg: %s" % e)
    with _procs_lock:
        _procs.add(proc)
    raw_cuts = []
    tail = collections.deque(maxlen=20)
    try:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            m = _SHOWINFO_RE.search(line)
            if m:
                raw_cuts.append(float(m.group(1)))
            elif "Parsed_showinfo" not in line and line:
                tail.append(line)
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stderr.close()
        with _procs_lock:
            _procs.discard(proc)

    if stop_event is not None and stop_event.is_set():
        raise MediaError("interrupted")
    if rc != 0:
        errors = [l for l in tail if "rror" in l] or list(tail)
        raise MediaError("ffmpeg exit %d: %s" % (rc, errors[-1] if errors else "no output"))
    return clean_cuts(raw_cuts, min_gap, duration)


def clean_cuts(raw, min_gap=1.0, duration=None):
    """Sort, clamp to the file, drop a cut at 0 and flashes closer than ``min_gap``."""
    cuts = []
    for t in sorted(raw):
        if t <= 0.05 or (duration and t >= duration):
            continue
        if cuts and t - cuts[-1] < min_gap:
            continue
        cuts.append(round(t, 3))
    return cuts
