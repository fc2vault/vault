"""Subtitles for Vault: a queue of Whisper transcriptions (Japanese speech -> English .en.srt).

serve.py owns the HTTP side; this module owns the job. One movie part is transcribed at a time,
by tools/transcribe.py run with the Python that has a Whisper engine (Settings -> Subtitles):
Vault itself stays on the plain system Python, the heavy ML stack lives in its own environment.

A subtitle file sits next to its video as `<video name>.en.srt` (the name Plex, VLC, Infuse and
mpv pick up on their own). The player gets it as WebVTT from /subs/<code>?part=N.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time

SUFFIX = ".en.srt"
JA_SUFFIX = ".ja.srt"          # the Japanese transcript, kept in two-step (LLM) mode
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "tools", "transcribe.py")
MODELS = ("large-v3", "medium", "small")      # large-v3-turbo can't translate
TRANSLATORS = ("llm", "whisper")
# small instruction-tuned models that run on an 8 GB Apple Silicon Mac after Whisper is unloaded
LLM_MODELS = ("mlx-community/Qwen3-4B-Instruct-2507-4bit", "mlx-community/Qwen2.5-7B-Instruct-4bit")
DEFAULT_LLM = LLM_MODELS[0]
ENGINES = ("auto", "mlx", "faster", "openai")

PROGRESS = {"running": False, "current": None, "progress": 0.0, "done": 0, "total": 0,
            "queue": [], "errors": [], "line": "", "engine": "", "finished": []}
_LOCK = threading.Lock()
_PROC = {"p": None, "cancel": False}
_SETUP = {"running": False, "line": "", "ok": None}


def subs_path(video):
    return os.path.splitext(video)[0] + SUFFIX


def has_subs(video):
    return bool(video) and os.path.isfile(subs_path(video))


def env_dir(data_root):
    """The environment Vault sets up itself (Settings -> Subtitles -> Set up)."""
    return os.path.join(data_root, "whisper-env")


def env_python(root):
    return os.path.join(root, "Scripts", "python.exe") if os.name == "nt" else os.path.join(root, "bin", "python")


def resolve_python(cfg, data_root):
    """The configured Whisper Python, else the one Vault set up, else None."""
    p = (cfg.get("whisper_python") or "").strip()
    if p and os.path.isfile(os.path.expanduser(p)):
        return os.path.expanduser(p)
    p = env_python(env_dir(data_root))
    return p if os.path.isfile(p) else None


_CHECK = {"py": None, "at": 0.0, "res": None}


def check(py, fresh=False):
    """What the Whisper Python can run: {"python", "engines": [...], "ffmpeg"} or {"error"}."""
    if not py:
        return {"error": "No Whisper environment yet — set one up below."}
    if not fresh and _CHECK["py"] == py and time.time() - _CHECK["at"] < 600:
        return _CHECK["res"]
    try:
        r = subprocess.run([py, WORKER, "--check"], capture_output=True, text=True, timeout=120)
        res = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-300:]}
    except Exception as e:
        res = {"error": str(e)}
    if not res.get("error") and not res.get("engines"):
        res["error"] = "No Whisper engine is installed in %s" % py
    _CHECK.update(py=py, at=time.time(), res=res)
    return res


# ---------------------------------------------------------------- queue
def snapshot():
    with _LOCK:
        d = dict(PROGRESS)
        d["queue"] = [{"code": j["code"], "part": j["part"], "label": j["label"]} for j in PROGRESS["queue"]]
        d["errors"] = list(PROGRESS["errors"])[-20:]
        d["finished"] = list(PROGRESS["finished"])[-50:]
        cur = PROGRESS["current"]
        d["current"] = {"code": cur["code"], "part": cur["part"], "label": cur["label"]} if cur else None
    return d


def queued_keys():
    with _LOCK:
        ks = {(j["code"], j["part"]) for j in PROGRESS["queue"]}
        if PROGRESS["current"]:
            ks.add((PROGRESS["current"]["code"], PROGRESS["current"]["part"]))
    return ks


def options(cfg, chk=None):
    """The job settings from config.json. The two-step (LLM) translator is the default where an
    LLM runtime is installed (or an OpenAI-compatible server is configured)."""
    url = (cfg.get("llm_url") or "").strip()
    has_llm = bool(url) or bool((chk or {}).get("llm"))
    tr = cfg.get("whisper_translator") or ("llm" if has_llm else "whisper")
    if tr == "llm" and not has_llm:
        tr = "whisper"
    return {"engine": cfg.get("whisper_engine") or "auto", "model": cfg.get("whisper_model") or "large-v3",
            "translator": tr, "llm": cfg.get("llm_model") or DEFAULT_LLM, "llm_url": url}


def enqueue(jobs, py, opts, on_done):
    """jobs: [{code, part, path, label}]. Skips parts already queued. Starts the worker thread
    if it isn't running. -> number added."""
    have = queued_keys()
    add = [j for j in jobs if (j["code"], j["part"]) not in have]
    with _LOCK:
        PROGRESS["queue"].extend(add)
        PROGRESS["total"] += len(add)
        start = not PROGRESS["running"] and bool(PROGRESS["queue"])
        if start:
            PROGRESS.update(running=True, errors=[], finished=[], line="", done=0,
                            total=len(PROGRESS["queue"]))
            _PROC["cancel"] = False
    if start:
        threading.Thread(target=_run, args=(py, opts, on_done), daemon=True).start()
    return len(add)


