"""Scrub previews: a strip of small frames along each video, for the player's seek bar.

Per video part: pages of 100 frames (a 10x10 grid of 320x180, WebP when this ffmpeg can write it,
else JPEG) plus a small JSON index with each frame's time, in <cache>/scrub/. Pages keep any one
image small for the browser (a 5-hour movie as one sheet would be 100+ megapixels). It's a cache:
Clear deletes it and it is simply made again.

Speed is the whole design. Decoding a movie end to end means reading all of it over the network
(a 2 GB file at ~90 MB/s on a NAS), so instead each frame is one seek: ffmpeg jumps to the
keyframe before the wanted time and decodes only that keyframe (-skip_frame nokey). That costs
~0.5 s, almost all of it network latency, so three seeks run at once (~0.2 s per frame; more
doesn't help). A frame every 10 s of a one-hour movie is then about 75 s of work.

Frames are taken coarse to fine: 16 spread over the whole movie first, then 48, 100, 200, …
The sheet and index are rewritten after each level, so the preview is usable after a few seconds
and gets sharper while you watch. Keyframes don't sit exactly on the slot times, so every frame
carries its real time (from showinfo), and two slots that land on the same keyframe keep one.
An interrupted build resumes from the frames already on disk.

Two kinds of request share one queue: a movie opened in the player goes first and is never held
back; the nightly run's background requests wait while something is streaming (hold()) and can be
called off at the end of the window (stop_background()), to be resumed the next night.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

FMT = 2                   # bump when the layout changes: older strips are made again
W, H = 320, 180
COLS = 10
PER_PAGE = 100            # frames per image (10 x 10 = 3200 x 1800 px)
# The grid's flick-through: about one frame every 5 minutes of the movie (6 to 24 of them), as one
# image of 4 columns. A half-hour movie flicks through 6, an hour 12, two hours 24.
FLICK_EVERY, FLICK_MIN, FLICK_MAX, FLICK_COLS = 300.0, 6, 24, 4


def flick_count(meta):
    n = int(meta.get("n") or 0)
    dur = float(meta.get("total") or 0) * float(meta.get("interval") or 0)
    k = int(round(dur / FLICK_EVERY)) if dur else FLICK_MIN
    return max(1, min(n, max(FLICK_MIN, min(FLICK_MAX, k))))
SLOT_S = 10.0             # one frame every 10 s…
MAX_SLOTS = 1800          # …up to 5 hours; longer movies get a wider spacing (WebP caps at 16383 px)
WORKERS = 3               # parallel seeks: 3 is the sweet spot on a NAS (6 is no faster)
LEVELS = (16, 48, 100, 200, 400, 800)   # rewrite the sheet when this many frames are in
FRAME_TIMEOUT = 60
RETRY_FAILED_AFTER = 7 * 86400


def _ordered_slots(n):
    """Slot indices coarse to fine: an even spread of 16, then 48, 100, … then the rest."""
    seen, order = set(), []
    for k in LEVELS + (n,):
        k = min(k, n)
        for j in range(k):
            i = min(n - 1, int((j + 0.5) * n / k))
            if i not in seen:
                seen.add(i)
                order.append(i)
    return order


class Scrubber:
    def __init__(self, cache_dir, ffmpeg="ffmpeg", ffprobe="ffprobe", log=None):
        self.dir = cache_dir
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.log = log or (lambda *_: None)
        self._q = deque()
        self._lock = threading.Lock()
        self._busy = None             # the path being built
        self._thread = None
        self._fmt = None
        self._bg = set()              # queued by the nightly run (may wait, may be called off)
        self._cancel = set()
        self._index = {}              # meta file -> (mtime, flick count) for index()
        self.hold = lambda: False     # serve.py: True while a video is streaming
        os.makedirs(self.dir, exist_ok=True)

    # ---------------------------------------------------------------- files
    def key(self, path):
        return self._key(path)

    def index(self):
        """{key: flick count} for every strip with frames — which movies the grid can flick
        through. Reads only the meta files that changed since the last call."""
        out, seen = {}, set()
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for f in names:
            if not f.endswith(".json"):
                continue
            p = os.path.join(self.dir, f)
            seen.add(p)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            hit = self._index.get(p)
            if not hit or hit[0] != mt:
                try:
                    with open(p, encoding="utf-8") as fh:
                        m = json.load(fh)
                    k = flick_count(m) if m.get("fmt") == FMT and m.get("n") else 0
                except (OSError, ValueError):
                    k = 0
                hit = self._index[p] = (mt, k)
            if hit[1]:
                out[f[:-5]] = hit[1]
        for p in list(self._index):
            if p not in seen:
                del self._index[p]
        return out

    def _key(self, path):
        return hashlib.sha1(os.path.abspath(path).encode("utf-8", "replace")).hexdigest()[:20]

    def _meta_path(self, path):
        return os.path.join(self.dir, self._key(path) + ".json")

    def _page_file(self, path, page, ext):
        return os.path.join(self.dir, "%s.p%d.%s" % (self._key(path), page, ext))

    def sheet_path(self, path, page=0):
        m = self._read(path)
        if not m or not m.get("n") or m.get("fmt") != FMT:
            return None, None
        p = self._page_file(path, int(page or 0), m["ext"])
        return (p, "image/webp" if m["ext"] == "webp" else "image/jpeg") if os.path.isfile(p) else (None, None)

    def _read(self, path):
        try:
            with open(self._meta_path(path), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self, path, meta):
        mp = self._meta_path(path)
        with open(mp + ".tmp", "w", encoding="utf-8") as f:
            json.dump(meta, f, separators=(",", ":"))
        os.replace(mp + ".tmp", mp)

    @staticmethod
    def _sig(path):
        st = os.stat(path)
        return [st.st_size, int(st.st_mtime)]

    def image_format(self):
        """WebP if this ffmpeg has libwebp (about half the size of JPEG), else JPEG."""
        if self._fmt is None:
            try:
                r = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                   text=True, timeout=30)
                self._fmt = "webp" if "libwebp" in r.stdout else "jpg"
            except (OSError, subprocess.TimeoutExpired):
                self._fmt = "jpg"
        return self._fmt

    # ---------------------------------------------------------------- status / queue
    def status(self, path):
        """{state: none|queued|working|done|failed, n, total, t:[…], w, h, cols, v}"""
        m = self._read(path)
        try:
            sig = self._sig(path)
        except OSError:
            return {"state": "none"}
        with self._lock:
            busy = self._busy == path
            queued = path in self._q
        if m and (m.get("sig") != sig or m.get("fmt") != FMT):
            m = None                                   # the file changed (or an old layout): redo
        if not m:
            return {"state": "working" if busy else "queued" if queued else "none"}
        out = {k: m.get(k) for k in ("n", "total", "t", "w", "h", "cols", "per", "pages", "v", "interval")}
        if m.get("done"):
            out["state"] = "done"
        elif m.get("failed"):
            out["state"] = "failed"
        else:
            out["state"] = "working" if busy else "queued" if queued else "partial"
        return out

    def needed(self, path):
        """Would a request do anything (no strip, an unfinished one, or a failure due a retry)?"""
        st = self.status(path)
        if st["state"] == "failed":
            m = self._read(path) or {}
            return time.time() - m.get("failed_at", 0) >= RETRY_FAILED_AFTER
        return st["state"] in ("none", "partial")

    def request(self, path, front=True, background=False):
        """Ask for a strip (no-op when it's done, building or recently failed). front = the player
        wants it now; background = the nightly run (waits while streaming, can be called off)."""
        with self._lock:
            if not background:                     # the player asking makes it a foreground job
                self._bg.discard(path)
                self._cancel.discard(path)
        st = self.status(path)
        if st["state"] in ("done", "working"):
            return st
        if st["state"] == "failed" and not self.needed(path):
            return st
        with self._lock:
            if background and path not in self._q and self._busy != path:
                self._bg.add(path)
            if path in self._q:
                if front:
                    self._q.remove(path)
                    self._q.appendleft(path)
            elif front:
                self._q.appendleft(path)
            else:
                self._q.append(path)
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, name="scrub", daemon=True)
                self._thread.start()
        st = dict(st)
        st["state"] = "queued" if st["state"] == "none" else st["state"]
        return st

    def queue_len(self):
        with self._lock:
            return len(self._q) + (1 if self._busy else 0)

    def background_busy(self):
        with self._lock:
            return any(p in self._bg for p in self._q) or (self._busy in self._bg)

    def stop_background(self):
        """End of the nightly window: drop the queued background work and stop the one being
        built after its current batch (its frames stay on disk; the next night resumes it)."""
        with self._lock:
            self._q = deque(p for p in self._q if p not in self._bg)
            if self._busy in self._bg:
                self._cancel.add(self._busy)
            self._bg.clear()

    def _loop(self):
        while True:
            with self._lock:
                if not self._q:
                    self._busy = None
                    return
                path = self._q.popleft()
                self._busy = path
            try:
                self.build(path)
            except Exception as e:                       # never let one file stop the queue
                self.log("scrub: %s: %s" % (os.path.basename(path), e))
            finally:
                with self._lock:
                    self._busy = None
                    self._bg.discard(path)
                    self._cancel.discard(path)

    # ---------------------------------------------------------------- building
    def _duration(self, path):
        r = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=120)
        try:
            d = float(r.stdout.strip())
        except ValueError:
            return 0.0
        return d if d > 0 else 0.0

    def _grab(self, path, t, out):
        """One keyframe at/just before t -> 160x90 JPEG. Returns its real time, or None."""
        vf = ("showinfo,scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1" % (W, H, W, H))
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-skip_frame", "nokey",
               "-ss", "%.3f" % t, "-copyts", "-i", path, "-map", "0:V:0", "-frames:v", "1",
               "-vf", vf, "-q:v", "3", out]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0 or not os.path.isfile(out):
            return None
        m = re.search(rb"pts_time:\s*(-?[\d.]+)", r.stderr)
        return round(float(m.group(1)), 2) if m else round(t, 2)

    def _compose(self, path, frames, work, ext):
        """frames: [(time, file)] -> pages of grid images, frames in time order, row by row.
        Returns (times, number of pages)."""
        frames = sorted(frames)
        enc = (["-c:v", "libwebp", "-quality", "62", "-compression_level", "4"] if ext == "webp"
               else ["-c:v", "mjpeg", "-q:v", "4"])
        pages = (len(frames) + PER_PAGE - 1) // PER_PAGE
        for pg in range(pages):
            chunk = frames[pg * PER_PAGE:(pg + 1) * PER_PAGE]
            rows = (len(chunk) + COLS - 1) // COLS
            lst = os.path.join(work, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for _t, fp in chunk + [chunk[-1]] * (rows * COLS - len(chunk)):   # pad the last row
                    f.write("file '%s'\n" % fp.replace("'", "'\\''"))
            out = self._page_file(path, pg, ext)
            tmp = out + ".tmp." + ext
            r = subprocess.run([self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                                "-i", lst, "-vf", "tile=%dx%d" % (COLS, rows), "-frames:v", "1"] + enc + [tmp],
                               capture_output=True, timeout=300)
            if r.returncode != 0 or not os.path.isfile(tmp):
                raise RuntimeError("compose failed: %s" % r.stderr.decode("utf-8", "replace")[-300:])
            os.replace(tmp, out)
        return [t for t, _ in frames], pages

    def _drop_files(self, path, keep_pages=0):
        """Remove page images beyond keep_pages (and the old single-sheet layout)."""
        key = self._key(path)
        for f in os.listdir(self.dir):
            m = re.fullmatch(re.escape(key) + r"(?:\.p(\d+))?\.(webp|jpg)", f)
            if m and (m.group(1) is None or int(m.group(1)) >= keep_pages):
                try:
                    os.remove(os.path.join(self.dir, f))
                except OSError:
                    pass

    def build(self, path):
        sig = self._sig(path)
        dur = self._duration(path)
        ext = self.image_format()
        key = self._key(path)
        if not dur:
            self._write(path, {"sig": sig, "failed": True, "failed_at": time.time(), "n": 0,
                               "why": "no duration"})
            return
        n = max(1, min(MAX_SLOTS, int(round(dur / SLOT_S))))
        interval = dur / n
        work = os.path.join(self.dir, key + ".work")
        old = self._read(path)
        if not old or old.get("sig") != sig or old.get("fmt") != FMT:
            shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        got = {}                                          # slot -> (time, file), resumed from disk
        for f in os.listdir(work):
            m = re.fullmatch(r"s(\d+)_(-?[\d.]+)\.jpg", f)
            if m and int(m.group(1)) < n:
                got[int(m.group(1))] = (float(m.group(2)), os.path.join(work, f))
        order = [i for i in _ordered_slots(n) if i not in got]
        meta = {"fmt": FMT, "sig": sig, "w": W, "h": H, "cols": COLS, "per": PER_PAGE, "pages": 0,
                "ext": ext, "interval": round(interval, 3), "total": n, "n": 0, "t": [],
                "v": int(time.time())}
        fails = 0
        t0 = time.time()

        def publish(final=False):
            uniq, seen = [], set()
            for tt, fp in sorted(got.values()):
                if tt not in seen:                          # two slots, one keyframe: keep one
                    seen.add(tt)
                    uniq.append((tt, fp))
            if not uniq:
                return
            meta["t"], meta["pages"] = self._compose(path, uniq, work, ext)
            self._drop_files(path, meta["pages"])
            meta["n"] = len(uniq)
            meta["v"] = int(time.time() * 1000)
            meta["done"] = bool(final)
            self._write(path, meta)

        def grab(i):
            tmp = os.path.join(work, "g%d.jpg" % i)
            t = self._grab(path, (i + 0.5) * interval, tmp)
            if t is None:
                return i, None
            fp = os.path.join(work, "s%d_%s.jpg" % (i, t))
            os.replace(tmp, fp)
            return i, (t, fp)

        checkpoints = sorted({min(k, n) for k in LEVELS})
        with ThreadPoolExecutor(WORKERS) as ex:
            pos = 0
            while pos < len(order):
                while path in self._bg and self.hold() and path not in self._cancel:
                    time.sleep(3)                         # nightly work waits while you watch
                if path in self._cancel:
                    if got:
                        publish()                         # keep what's there; resumes next time
                    self.log("scrub: %s: paused at %d frames" % (os.path.basename(path), len(got)))
                    return
                # the next batch runs up to the next checkpoint (so a level is published as a whole)
                have = len(got)
                nxt = next((c for c in checkpoints if c > have), n)
                batch = order[pos:pos + max(1, nxt - have)]
                pos += len(batch)
                for i, res in ex.map(grab, batch):
                    if res:
                        got[i] = res
                    else:
                        fails += 1
                if fails > 10 and fails > len(got):
                    meta.update(failed=True, failed_at=time.time(), why="frames failed")
                    if got:
                        publish()
                        meta["failed"] = True
                    self._write(path, meta)
                    shutil.rmtree(work, ignore_errors=True)
                    return
                if pos < len(order):
                    publish()
        publish(final=True)
        shutil.rmtree(work, ignore_errors=True)
        self.log("scrub: %s: %d frames in %.0f s" % (os.path.basename(path), meta["n"], time.time() - t0))

    # ---------------------------------------------------------------- grid flick-through
    def flick_path(self, path):
        """A small 4x3 image of 12 frames spread over the movie, for hovering a poster in the grid.
        Cut from the strip's pages the first time it is asked for (and again after the strip
        got sharper); None when the movie has no strip — asking never starts a build.
        -> (file, content type) or (None, None)"""
        st = self.status(path)
        m = self._read(path)
        if st["state"] not in ("done", "partial", "working", "queued", "failed") or not m or not m.get("n"):
            return None, None
        ext = m["ext"]
        key = self._key(path)
        k = flick_count(m)
        out = os.path.join(self.dir, "%s.flick%s_%d.%s" % (key, m["v"], k, ext))
        ctype = "image/webp" if ext == "webp" else "image/jpeg"
        if os.path.isfile(out):
            return out, ctype
        n, per, cols, w, h = m["n"], m["per"], m["cols"], m["w"], m["h"]
        idx = [min(n - 1, int((j + 0.5) * n / k)) for j in range(k)]
        pages = sorted({i // per for i in idx})
        args = [self.ffmpeg, "-hide_banner", "-nostdin", "-y"]
        for pg in pages:
            args += ["-i", self._page_file(path, pg, ext)]
        uses = {pg: [i for i in idx if i // per == pg] for pg in pages}
        graph = []
        for n_in, pg in enumerate(pages):
            outs = "".join("[s%d_%d]" % (pg, j) for j in range(len(uses[pg])))
            graph.append("[%d:v]split=%d%s" % (n_in, len(uses[pg]), outs))
        seen = {pg: 0 for pg in pages}
        cells = []
        for c, i in enumerate(idx):
            pg, j = i // per, i % per
            graph.append("[s%d_%d]crop=%d:%d:%d:%d,setsar=1[c%d]" % (pg, seen[pg], w, h, (j % cols) * w,
                                                                   (j // cols) * h, c))
            seen[pg] += 1
            cells.append("[c%d]" % c)
        rows = (k + FLICK_COLS - 1) // FLICK_COLS
        graph.append("%sconcat=n=%d:v=1:a=0,tile=%dx%d" % ("".join(cells), k, FLICK_COLS, rows))
        enc = (["-c:v", "libwebp", "-quality", "60"] if ext == "webp" else ["-c:v", "mjpeg", "-q:v", "4"])
        tmp = out + ".tmp." + ext
        try:
            r = subprocess.run(args + ["-filter_complex", ";".join(graph), "-frames:v", "1"] + enc + [tmp],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        if r.returncode != 0 or not os.path.isfile(tmp):
            self.log("scrub: flick failed: %s" % r.stderr.decode("utf-8", "replace")[-200:])
            return None, None
        os.replace(tmp, out)
        for f in os.listdir(self.dir):                    # older flicks of this movie
            if f.startswith(key + ".flick") and os.path.join(self.dir, f) != out:
                try:
                    os.remove(os.path.join(self.dir, f))
                except OSError:
                    pass
        return out, ctype

    # ---------------------------------------------------------------- housekeeping
    def size(self):
        n = b = 0
        for f in os.listdir(self.dir):
            if f.endswith(".json"):
                n += 1
            try:
                b += os.path.getsize(os.path.join(self.dir, f))
            except OSError:
                pass
        return n, b

    def clear(self):
        with self._lock:
            self._q.clear()
            self._bg.clear()
        for f in os.listdir(self.dir):
            p = os.path.join(self.dir, f)
            if f.endswith(".work") and os.path.isdir(p):
                if self._busy and f.startswith(self._key(self._busy)):
                    continue
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":                    # python3 scrub.py VIDEO [CACHE_DIR]: build one strip
    import sys
    s = Scrubber(sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "scrub"),
                 log=print)
    t = time.time()
    s.build(sys.argv[1])
    st = s.status(sys.argv[1])
    print(st["state"], st.get("n"), "frames", "%.0f s" % (time.time() - t), s.sheet_path(sys.argv[1]))
