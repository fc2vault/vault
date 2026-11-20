"""Updating Vault itself: Settings → Updates.

check() asks GitHub for the newest release of fc2vault/vault (once a day while Vault runs, and
when the tab opens; Settings can switch it off). install() downloads that release's installer
for this computer, checks its size and SHA-256 against what GitHub publishes for the file, and
hands over:

  Windows (installed with the setup): the new setup runs silently. It stops Vault, replaces the
      program files (settings and data stay in %LOCALAPPDATA%\\Vault) and starts Vault again.
  Mac (Vault.app): a small helper script, in its own session so it outlives Vault, copies the
      new Vault.app next to the old one, quits Vault, swaps the two (putting the old one back if
      anything fails) and opens the new one. Data stays in ~/Library/Application Support/Vault.
  Anything else (from source, Linux): no install — the tab links to the release.

Nothing about the user travels: the only requests are GitHub's release API and the download."""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

REPO = os.environ.get("VAULT_UPDATE_REPO") or "fc2vault/vault"
LATEST_URL = "https://api.github.com/repos/%s/releases/latest"
USER_AGENT = "vault-update"
CHECK_EVERY = 24 * 3600

STATE = {"checked": 0.0, "info": None}
PROGRESS = {"running": False, "state": "", "done": 0, "total": 0, "error": "", "version": ""}
_LOCK = threading.Lock()


def parse_version(s):
    """"v1.10.0" / "1.10.0" -> (1, 10, 0); None if it isn't one."""
    m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$", (s or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def install_kind(here):
    """How this copy of Vault was installed: ("mac", Vault.app path), ("windows", install dir),
    or ("source", None). `here` is the folder serve.py runs from."""
    here = os.path.abspath(here)
    parts = here.split(os.sep)
    if sys.platform == "darwin" and len(parts) > 3 and parts[-3:] == ["Contents", "Resources", "app"] \
            and parts[-4].endswith(".app"):
        return "mac", os.sep.join(parts[:-3])
    up = os.path.dirname(here)
    if os.name == "nt" and os.path.isfile(os.path.join(up, "launcher.pyw")) \
            and os.path.isdir(os.path.join(up, "python")):
        return "windows", up
    return "source", None


def asset_name(kind, machine=None):
    machine = (machine or platform.machine()).lower()
    if kind == "mac":
        return "Vault-mac-apple-silicon.dmg" if machine == "arm64" else "Vault-mac-intel.dmg"
    if kind == "windows":
        return "Vault-windows-setup.exe"
    return None


def _short(e):
    s = str(e) or e.__class__.__name__
    return s if len(s) < 120 else s[:117] + "..."


def check(current, kind, opener=urlopen, timeout=8):
    """{"current", "latest", "newer", "notes", "published", "page", "asset": {...}|None} or {"error"}."""
    try:
        with opener(Request(LATEST_URL % REPO, headers={"User-Agent": USER_AGENT,
                                                        "Accept": "application/vnd.github+json"}),
                    timeout=timeout) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        info = {"error": "could not reach GitHub (%s)" % _short(e), "current": current}
        STATE.update(checked=time.time(), info=info)
        return info
    tag = rel.get("tag_name") or ""
    want = asset_name(kind)
    asset = None
    for a in rel.get("assets") or []:
        if a.get("name") == want:
            dig = a.get("digest") or ""
            asset = {"name": want, "size": int(a.get("size") or 0), "url": a.get("browser_download_url") or "",
                     "sha256": dig.split(":", 1)[1].lower() if dig.startswith("sha256:") else ""}
    info = {"current": current, "latest": tag.lstrip("v"), "newer": is_newer(tag, current),
            "notes": (rel.get("body") or "").strip(), "published": rel.get("published_at") or "",
            "page": rel.get("html_url") or "", "asset": asset}
    STATE.update(checked=time.time(), info=info)
    return info


def due(enabled):
    return enabled and time.time() - STATE["checked"] > CHECK_EVERY


# ---------------------------------------------------------------- download


