"""Vault for Windows: start the server in the background and open the browser.

    pythonw launcher.pyw              start (if needed) and open Vault in the browser
    pythonw launcher.pyw --background start at log on, without opening the browser
    pythonw launcher.pyw --stop       stop the server
    pythonw launcher.pyw --library    choose a different movie folder

The installer puts this next to app\\ (the release), python\\ and bin\\ (ffmpeg, ffprobe).
Writable data (config, cache, logs) lives in %LOCALAPPDATA%\\Vault."""
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app")
BIN = os.path.join(HERE, "bin")
PYW = os.path.join(HERE, "python", "pythonw.exe")
DATA = os.environ.get("VAULT_DATA") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "Vault")
CONFIG = os.path.join(DATA, "config.json")
LOG = os.path.join(DATA, "server.log")
PIDFILE = os.path.join(DATA, "server.pid")


def config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def port():
    try:
        return int(config().get("port") or 8730)
    except (TypeError, ValueError):
        return 8730


def url():
    return "http://127.0.0.1:%d/" % port()


def answers():
    try:
        urllib.request.urlopen(url(), timeout=2).close()
        return True
    except Exception as e:                       # an HTTP error still means it is up
        return hasattr(e, "code")


def message(title, text, error=False):
    import tkinter
    from tkinter import messagebox
    root = tkinter.Tk()
    root.withdraw()
    (messagebox.showerror if error else messagebox.showinfo)(title, text, parent=root)
    root.destroy()


def choose_library(first):
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    title = ("Welcome to Vault: choose the folder that holds your movies" if first
             else "Choose the folder that holds your movies")
    path = filedialog.askdirectory(title=title, parent=root, mustexist=True)
    root.destroy()
    return os.path.normpath(path) if path else ""


def log_tail(n=8):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).strip()
    except OSError:
        return ""


def start(library):
    env = dict(os.environ, VAULT_DATA=DATA, PYTHONDONTWRITEBYTECODE="1",
               PATH=BIN + os.pathsep + os.environ.get("PATH", ""))
    flags = 0x08000000 | 0x00000200              # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    log = open(LOG, "w", encoding="utf-8")
    p = subprocess.Popen([PYW, "serve.py", "--library", library, "--port", str(port())],
                         cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    return p


def stop():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=0x08000000)
    try:
        os.remove(PIDFILE)
    except OSError:
        pass
    return True


def main():
    os.makedirs(DATA, exist_ok=True)
    args = sys.argv[1:]
    if "--stop" in args:
        stop()
        return
    if "--library" in args:
        lib = choose_library(False)
        if lib:
            stop()
            cfg = config()
            cfg["library"] = lib
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            time.sleep(1)
            args = []                            # fall through: start again with the new folder
        else:
            return
    background = "--background" in args
    if answers():
        if not background:
            webbrowser.open(url())
        return
    lib = config().get("library") or ""
    if not lib or not os.path.isdir(lib):
        if background:
            return                               # at log on with no library: nothing to do
        lib = choose_library(first=not lib)
        if not lib:
            return
    p = start(lib)
    for _ in range(600):                         # the first start scans the whole library
        if answers():
            if not background:
                webbrowser.open(url())
            return
        if p.poll() is not None:
            message("Vault could not start", log_tail() or "The server ended.", error=True)
            return
        time.sleep(0.5)
    message("Vault", "Vault is still starting. Open %s in a minute." % url())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                       # pythonw has no console: show it
        try:
            message("Vault", "Something went wrong:\n%s" % e, error=True)
        except Exception:
            pass