def cancel():
    with _LOCK:
        n = len(PROGRESS["queue"])
        PROGRESS["queue"] = []
        _PROC["cancel"] = True
        p = _PROC["p"]
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
    return n


def _run(py, opts, on_done):
    while True:
        with _LOCK:
            if not PROGRESS["queue"] or _PROC["cancel"]:
                PROGRESS.update(running=False, current=None, progress=0.0)
                PROGRESS["line"] = "Cancelled" if _PROC["cancel"] else "Done"
                return
            job = PROGRESS["queue"].pop(0)
            PROGRESS.update(current=job, progress=0.0, line="Starting…")
        err = _one(py, opts, job)
        with _LOCK:
            PROGRESS["done"] += 1
            if err:
                if not _PROC["cancel"]:
                    PROGRESS["errors"].append({"code": job["code"], "part": job["part"], "error": err})
            else:
                PROGRESS["finished"].append({"code": job["code"], "part": job["part"]})
        if not err:
            try:
                on_done(job)
            except Exception:
                pass


def _one(py, opts, job):
    out = subs_path(job["path"])
    cmd = [py, "-u", WORKER, job["path"], out, "--engine", opts["engine"], "--model", opts["model"],
           "--translator", opts["translator"]]
    if opts["translator"] == "llm":
        cmd += ["--llm", opts["llm"], "--ja-out", os.path.splitext(job["path"])[0] + JA_SUFFIX]
        if opts.get("llm_url"):
            cmd += ["--llm-url", opts["llm_url"]]
    env = dict(os.environ)
    # a Finder-launched Vault has a bare PATH: make sure the worker finds Homebrew's ffmpeg
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except Exception as e:
        return "could not start the worker: %s" % e
    _PROC["p"] = p
    err = None
    tail = []

    def _drain():                                   # keep stderr from filling its pipe
        for ln in p.stderr:
            tail.append(ln.rstrip())
            del tail[:-15]
    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    for ln in p.stdout:
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        with _LOCK:
            if "progress" in m:
                PROGRESS["progress"] = float(m["progress"])
                el = m.get("elapsed")
                ph = {"listening": "Listening (Japanese) · ", "translating": "Translating · "}.get(m.get("phase") or "", "")
                PROGRESS["line"] = (ph + "%d%%" % round(100 * m["progress"])
                                    + (" · %s elapsed" % _fmt(el) if el else ""))
            if m.get("engine"):
                PROGRESS["engine"] = "%s · %s" % (m["engine"], m.get("model", "")) + (
                    " → %s" % m["translator"] if m.get("translator") and m["translator"] != "whisper" else "")
                PROGRESS["line"] = "Loading %s…" % PROGRESS["engine"]
            if m.get("error"):
                err = m["error"]
    p.wait()
    t.join(timeout=2)
    _PROC["p"] = None
    if _PROC["cancel"]:
        return "cancelled"
    if p.returncode != 0 and not err:
        err = (tail[-1] if tail else "worker exited with %s" % p.returncode)[:300]
    if not err and not os.path.isfile(out):
        err = "no subtitle file was written"
    return err


def _fmt(s):
    s = int(s or 0)
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


# ---------------------------------------------------------------- SRT -> WebVTT
_TS = re.compile(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})")


def srt_to_vtt(text):
    text = text.replace("\r\n", "\n").lstrip("﻿")
    return "WEBVTT\n\n" + _TS.sub(r"\1.\2", text)


# ---------------------------------------------------------------- environment setup
def _find_base_python():
    """A Python >= 3.10 to build the environment from (Whisper engines need a recent one)."""
    cands = [sys.executable, shutil.which("python3.12"), shutil.which("python3.11"),
             shutil.which("python3.13"), shutil.which("python3"),
             os.path.expanduser("~/miniforge3/bin/python"), "/opt/homebrew/bin/python3",
             shutil.which("py")]
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import sys,venv;print(sys.version_info[:2]>=(3,10))"],
                               capture_output=True, text=True, timeout=20)
            if r.stdout.strip() == "True":
                return c
        except Exception:
            pass
    return None


def default_engine_package():
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    return "mlx-whisper faster-whisper mlx-lm" if apple else "faster-whisper"


def setup_status():
    return dict(_SETUP)


def setup(data_root, on_done):
    """Create <data_root>/whisper-env and install the engine that runs fastest here."""
    if _SETUP["running"]:
        return False
    _SETUP.update(running=True, line="Looking for Python 3.10+…", ok=None)

    def run():
        root = env_dir(data_root)
        try:
            base = _find_base_python()
            if not base:
                raise RuntimeError("needs Python 3.10 or newer — install one (python.org) and try again")
            pkg = default_engine_package()
            if not os.path.isfile(env_python(root)):
                _SETUP["line"] = "Creating the environment…"
                subprocess.run([base, "-m", "venv", root], check=True, capture_output=True, timeout=600)
            py = env_python(root)
            _SETUP["line"] = "Installing %s (a few hundred MB)…" % pkg
            r = subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"] + pkg.split(),
                               capture_output=True, text=True, timeout=3600)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout)[-300:])
            _SETUP.update(line="Ready — %s installed. The model downloads on the first transcription." % pkg, ok=True)
            on_done(py)
        except Exception as e:
            _SETUP.update(line="Setup failed: %s" % e, ok=False)
        finally:
            _SETUP["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True
