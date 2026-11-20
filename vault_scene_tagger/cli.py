"""Command-line entry point."""
import argparse
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import GENERATOR, ai_client, media, moments, phar, sidecar, tagger
from .walker import iter_videos

log = logging.getLogger("vault_scene_tagger")


def _parse_since(value):
    """Epoch seconds, or an ISO date/datetime (local time unless it carries an offset)."""
    try:
        return float(value)
    except ValueError:
        pass
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected epoch seconds or an ISO date like 2026-09-01 / 2026-09-01T12:00")
    return dt.timestamp()


def _parse_phases(value):
    phases = [p.strip().lower() for p in value.split(",") if p.strip()]
    for p in phases:
        if p not in tagger.PHASES:
            raise argparse.ArgumentTypeError("unknown phase %r (choose from a, b, c)" % p)
    return phases


ENGINES = ("ai-server", "phar")


def _parse_engines(value):
    if value.strip().lower() == "both":
        return list(ENGINES)
    out = [e.strip().lower() for e in value.split(",") if e.strip()]
    for e in out:
        if e not in ENGINES:
            raise argparse.ArgumentTypeError("unknown engine %r (ai-server, phar, both)" % e)
    return out or ["ai-server"]


def _threshold(value):
    v = float(value)
    if not 0.0 < v < 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return v


def _positive_int(value):
    v = int(value)
    if v < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return v


def build_parser():
    p = argparse.ArgumentParser(
        prog="vault-scene-tagger",
        description="Write <video>.scenes.json scene-marker sidecars next to every video "
                    "under MEDIA_ROOT (format: SCENES_SPEC.md).")
    p.add_argument("media_root", help="library folder to walk (or a single video file)")
    p.add_argument("--phase", dest="phases", type=_parse_phases, default=["c"],
                   help="phase(s) to run, comma-separated: c = AI key moments via the local AI "
                        "server (default), a = raw shot cuts (ffmpeg), b = states (planned)")
    p.add_argument("--workers", type=_positive_int, default=1,
                   help="videos processed in parallel (default 1; keep low on SMB/NAS)")
    p.add_argument("--force", action="store_true", help="redo every video, ignoring resume state")
    p.add_argument("--since", type=_parse_since, metavar="MTIME",
                   help="only consider videos modified at/after this time (epoch or ISO date)")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be processed; analyze and write nothing")

    g = p.add_argument_group("phase c: AI key moments")
    g.add_argument("--engine", dest="engines", type=_parse_engines, default=["ai-server"],
                   help="which analyzer(s): ai-server (skier233 server, acts), phar (local P-HAR "
                        "TimeSformer, positions + acts), or both. Default: ai-server")
    g.add_argument("--ai-url", default="http://127.0.0.1:8000",
                   help="local AI server (skier233 media_ai_model_server); default %(default)s")
    g.add_argument("--ai-frame-interval", type=float, default=2.0,
                   help="seconds between frames the AI analyzes (default 2)")
    g.add_argument("--ai-threshold", type=_threshold, default=0.5,
                   help="per-frame tag confidence the server must reach (default 0.5)")
    g.add_argument("--ai-timeout", type=float, default=14400,
                   help="max seconds to wait for one video (default 4h)")
    g.add_argument("--max-moments", type=_positive_int, default=10,
                   help="key moments kept per video (default 10)")
    g.add_argument("--max-per-label", type=_positive_int, default=2,
                   help="key moments kept per label per video (default 2)")
    g.add_argument("--creampie-after-position", action="store_true",
                   help="drop a creampie whose last preceding act is toy, fingering, blowjob or "
                        "titjob (off by default; leave off for training rounds)")
    g.add_argument("--min-moment", type=float, default=15.0,
                   help="shortest moment in seconds; creampie exempt (default 15)")
    g.add_argument("--keep-cuts", action="store_true",
                   help="keep raw 'scene' cut markers in the sidecar next to the key moments")
    g.add_argument("--label-map", dest="label_map_file", metavar="JSON",
                   help='override tag->label mapping, e.g. {"Doggystyle": "doggy", "Footjob": null}')
    g.add_argument("--skip-manual", action="store_true",
                   help="skip videos that already carry your own (manual) markers — keeps reviewed "
                        "movies free of new AI suggestions, even with --force")
    g.add_argument("--keep-unknown-tags", action="store_true",
                   help="emit tags not in the label map (kebab-cased) instead of ignoring them")

    g = p.add_argument_group("phase c engine: phar (local TimeSformer)")
    g.add_argument("--phar-model", "--phar-checkpoint", dest="phar_checkpoint", default=None,
                   metavar="PATH",
                   help="fine-tuned model directory (models/vault-r<N>/) or P-HAR's timeSformer.pth. "
                        "Default: $VAULT_PHAR_MODEL, else the newest models/vault-r<N>/, else "
                        "models/timeSformer.pth")
    g.add_argument("--phar-device", default="auto", help="auto (default), cpu, mps or cuda")
    g.add_argument("--phar-batch", type=_positive_int, default=4, help="windows per forward pass (default 4)")
    g.add_argument("--phar-stride", type=_positive_int, default=8,
                   help="sampled frames between windows; 8 = every 3.2 s (default)")
    g.add_argument("--phar-threshold", type=_threshold, default=0.35,
                   help="minimum top-1 probability for a window (default 0.35)")
    g.add_argument("--save-scores", action="store_true",
                   help="also write every window's probabilities for all classes (before threshold "
                        "and caps) to <video>.scores.json.gz, for finding exact action starts")
    g.add_argument("--scores-dir", metavar="DIR",
                   help="write the --save-scores files into DIR instead of next to the videos "
                        "(implies --save-scores)")
    g.add_argument("--phar-suppress", default=",".join(phar.SUPPRESS_DEFAULT), metavar="LABELS",
                   help="P-HAR classes to ignore, comma-separated (default %(default)s — it fires "
                        "on rear-entry positions)")

    g = p.add_argument_group("phase a: shot cuts")
    g.add_argument("--scene-threshold", type=_threshold, default=0.3,
                   help="ffmpeg scene-change score 0-1; lower = more cuts (default 0.3)")
    g.add_argument("--min-gap", type=float, default=1.0,
                   help="drop cuts closer than this many seconds to the previous one (default 1.0)")
    g.add_argument("--sprites", action="store_true",
                   help="also write <video>.sprites.jpg + .sprites.vtt (same decode pass)")
    g.add_argument("--sprite-interval", type=float, default=10.0,
                   help="seconds between sprite thumbnails (default 10)")
    g.add_argument("--hwaccel", metavar="API",
                   help="ffmpeg -hwaccel value, e.g. videotoolbox (macOS), cuda, vaapi, auto")
    g.add_argument("--fast-decode", action="store_true",
                   help="skip decoding non-reference frames (faster on H.264; "
                        "cuts may land a few frames late)")

    g = p.add_argument_group("general")
    g.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary (default: from PATH)")
    g.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary (default: from PATH)")
    g.add_argument("--log-file", help="also append the log to this file")
    g.add_argument("--summary-json", metavar="PATH", help="write the run summary as JSON here")
    g.add_argument("-v", "--verbose", action="store_true", help="debug logging (skip reasons etc.)")
    g.add_argument("-q", "--quiet", action="store_true", help="only warnings and the summary")
    g.add_argument("--version", action="version", version=GENERATOR)
    return p


