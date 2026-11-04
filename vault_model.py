"""The scene model: where it lives, what the newest one is, how it is fetched.

Standard library only. Models are release assets of the public GitHub repository MODEL_REPO
(one release per build, tag ``vault-r<N>``, one ``vault-r<N>.zip``). Vault asks GitHub for the
newest release and downloads the zip from it; nothing about the user travels.

The model lands in ``user_models_dir()/vault-r<N>/``, a per-user folder that vault-scene-tagger
looks in as well, so a downloaded model is picked up by the tagger with no further setup.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from urllib.request import Request, urlopen

MODEL_REPO = os.environ.get("VAULT_MODEL_REPO") or "fc2vault/models"
RELEASES_URL = "https://api.github.com/repos/%s/releases/latest" % MODEL_REPO
_ROUND_RE = re.compile(r"^vault-r(\d+)$")
USER_AGENT = "vault-model"

# One download at a time; the Scene model tab polls this.
PROGRESS = {"running": False, "state": "", "done": 0, "total": 0, "error": "", "name": ""}
_LOCK = threading.Lock()
_LATEST = {"at": 0.0, "info": None}


def user_models_dir():
    """Per user, per platform. The same function lives in vault-scene-tagger's phar.py; keep
    them identical, it is the contract that lets the tagger find what Vault downloaded."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Vault")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Vault")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                            "vault")
    return os.path.join(base, "models")


def installed(models_dir=None):
    """Name of the newest installed model (``vault-r<N>`` with a config.json), or None."""
    models_dir = models_dir or user_models_dir()
    rounds = []
    try:
        for name in os.listdir(models_dir):
            m = _ROUND_RE.match(name)
            if m and os.path.isfile(os.path.join(models_dir, name, "config.json")):
                rounds.append((int(m.group(1)), name))
    except OSError:
        pass
    return max(rounds)[1] if rounds else None


def _parse_release(rel):
    """GitHub's release JSON -> {name, file, size, published, notes, url}, or {error}."""
    asset = next((a for a in rel.get("assets") or [] if (a.get("name") or "").endswith(".zip")), None)
    if not asset:
        return {"error": "the newest release has no model zip"}
    return {"name": rel.get("tag_name") or "", "file": asset.get("name") or "model.zip",
            "size": int(asset.get("size") or 0), "published": rel.get("published_at") or "",
            "notes": (rel.get("body") or "").strip(), "url": asset.get("browser_download_url") or ""}


def latest(timeout=8, max_age=600, opener=urlopen):
    """The newest release: {name, file, size, published, notes, url} or {error}.
    Cached for max_age seconds so opening Settings repeatedly doesn't hit the network."""
    now = time.time()
    if _LATEST["info"] is not None and now - _LATEST["at"] < max_age:
        return _LATEST["info"]
    try:
        with opener(Request(RELEASES_URL, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/vnd.github+json"}),
                    timeout=timeout) as r:
            info = _parse_release(json.loads(r.read().decode("utf-8")))
    except Exception as e:   # offline, DNS, 404 before the first model is published, ...
        info = {"error": "could not reach GitHub for the model (%s)" % _short(e)}
    if "error" not in info:
        _LATEST.update(at=now, info=info)
    return info


def _short(e):
    s = str(e) or e.__class__.__name__
    return s if len(s) < 120 else s[:117] + "..."


def install_zip(zip_path, models_dir):
    """Unpack ``vault-r<N>.zip`` (one top-level folder with a config.json) into models_dir.
    Refuses anything else: paths that leave the folder, several top folders, no config."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise ValueError("the model zip must hold exactly one folder")
        top = tops.pop()
        if not _ROUND_RE.match(top):
            raise ValueError("the model zip folder is not named vault-r<N>")
        for n in names:
            parts = n.split("/")
            if n.startswith("/") or ".." in parts or ":" in n or "\\" in n:
                raise ValueError("unsafe path in the model zip: %s" % n)
        if top + "/config.json" not in names:
            raise ValueError("the model zip has no config.json")
        dest = os.path.join(models_dir, top)
        tmp = dest + ".part"
        shutil.rmtree(tmp, ignore_errors=True)
        for n in names:
            out = os.path.join(tmp, *n.split("/")[1:])
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(n) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        shutil.rmtree(dest, ignore_errors=True)
        os.rename(tmp, dest)
    return top


def download(models_dir=None, opener=urlopen, timeout=30):
    """Fetch the newest model release and install it. Blocks; run in a thread.
    Returns the installed model name, raises on failure. PROGRESS tracks it either way."""
    models_dir = models_dir or user_models_dir()
    with _LOCK:
        if PROGRESS["running"]:
            raise RuntimeError("a model download is already running")
        PROGRESS.update(running=True, state="connecting", done=0, total=0, error="", name="")
    try:
        info = latest(opener=opener)
        if "error" in info:
            raise RuntimeError(info["error"])
        if not info.get("url"):
            raise RuntimeError("the release has no download link")
        PROGRESS.update(name=info.get("name") or "", total=int(info.get("size") or 0))
        os.makedirs(models_dir, exist_ok=True)
        zpath = os.path.join(models_dir, (info.get("file") or "model.zip") + ".part")
        req = Request(info["url"], headers={"User-Agent": USER_AGENT})
        try:
            r = opener(req, timeout=timeout)
        except Exception as e:
            raise RuntimeError("download refused (%s)" % _short(e))
        with r, open(zpath, "wb") as f:
            PROGRESS["state"] = "downloading"
            total = int(r.headers.get("Content-Length") or PROGRESS["total"] or 0)
            if total:
                PROGRESS["total"] = total
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                PROGRESS["done"] += len(chunk)
        if PROGRESS["total"] and PROGRESS["done"] != PROGRESS["total"]:
            raise RuntimeError("download incomplete (%d of %d bytes)" % (PROGRESS["done"], PROGRESS["total"]))
        PROGRESS["state"] = "unpacking"
        name = install_zip(zpath, models_dir)
        os.remove(zpath)
        PROGRESS.update(state="done", name=name)
        return name
    except Exception as e:
        PROGRESS.update(state="error", error=_short(e))
        raise
    finally:
        PROGRESS["running"] = False
