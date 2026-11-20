"""nightly.py — Vault's nightly run over the library (Settings → Nightly).

Inside a time window the user sets (e.g. 01:00–06:00), once per night, in this order:

  1. Import      — the download folder into the library, with the choices the Import window
                   asks for made up front: replace a library copy with a bigger download or
                   leave it for review; delete duplicates or leave them in the download folder.
  2. Metadata    — ONLY movies added since the nightly run was switched on. A movie the sources
                   had nothing for is asked again after a week at the earliest, never sooner, so
                   the source sites don't see the same misses every night.
  3. Scene markers — vault-scene-tagger (a separate command-line tool) over new movies, or
                   working through the whole library; it is stopped at the end of the window
                   and the next night carries on where it stopped.
  4. Subtitles   — the Whisper queue, the same as 💬 Transcribe; cancelled at the end of the
                   window (the movie it was on is redone the next night).

Nothing here knows Vault's internals: serve.py hands in a `host` with the few calls needed.
Standard library only.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

DEFAULTS = {
    "enabled": False,
    "start": "01:00", "end": "06:00",
    "import": False,
    "replace": False,          # bigger download -> replace the library copy (else leave it)
    "dupes": "delete",         # "delete" | "keep" (download copy no better than the library's)
    "metadata": True,
    "scenes": False, "scenes_scope": "new",      # "new" | "library"
    "subs": False, "subs_scope": "new",          # "new" | "library"
    "tagger": "",              # vault-scene-tagger command; empty = find it
}
META_RETRY = 7 * 86400         # a miss is asked again after a week, not sooner
META_CAP = 200                 # most movies asked of the sources in one night
SCOPES = ("new", "library")


def _hm(s):
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, AttributeError):
        return None


def settings(cfg):
    """The nightly settings from config.json, every field present and valid."""
    raw = (cfg or {}).get("nightly") or {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[k], bool):
            out[k] = bool(v)
        elif k in ("start", "end"):
            if _hm(v) is not None:
                out[k] = "%02d:%02d" % divmod(_hm(v), 60)
        elif k == "dupes":
            out[k] = v if v in ("delete", "keep") else "delete"
        elif k in ("scenes_scope", "subs_scope"):
            out[k] = v if v in SCOPES else "new"
        elif k == "tagger":
            out[k] = str(v or "").strip()
    return out


def clean(body, current):
    """Merge a Settings save into the current settings. -> (settings, error)."""
    s = dict(current)
    for k in DEFAULTS:
        if k in body:
            s[k] = body[k]
    for k in ("start", "end"):
        if _hm(s[k]) is None:
            return None, "Times are HH:MM, e.g. 01:00"
    if _hm(s["start"]) == _hm(s["end"]):
        return None, "The window needs an end different from its start"
    return settings({"nightly": s}), None


def window(now, start, end):
    """(window start, window end) as datetimes for the window `now` is in, else None.
    A window may cross midnight (22:00–05:00)."""
    s, e = _hm(start), _hm(end)
    if s is None or e is None or s == e:
        return None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in (day - timedelta(days=1), day):
        ws = d + timedelta(minutes=s)
        we = d + timedelta(minutes=e) + (timedelta(days=1) if e < s else timedelta())
        if ws <= now < we:
            return ws, we
    return None


def window_length(start, end):
    s, e = _hm(start), _hm(end)
    return ((e - s) % 1440) * 60


def next_start(now, start, end):
    """When the next window opens (a datetime)."""
    s = _hm(start)
    ws = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=s)
    return ws if ws > now else ws + timedelta(days=1)


class Runner:
    def __init__(self, host, state_path):
        self.h = host
        self.path = state_path
        self.lock = threading.Lock()
        self.st = self._load()
        self.status = {"running": False, "step": "", "line": "", "manual": False, "until": 0}
        self._stop = False
        self._proc = None

    # ------------------------------------------------------------ state file
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("meta", {})
                return d
        except (OSError, ValueError):
            pass
        return {"baseline": 0, "meta": {}, "done_key": "", "last": None}

    def _save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.st, f)
                os.replace(tmp, self.path)
            except OSError:
                pass

    def ensure_baseline(self):
        """"New" means added after the nightly run was first switched on."""
        if not self.st.get("baseline"):
            self.st["baseline"] = int(time.time())
            self._save()

    def note_meta(self, codes):
        """A metadata scan asked the sources for these (also the scan after a manual import)."""
        now = int(time.time())
        for c in codes:
            self.st["meta"][c] = now
        self._save()

    # ------------------------------------------------------------ status for Settings
    def snapshot(self):
        cfg = settings(self.h.config())
        now = datetime.now()
        d = dict(self.status)
        d["last"] = self.st.get("last")
        d["baseline"] = self.st.get("baseline") or 0
        d["waiting"] = sum(1 for c, t in self.st["meta"].items() if time.time() - t < META_RETRY)
        d["next"] = (next_start(now, cfg["start"], cfg["end"]).strftime("%a %H:%M")
                     if cfg["enabled"] else "")
        d["tagger"] = " ".join(shlex.quote(a) for a in (self.tagger_cmd(cfg) or []))
        return d

    # ------------------------------------------------------------ scheduling
    def start_scheduler(self):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:                       # never let the clock thread die
                    sys.stderr.write("[nightly] %s\n" % e)
                time.sleep(30)
        threading.Thread(target=loop, daemon=True, name="nightly").start()

    def tick(self, now=None):
        cfg = settings(self.h.config())
        if not cfg["enabled"] or self.status["running"]:
            return False
        w = window(now or datetime.now(), cfg["start"], cfg["end"])
        if not w:
            return False
        key = w[0].strftime("%Y-%m-%d %H:%M")
        if self.st.get("done_key") == key:
            return False                                     # tonight's run already happened
        self.st["done_key"] = key
        self._save()
        self._start(cfg, w[1].timestamp(), manual=False)
        return True

    def run_now(self):
        """Settings' Run now: the same steps at once, for at most the window's length."""
        if self.status["running"]:
            return False
        cfg = settings(self.h.config())
        self._start(cfg, time.time() + window_length(cfg["start"], cfg["end"]), manual=True)
        return True

    def stop(self):
        self._stop = True
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
        if self.status.get("step") == "Subtitles":
            self.h.subs_cancel()
        return self.status["running"]

    def _start(self, cfg, deadline, manual):
        self.ensure_baseline()
        self._stop = False
        self.status.update(running=True, step="", line="Starting…", manual=manual, until=int(deadline))
        self.st["last"] = {"started": int(time.time()), "ended": 0, "manual": manual, "log": []}
        threading.Thread(target=self._run, args=(cfg, deadline), daemon=True, name="nightly-run").start()

    # ------------------------------------------------------------ the run
    def log(self, msg):
        sys.stderr.write("[nightly] %s\n" % msg)
        self.status["line"] = msg
        last = self.st.get("last")
        if last is not None:
            last["log"].append([int(time.time()), msg])
            del last["log"][:-60]
            self._save()

    def _left(self, deadline):
        return 0 if self._stop else deadline - time.time()

    def _run(self, cfg, deadline):
        awake = None
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            try:                                     # keep the Mac from sleeping mid-run
                awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                awake = None
        try:
            steps = [("Import", cfg["import"], self._import),
                     ("Metadata", cfg["metadata"], self._metadata),
                     ("Scene markers", cfg["scenes"], self._scenes),
                     ("Subtitles", cfg["subs"], self._subs)]
            todo = [s for s in steps if s[1]]
            if not todo:
                self.log("Nothing switched on")
            for i, (name, _on, fn) in enumerate(todo):
                if self._left(deadline) <= 60:
                    self.log("%s: skipped — %s" % (name, "stopped" if self._stop else "the window is over"))
                    continue
                self.status["step"] = name
                # scene markers working through the library would eat the whole night: leave
                # half of what is left for the subtitles when they come after
                until = deadline
                if name == "Scene markers" and cfg["scenes_scope"] == "library" and cfg["subs"]:
                    until = time.time() + self._left(deadline) / 2
                try:
                    fn(cfg, until)
                except Exception as e:
                    self.log("%s: failed — %s" % (name, e))
        finally:
            if awake:
                awake.terminate()
                try:
                    awake.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            self.st["last"]["ended"] = int(time.time())
            self.status.update(running=False, step="", line="Stopped" if self._stop else "Done")
            self.log("Stopped" if self._stop else "Done")

    def _new_items(self):
        base = self.st.get("baseline") or 0
        return [it for it in self.h.items()
                if not it.get("missing") and (it.get("added") or 0) >= base]

    # 1 ---------------------------------------------------------------- import
    def _import(self, cfg, deadline):
        self.log("Import: from the download folder…")
        msg = self.h.run_import(cfg["replace"], cfg["dupes"] == "keep")
        self.log("Import: " + msg)

    # 2 ---------------------------------------------------------------- metadata
    def _metadata(self, cfg, deadline):
        now = time.time()
        meta = self.st["meta"]
        new = self._new_items()
        for it in new:                              # found since: forget the miss
            if it.get("title_en"):
                meta.pop(it["code"], None)
        need = [it["code"] for it in new if not it.get("title_en")]
        due = [c for c in need if now - meta.get(c, 0) >= META_RETRY]
        waiting = len(need) - len(due)
        due = due[:META_CAP]
        if not due:
            self.log("Metadata: nothing new to look up" +
                     (" (%d wait for their weekly retry)" % waiting if waiting else ""))
            self._save()
            return
        ok, why = self.h.start_metadata(due)
        if not ok:
            self.log("Metadata: not started — " + why)
            return
        self.note_meta(due)
        self.log("Metadata: looking up %d new movie%s…" % (len(due), "" if len(due) == 1 else "s"))
        while self.h.metadata_running():
            time.sleep(5)
        have = {it["code"] for it in self.h.items() if it.get("title_en")}
        found = [c for c in due if c in have]
        for c in found:
            meta.pop(c, None)
        self._save()
        self.log("Metadata: found %d of %d%s" % (
            len(found), len(due),
            "; the rest are asked again in a week" if len(found) < len(due) else ""))

    # 3 ---------------------------------------------------------------- scene markers
    def tagger_cmd(self, cfg):
        """vault-scene-tagger as an argv prefix, or None."""
        if cfg.get("tagger"):
            try:
                return shlex.split(cfg["tagger"])
            except ValueError:
                return None
        bundled = getattr(self.h, "bundled_tagger", None)          # the one shipped with Vault
        cmd = bundled() if bundled else None
        if cmd:
            return cmd
        exe = shutil.which("vault-scene-tagger")
        if exe:
            return [exe]
        py = self.h.whisper_python()                  # a pip install into Vault's Whisper env
        if py and os.path.isfile(py):
            try:
                r = subprocess.run([py, "-c", "import vault_scene_tagger"], capture_output=True,
                                   timeout=60)
                if r.returncode == 0:
                    return [py, "-m", "vault_scene_tagger"]
            except (OSError, subprocess.TimeoutExpired):
                pass
        return None

    def _wait(self, p, deadline):
        """Wait for a child process; terminate it at the deadline or on Stop. -> stopped?"""
        self._proc = p
        try:
            while p.poll() is None:
                if self._left(deadline) <= 0:
                    p.terminate()
                    try:
                        p.wait(60)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    return True
                time.sleep(2)
            return False
        finally:
            self._proc = None

    def _scenes(self, cfg, deadline):
        cmd = self.tagger_cmd(cfg)
        if not cmd:
            self.log("Scene markers: skipped — the scene tagger isn't set up "
                     "(Settings → Scene model → Set up)")
            return
        logf = self.h.log_path("nightly-tagger.log")
        args = ["--phase", "c", "--engine", "phar"]
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
        if cfg["scenes_scope"] == "library":
            targets = [self.h.library()]
        else:
            targets = []
            for it in self._new_items():
                for pp in self.h.item_parts(it):
                    vp = pp.get("path")
                    if vp and not os.path.exists(os.path.splitext(vp)[0] + ".scenes.json"):
                        targets.append(vp)
            if not targets:
                self.log("Scene markers: every new movie has them")
                return
        done = 0
        for tgt in targets:
            fd, summ = tempfile.mkstemp(suffix=".json")
            os.close(fd)
            label = "the library" if tgt == self.h.library() else os.path.basename(tgt)
            self.log("Scene markers: %s…" % label)
            with open(logf, "a", encoding="utf-8") as lf:
                p = subprocess.Popen(cmd + [tgt] + args + ["--summary-json", summ],
                                     stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=env, cwd=os.path.dirname(logf))
                stopped = self._wait(p, deadline)
            try:
                with open(summ, encoding="utf-8") as f:
                    done += int(json.load(f).get("processed") or 0)
            except (OSError, ValueError):
                if not stopped and p.returncode == 0:
                    done += 1
            finally:
                try:
                    os.remove(summ)
                except OSError:
                    pass
            if stopped:
                self.log("Scene markers: %d tagged, stopped at the end of the window — "
                         "the next night carries on" % done if not self._stop
                         else "Scene markers: %d tagged, stopped" % done)
                return
            if p.returncode not in (0, None):
                self.log("Scene markers: the tagger exited with %s — see %s" % (p.returncode, logf))
                return
        self.log("Scene markers: %d video%s tagged" % (done, "" if done == 1 else "s"))

    # 4 ---------------------------------------------------------------- subtitles
    def _subs(self, cfg, deadline):
        err = self.h.subs_ready()
        if err:
            self.log("Subtitles: skipped — " + err)
            return
        if self.h.subs_busy():
            self.log("Subtitles: skipped — a transcription is already running")
            return
        if cfg["subs_scope"] == "library":
            items = sorted((it for it in self.h.items() if not it.get("missing")),
                           key=lambda it: -(it.get("added") or 0))
        else:
            items = self._new_items()
        jobs = []
        for it in items:
            parts = self.h.item_parts(it)
            for i, pp in enumerate(parts):
                vp = pp.get("path")
                if vp and not os.path.isfile(os.path.splitext(vp)[0] + ".en.srt"):
                    jobs.append({"code": it.get("uid") or it["code"], "part": i, "path": vp,
                                 "label": it["code"] + (" · part %d" % (i + 1) if len(parts) > 1 else "")})
            if len(jobs) >= 300:
                break
        if not jobs:
            self.log("Subtitles: every %s has them" % ("movie" if cfg["subs_scope"] == "library" else "new movie"))
            return
        n = self.h.subs_enqueue(jobs)
        self.log("Subtitles: %d video%s queued…" % (n, "" if n == 1 else "s"))
        while self.h.subs_busy():
            if self._left(deadline) <= 0:
                self.h.subs_cancel()
                break
            time.sleep(5)
        done = self.h.subs_finished()
        self.log("Subtitles: %d done%s" % (done, ", stopped at the end of the window" if self._left(deadline) <= 0 and not self._stop
                                            else ", stopped" if self._stop else ""))