def _setup_logging(opts):
    level = logging.DEBUG if opts.verbose else logging.WARNING if opts.quiet else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
    log.handlers[:] = []
    log.setLevel(logging.DEBUG)
    log.propagate = False
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if opts.log_file:
        fh = logging.FileHandler(opts.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG if opts.verbose else logging.INFO)
        fh.setFormatter(fmt)
        log.addHandler(fh)


def _fmt_secs(s):
    s = int(round(s))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def main(argv=None):
    opts = build_parser().parse_args(argv)
    _setup_logging(opts)

    not_ready = [p for p in opts.phases if not tagger.PHASES[p]["implemented"]]
    if not_ready:
        log.error("phase %s is not implemented in %s yet", ",".join(not_ready), GENERATOR)
        return 2
    if not os.path.exists(opts.media_root):
        log.error("media root not found: %s", opts.media_root)
        return 2
    if not opts.dry_run:
        for tool in (opts.ffmpeg, opts.ffprobe):
            if not shutil.which(tool):
                log.error("%s not found on PATH (install ffmpeg, or pass --ffmpeg/--ffprobe)", tool)
                return 2

    opts.label_map = moments.DEFAULT_LABEL_MAP
    if opts.label_map_file:
        try:
            opts.label_map = moments.load_label_map(opts.label_map_file)
        except (OSError, ValueError) as e:
            log.error("--label-map: %s", e)
            return 2
    opts.ai_client = opts.phar = None
    if "c" in opts.phases and not opts.dry_run and "phar" in opts.engines:
        try:
            opts.phar = phar.PharEngine(
                checkpoint=opts.phar_checkpoint, device=opts.phar_device, batch=opts.phar_batch,
                stride=opts.phar_stride, threshold=opts.phar_threshold,
                suppress=opts.phar_suppress.split(","), ffmpeg=opts.ffmpeg,
                keep_scores=opts.save_scores or bool(opts.scores_dir))
            opts.save_scores = opts.save_scores or bool(opts.scores_dir)
            opts.phar.load()         # fail fast: missing weights / torch
        except phar.PharError as e:
            log.error("%s", e)
            return 2
    if "c" in opts.phases and not opts.dry_run and "ai-server" in opts.engines:
        try:
            opts.ai_client = ai_client.StashAIClient(opts.ai_url, timeout=opts.ai_timeout)
            opts.ai_client.check()
        except ai_client.AIServerError as e:
            log.error("%s", e)
            log.error("phase c needs the local AI server running with a tagging model loaded "
                      "(README: 'AI key moments'), or run --phase a for plain shot cuts")
            return 2

    root = os.path.abspath(opts.media_root)
    started = time.time()
    summary = {
        "generator": GENERATOR, "media_root": root, "phases": opts.phases,
        "started_at": sidecar.utc_now(), "dry_run": opts.dry_run,
        "videos_found": 0, "processed": 0, "skipped": 0, "errors": 0,
        "markers_emitted": 0, "video_seconds": 0.0, "interrupted": False, "failures": [],
    }

    def rel(path):
        r = os.path.relpath(path, root)
        return os.path.basename(path) if r == "." else r

    log.info("%s — scanning %s (phase %s%s)", GENERATOR, root, ",".join(opts.phases),
             ", engine " + ",".join(opts.engines) if "c" in opts.phases else "")
    todo = []
    for video in iter_videos(root):
        summary["videos_found"] += 1
        try:
            needed, reason = tagger.needs_work(video, opts)
        except OSError as e:
            log.warning("skip %s: %s", rel(video), e)
            summary["errors"] += 1
            summary["failures"].append({"video": video, "error": str(e)})
            continue
        if needed:
            todo.append((video, reason))
        else:
            summary["skipped"] += 1
            log.debug("skip (%s): %s", reason, rel(video))
    log.info("%d videos found, %d to process, %d up to date",
             summary["videos_found"], len(todo), summary["skipped"])

    if opts.dry_run:
        for video, reason in todo:
            print("would process (%s): %s" % (reason, rel(video)))
        return _finish(summary, started, opts)

    stop = threading.Event()
    total = len(todo)

    def work(item):
        video, reason = item
        if stop.is_set():
            return None
        log.info("-> %s (%s)", rel(video), reason)
        return tagger.process(video, opts, stop_event=stop)

    prev_term = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        raise KeyboardInterrupt

    in_main = threading.current_thread() is threading.main_thread()
    if in_main:
        signal.signal(signal.SIGTERM, _on_term)

    expected_errors = (media.MediaError, sidecar.SidecarError, ai_client.AIServerError,
                       phar.PharError, OSError)
    done = 0
    executor = ThreadPoolExecutor(max_workers=opts.workers)
    futures = {executor.submit(work, item): item[0] for item in todo}
    try:
        for fut in as_completed(futures):
            video = futures[fut]
            done += 1
            try:
                res = fut.result()
            except expected_errors as e:
                if stop.is_set():
                    continue
                summary["errors"] += 1
                summary["failures"].append({"video": video, "error": str(e)})
                log.error("[%d/%d] FAILED %s: %s", done, total, rel(video), e)
                continue
            except Exception as e:  # never let one bad file kill a long run
                summary["errors"] += 1
                summary["failures"].append({"video": video, "error": repr(e)})
                log.exception("[%d/%d] FAILED %s", done, total, rel(video))
                continue
            if res is None:
                continue
            summary["processed"] += 1
            summary["markers_emitted"] += res["markers"]
            summary["video_seconds"] += res["duration"]
            log.info("[%d/%d] %d markers, %s video in %s  %s", done, total, res["markers"],
                     _fmt_secs(res["duration"]), _fmt_secs(res["seconds"]), rel(video))
    except KeyboardInterrupt:
        summary["interrupted"] = True
        log.warning("interrupted — stopping analysis; finished sidecars are kept, re-run to resume")
        stop.set()
        for fut in futures:
            fut.cancel()
        media.terminate_all()
        ai_client.abort_all()
    finally:
        executor.shutdown(wait=True)
        if in_main:
            signal.signal(signal.SIGTERM, prev_term)

    return _finish(summary, started, opts)


def _finish(summary, started, opts):
    summary["finished_at"] = sidecar.utc_now()
    summary["elapsed_seconds"] = round(time.time() - started, 1)
    summary["video_seconds"] = round(summary["video_seconds"], 1)
    log.info("summary: %d found, %d processed, %d skipped, %d errors, %d markers, %s elapsed%s",
             summary["videos_found"], summary["processed"], summary["skipped"],
             summary["errors"], summary["markers_emitted"],
             _fmt_secs(summary["elapsed_seconds"]),
             " (interrupted)" if summary["interrupted"] else "")
    if opts.quiet:
        print(json.dumps({k: v for k, v in summary.items() if k != "failures"}))
    if opts.summary_json:
        with open(opts.summary_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
    if summary["interrupted"]:
        return 130
    return 1 if summary["errors"] else 0