def download(asset, dest_dir, opener=urlopen, timeout=60):
    """Fetch the installer into dest_dir and verify it. -> path. Raises on any mismatch."""
    os.makedirs(dest_dir, exist_ok=True)
    if not asset or not asset.get("url"):
        raise RuntimeError("this release has no installer for this computer")
    if not asset["url"].startswith("https://github.com/%s/releases/download/" % REPO):
        raise RuntimeError("unexpected download address")
    path = os.path.join(dest_dir, asset["name"])
    part = path + ".part"
    h = hashlib.sha256()
    PROGRESS.update(state="downloading", done=0, total=asset.get("size") or 0)
    with opener(Request(asset["url"], headers={"User-Agent": USER_AGENT}), timeout=timeout) as r, \
            open(part, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            PROGRESS["done"] += len(chunk)
    size = os.path.getsize(part)
    if asset.get("size") and size != asset["size"]:
        os.remove(part)
        raise RuntimeError("the download is incomplete (%d of %d bytes)" % (size, asset["size"]))
    if not asset.get("sha256"):
        os.remove(part)
        raise RuntimeError("GitHub published no checksum for the installer")
    if h.hexdigest() != asset["sha256"]:
        os.remove(part)
        raise RuntimeError("the download doesn't match GitHub's checksum")
    os.replace(part, path)
    return path


# ---------------------------------------------------------------- hand over

MAC_HELPER = r'''#!/bin/bash
# Vault update helper (written by updater.py): swap Vault.app for the new one, then reopen it.
DMG="$1"; APP="$2"; LOG="$3"; BUNDLE="${4:-io.github.fc2vault.vault}"; REOPEN="${5:-1}"
exec >>"$LOG" 2>&1
echo "== $(date) updating $APP from $DMG"
MNT=$(mktemp -d /tmp/vault-update.XXXXXX)
STAGE="$(dirname "$APP")/.Vault-update.app"
OLD="$(dirname "$APP")/.Vault-old.app"
fail() { echo "FAILED: $1"; hdiutil detach -quiet "$MNT" 2>/dev/null; rm -rf "$STAGE"; if [ "$REOPEN" = 1 ]; then open "$APP"; fi; exit 1; }
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MNT" "$DMG" || fail "could not open the disk image"
[ -x "$MNT/Vault.app/Contents/MacOS/Vault" ] || fail "no Vault.app in the disk image"
rm -rf "$STAGE"
ditto "$MNT/Vault.app" "$STAGE" || fail "could not copy the new Vault.app"
hdiutil detach -quiet "$MNT"
osascript -e "tell application id \"$BUNDLE\" to quit" || true
for i in $(seq 1 60); do pgrep -f "$APP/Contents/MacOS/Vault" >/dev/null || break; sleep 0.5; done
pkill -f "$APP/Contents/MacOS/Vault" 2>/dev/null
pkill -f "$APP/Contents/Resources/python/" 2>/dev/null     # its server, if the app was killed
sleep 1
rm -rf "$OLD"
mv "$APP" "$OLD" || fail "could not move the old Vault.app aside"
if ! mv "$STAGE" "$APP"; then mv "$OLD" "$APP"; fail "could not put the new Vault.app in place"; fi
rm -rf "$OLD" "$DMG"
echo "updated"
if [ "$REOPEN" = 1 ]; then open "$APP" --args --no-browser; fi    # the page that asked reloads itself
exit 0
'''


def can_install(kind, target):
    """"" if this copy can update itself, else the reason."""
    if kind == "source":
        return "Vault runs from source here: update it with git pull, or install it with a download."
    if kind == "mac":
        if "/AppTranslocation/" in target:
            return "Move Vault into the Applications folder first (it runs from a temporary copy)."
        if not os.access(os.path.dirname(target), os.W_OK):
            return "Vault can't replace itself in %s (no permission)." % os.path.dirname(target)
    return ""


def install(info, kind, target, data_dir, opener=urlopen, start=True):
    """Download and hand over in the background. False if one is already running."""
    with _LOCK:
        if PROGRESS["running"]:
            return False
        PROGRESS.update(running=True, state="starting", done=0, total=0, error="",
                        version=info.get("latest") or "")

    def run():
        try:
            path = download(info.get("asset"), os.path.join(data_dir, "updates"), opener)
            PROGRESS["state"] = "installing"
            if start:
                _hand_over(kind, path, target, data_dir)
            PROGRESS["state"] = "restarting"
        except Exception as e:
            PROGRESS.update(state="error", error=_short(e))
        finally:
            PROGRESS["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def _hand_over(kind, path, target, data_dir):
    log = os.path.join(data_dir, "update.log")
    if kind == "windows":
        # Through "start", so the setup is not a child of this server: the setup stops Vault
        # first (launcher.pyw --stop ends the server's whole process tree).
        subprocess.Popen(["cmd", "/c", "start", "", path, "/VERYSILENT", "/SUPPRESSMSGBOXES",
                          "/NORESTART", "/RELAUNCH=1", "/LOG=" + log],
                         creationflags=0x08000000 | 0x00000008, close_fds=True)   # NO_WINDOW | DETACHED
    elif kind == "mac":
        helper = os.path.join(data_dir, "update-helper.sh")
        with open(helper, "w") as f:
            f.write(MAC_HELPER)
        os.chmod(helper, 0o755)
        reopen = "0" if os.environ.get("VAULT_UPDATE_NO_REOPEN") else "1"      # tests
        subprocess.Popen(["/bin/bash", helper, path, target, log, "io.github.fc2vault.vault", reopen],
                         start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise RuntimeError("this copy of Vault can't update itself")
