"""Placing scene markers from inside Vault: Settings → Scene model → Set up / Place markers.

The tagger (vault_scene_tagger: video frames → the TimeSformer scene model → *.scenes.json next to
the video) ships inside Vault. Only its runtime is installed on demand, into the same private
environment the subtitles use (<data>/whisper-env): PyTorch, transformers and numpy. On Apple
Silicon PyTorch runs on the GPU (MPS); on Windows / Linux with an NVIDIA card Set up installs the
CUDA build; anywhere else it runs on the processor, which works but takes hours per movie.

The training side of the tagger (exporting reviewed markers) is not part of Vault."""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import subtitles  # noqa: E402  (the shared Whisper / tagger environment)
_SETUP = {"running": False, "line": "", "ok": None}
JOB = {"running": False, "done": 0, "total": 0, "current": "", "line": "", "errors": [],
       "finished": 0.0, "stopping": False}
_PROC = {"p": None}
_LOCK = threading.Lock()
_CHECK = {"py": None, "at": 0.0, "res": None}


def tagger_script():
    """The tagger's launcher: shipped next to serve.py, or the developer's checkout beside vault/."""
    for p in (os.path.join(HERE, "scene_tagger.py"),
              os.path.join(os.path.dirname(os.path.dirname(HERE)), "vault-tagger", "vault-scene-tagger")):
        if os.path.isfile(p):
            return p
    return None


def command(py):
    """argv prefix that runs the bundled tagger with the environment's Python, or None."""
    script = tagger_script()
    return [py, script] if py and script and os.path.isfile(py) else None


_PROBE = (
    "import json, torch, transformers, numpy\n"
    "dev = 'mps' if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available() "
    "else ('cuda' if torch.cuda.is_available() else 'cpu')\n"
    "print(json.dumps({'torch': torch.__version__, 'device': dev}))\n")


def check(py, fresh=False):
    """{"ready": bool, "device": "mps"|"cuda"|"cpu", "torch": ver} or {"ready": False, "error"}."""
    if not py or not os.path.isfile(py):
        return {"ready": False, "error": "not set up"}
    now = time.time()
    if not fresh and _CHECK["py"] == py and now - _CHECK["at"] < 300 and _CHECK["res"]:
        return _CHECK["res"]
    try:
        r = subprocess.run([py, "-c", _PROBE], capture_output=True, text=True, timeout=120)
        res = dict(json.loads(r.stdout.strip().splitlines()[-1]), ready=True) if r.returncode == 0 \
            else {"ready": False, "error": "not set up (PyTorch missing)"}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
        res = {"ready": False, "error": "could not check: %s" % e}
    _CHECK.update(py=py, at=now, res=res)
    return res


def pip_steps():
    """pip install argument lists for this computer: PyTorch from the right index, then the rest."""
    system, machine = platform.system(), platform.machine().lower()
    nvidia = system in ("Windows", "Linux") and bool(shutil.which("nvidia-smi"))
    if nvidia:
        torch = ["torch", "--index-url", "https://download.pytorch.org/whl/cu126"]
    elif system == "Linux":
        torch = ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"]   # the default pulls CUDA (GBs)
    else:
        torch = ["torch"]
    rest = ["transformers", "numpy"]
    if system == "Darwin" and machine in ("x86_64", "amd64"):
        rest = ["transformers", "numpy<2"]          # the last Intel-Mac PyTorch (2.2) wants numpy 1.x
    return [torch, rest]


def hardware_note():
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin" and machine == "arm64":
        return ""
    if system in ("Windows", "Linux") and shutil.which("nvidia-smi"):
        return ""
    return ("No Apple Silicon or NVIDIA graphics found: placing markers runs on the processor here, "
            "which takes hours per movie.")


def setup_status():
    return dict(_SETUP)


def setup(data_root, on_done):
    """Create (or reuse) <data_root>/whisper-env and install PyTorch + transformers into it."""
    if _SETUP["running"]:
        return False
    _SETUP.update(running=True, line="Looking for Python…", ok=None)

    def run():
        root = subtitles.env_dir(data_root)
        try:
            py = subtitles.env_python(root)
            if not os.path.isfile(py):
                base = subtitles._find_base_python()
                if not base:
                    raise RuntimeError("needs Python 3.10 or newer")
                _SETUP["line"] = "Creating the environment…"
                subprocess.run([base, "-m", "venv", root], check=True, capture_output=True, timeout=600)
            subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], capture_output=True, timeout=600)
            for args in pip_steps():
                _SETUP["line"] = "Installing %s (this can take a while)…" % args[0]
                r = subprocess.run([py, "-m", "pip", "install"] + args, capture_output=True, text=True,
                                   timeout=7200)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout)[-300:])
            res = check(py, fresh=True)
            if not res.get("ready"):
                raise RuntimeError(res.get("error") or "PyTorch did not load")
            _SETUP.update(line="Ready — PyTorch %s on %s." % (res.get("torch"), res.get("device")), ok=True)
            on_done(py)
        except Exception as e:
            _SETUP.update(line="Setup failed: %s" % e, ok=False)
        finally:
            _SETUP["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def snapshot():
    with _LOCK:
        return dict(JOB, errors=list(JOB["errors"]))


def start(py, videos, log_path, on_each=None):
    """Tag these video files one after another in the background. False if a run is going."""
    cmd = command(py)
    if not cmd:
        raise RuntimeError("the scene tagger isn't set up")
    with _LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, done=0, total=len(videos), current="", line="Starting…", errors=[],
                   stopping=False)
    threading.Thread(target=_run, args=(cmd, list(videos), log_path, on_each), daemon=True).start()
    return True


def stop():
    with _LOCK:
        JOB["stopping"] = True
        p = _PROC["p"]
    if p and p.poll() is None:
        p.terminate()                  # the tagger finishes nothing half-written (atomic sidecars)


def _run(cmd, videos, log_path, on_each):
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    try:
        for vp in videos:
            with _LOCK:
                if JOB["stopping"]:
                    break
                JOB.update(current=os.path.basename(vp), line="Placing markers: %s" % os.path.basename(vp))
            fd, summ = tempfile.mkstemp(suffix=".json")
            os.close(fd)
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write("\n== %s %s\n" % (time.strftime("%Y-%m-%d %H:%M"), vp))
                lf.flush()
                p = subprocess.Popen(cmd + [vp, "--phase", "c", "--engine", "phar", "--summary-json", summ],
                                     stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=env, cwd=os.path.dirname(log_path))
                _PROC["p"] = p
                p.wait()
                _PROC["p"] = None
            try:
                os.remove(summ)
            except OSError:
                pass
            with _LOCK:
                if JOB["stopping"]:
                    break
                if p.returncode == 0:
                    JOB["done"] += 1
                else:
                    JOB["errors"].append("%s: the tagger exited with %s" % (os.path.basename(vp), p.returncode))
            if on_each and p.returncode == 0:
                try:
                    on_each(vp)
                except Exception:
                    pass
    finally:
        with _LOCK:
            n, stopped = JOB["done"], JOB["stopping"]
            JOB.update(running=False, current="", finished=time.time(), stopping=False,
                       line=("Stopped after %d" % n if stopped else "Done: %d movie%s" % (n, "" if n == 1 else "s"))
                       + ((" — %d failed, see the log" % len(JOB["errors"])) if JOB["errors"] else ""))
