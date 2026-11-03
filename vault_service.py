"""
vault_service.py — run Vault at login as a background service.

    python3 serve.py --install-service      start at login, restart if it dies, keep running
    python3 serve.py --uninstall-service    stop it and remove the login entry
    python3 serve.py --service-status

Per platform it writes the native per-user service definition, nothing system-wide and no
administrator rights:

    macOS    ~/Library/LaunchAgents/local.vault.server.plist      (launchd, KeepAlive)
    Linux    ~/.config/systemd/user/vault.service                 (systemd --user)
    Windows  Scheduled task "Vault", trigger "at log on", run with pythonw.exe (no console window)

The service runs the same interpreter that ran the install command, with the same serve.py,
so a `git pull` is picked up on the next restart. Standard library only.
"""
import os
import plistlib
import shutil
import subprocess
import sys

LABEL = "local.vault.server"        # launchd label / systemd unit name / task name base
TASK = "Vault"                      # Windows task name


def _run(cmd, ok_codes=(0,)):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in ok_codes:
        raise RuntimeError("%s failed: %s" % (cmd[0], (r.stderr or r.stdout).strip()))
    return r.stdout


def _python():
    exe = sys.executable
    if sys.platform == "win32":
        # pythonw has no console window; same folder as python.exe
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            exe = w
    return exe


def _argv(serve_py, port, library):
    args = [_python(), serve_py, "--port", str(port)]
    if library:
        args += ["--library", library]
    return args


# ---------------------------------------------------------------- macOS
def _plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def _install_darwin(serve_py, port, library, log, env):
    path = _plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    spec = {
        "Label": LABEL,
        "ProgramArguments": _argv(serve_py, port, library),
        "WorkingDirectory": os.path.dirname(serve_py),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": dict(env, PATH=os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin")),
    }
    _uninstall_darwin(quiet=True)
    with open(path, "wb") as f:
        plistlib.dump(spec, f)
    _run(["launchctl", "load", "-w", path])
    msg = ("installed %s\nVault starts at login and restarts if it stops. Log: %s"
           % (path, log))
    # macOS privacy: a launchd job cannot show the "allow access to Documents?" prompt, so a
    # Vault folder or library under Documents / Desktop / Downloads is refused outright.
    import time
    time.sleep(2.5)
    try:
        tail = open(log, errors="replace").read()[-2000:]
    except OSError:
        tail = ""
    if "Operation not permitted" in tail:
        msg += ("\n\nmacOS blocked the service from reading its files (see the log). Background "
                "jobs get no access to Documents, Desktop, Downloads or removable volumes.\n"
                "Either move the Vault folder somewhere else (for example ~/vault) and run "
                "--install-service again, or give python3 Full Disk Access in System Settings "
                "→ Privacy & Security → Full Disk Access:\n  %s" % _python())
    return msg


def _uninstall_darwin(quiet=False):
    path = _plist_path()
    if os.path.exists(path):
        subprocess.run(["launchctl", "unload", "-w", path], capture_output=True)
        os.remove(path)
        return "removed %s — Vault no longer starts at login" % path
    return "" if quiet else "no service installed (%s not found)" % path


def _status_darwin():
    if not os.path.exists(_plist_path()):
        return "not installed"
    out = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    return "installed, " + ("running" if out.returncode == 0 else "not running")


# ---------------------------------------------------------------- Linux (systemd --user)
def _unit_path():
    return os.path.expanduser("~/.config/systemd/user/vault.service")


def _install_linux(serve_py, port, library, log, env):
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl not found — this Linux has no systemd; start Vault from "
                           "your init system or a cron @reboot line instead")
    path = _unit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    q = lambda s: '"' + s.replace('"', '\\"') + '"'
    lines = ["[Unit]", "Description=Vault — local movie library", "After=network.target", "",
             "[Service]",
             "ExecStart=" + " ".join(q(a) for a in _argv(serve_py, port, library)),
             "WorkingDirectory=" + os.path.dirname(serve_py),
             "Restart=on-failure", "RestartSec=3",
             "StandardOutput=append:" + log, "StandardError=append:" + log]
    lines += ["Environment=%s=%s" % (k, q(v)) for k, v in env.items()]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", "vault.service"])
    return ("installed %s\nVault starts at login and restarts if it stops. Log: %s\n"
            "For a headless box that should run it without anyone logged in:\n"
            "  loginctl enable-linger %s" % (path, log, os.environ.get("USER", "$USER")))


def _uninstall_linux(quiet=False):
    path = _unit_path()
    if os.path.exists(path):
        subprocess.run(["systemctl", "--user", "disable", "--now", "vault.service"], capture_output=True)
        os.remove(path)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "removed %s — Vault no longer starts at login" % path
    return "" if quiet else "no service installed (%s not found)" % path


def _status_linux():
    if not os.path.exists(_unit_path()):
        return "not installed"
    out = subprocess.run(["systemctl", "--user", "is-active", "vault.service"],
                         capture_output=True, text=True)
    return "installed, " + (out.stdout.strip() or "unknown")


# ---------------------------------------------------------------- Windows (Task Scheduler)
def _install_windows(serve_py, port, library, log, env):
    if env:
        # a scheduled task has no per-task environment; VAULT_DATA etc. go through a .cmd shim
        shim = os.path.join(os.path.dirname(log), "vault-service.cmd")
        with open(shim, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
            for k, v in env.items():
                f.write('set "%s=%s"\r\n' % (k, v))
            f.write('start "" /B ' + " ".join('"%s"' % a for a in _argv(serve_py, port, library))
                    + ' > "%s" 2>&1\r\n' % log)
        tr = '"%s"' % shim
    else:
        tr = " ".join('"%s"' % a for a in _argv(serve_py, port, library))
    _run(["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", tr])
    _run(["schtasks", "/Run", "/TN", TASK])
    return ("installed scheduled task \"%s\" (runs at log on)\nVault is starting now; from the "
            "next log on it starts on its own. Log: %s\nTask Scheduler → Task Scheduler Library "
            "→ Vault to stop or change it." % (TASK, log))


def _uninstall_windows(quiet=False):
    out = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True)
    if out.returncode == 0:
        subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
        _run(["schtasks", "/Delete", "/F", "/TN", TASK])
        return "removed scheduled task \"%s\" — Vault no longer starts at log on" % TASK
    return "" if quiet else "no service installed (task \"%s\" not found)" % TASK


def _status_windows():
    out = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"], capture_output=True, text=True)
    if out.returncode != 0:
        return "not installed"
    state = [l.split(":", 1)[1].strip() for l in out.stdout.splitlines() if l.strip().startswith("Status")]
    return "installed, " + (state[0].lower() if state else "unknown")


# ---------------------------------------------------------------- dispatch
def _platform():
    if sys.platform == "darwin":
        return _install_darwin, _uninstall_darwin, _status_darwin
    if sys.platform.startswith("linux"):
        return _install_linux, _uninstall_linux, _status_linux
    if sys.platform == "win32":
        return _install_windows, _uninstall_windows, _status_windows
    raise RuntimeError("no service installer for this platform (%s)" % sys.platform)


def install(serve_py, port, library, log, env=None):
    """Register serve.py as a per-user login service and start it. Returns a message."""
    return _platform()[0](os.path.abspath(serve_py), int(port), library, os.path.abspath(log),
                          dict(env or {}))


def uninstall():
    return _platform()[1]()


def status():
    return _platform()[2]()
