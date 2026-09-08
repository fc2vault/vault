#!/usr/bin/env python3
"""
organize.py — opt-in, preview-then-apply library tidy for Vault.

Normalizes each movie folder to  <CODE>/<CODE>.<ext>  (multi-part -> <CODE>-1, -2),
quarantines junk files (txt/html/url/… + system cruft) into  _vault_trash/, and
moves duplicate folders (same code) into  _Duplicates/  (keeping the largest copy).

Hard guarantees: **never deletes, never overwrites** (name collisions get a " (2)"
suffix). build_plan() is a pure dry-run; apply_plan() executes it.
"""
import os
import re
import shutil
from collections import defaultdict

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".webm", ".flv",
             ".m4v", ".rmvb", ".mpg", ".mpeg"}
# kept next to the video, NOT treated as junk:
KEEP_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".nfo",
            ".jpg", ".jpeg", ".png", ".webp", ".gif"}
JUNK_EXT = {".txt", ".html", ".htm", ".url", ".lnk", ".ini", ".db", ".log",
            ".js", ".exe", ".bat", ".rtf", ".doc", ".docx", ".php", ".website"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
SPECIAL_DIRS = {"_vault_trash", "_duplicates"}


def norm_code(name):
    if not name:
        return None
    m = re.search(r"FC2[-_\s]*PPV[-_\s]*(\d{5,10})", name, re.I)
    if not m:
        m = re.search(r"\bFC2[-_\s]*(\d{6,10})\b", name, re.I)
    return "FC2-PPV-" + m.group(1) if m else None


def _nat(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _is_junk(f):
    lf = f.lower()
    if f.startswith("._") or lf.startswith(".smbdelete"):
        return True
    if lf in JUNK_NAMES:
        return True
    return os.path.splitext(lf)[1] in JUNK_EXT


def _uniq(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def build_plan(library):
    """Return a list of planned actions (dry run). No filesystem changes."""
    trash = os.path.join(library, "_vault_trash")
    dupd = os.path.join(library, "_Duplicates")
    entries = []
    for dirpath, dirs, files in os.walk(library):
        dirs.sort()
        if os.path.basename(dirpath).lower() in SPECIAL_DIRS:
            dirs[:] = []
            continue
        vids = [f for f in files
                if os.path.splitext(f)[1].lower() in VIDEO_EXT and not f.startswith("._")]
        if not vids:
            continue
        sizes = {}
        for f in vids:
            try:
                sizes[f] = os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                sizes[f] = 0
        fname0 = os.path.splitext(max(vids, key=lambda f: sizes[f]))[0]
        code = norm_code(os.path.basename(dirpath)) or norm_code(fname0)
        junk = [f for f in files if _is_junk(f)]
        entries.append({"dir": dirpath, "code": code, "vids": vids, "sizes": sizes, "junk": junk})

    # duplicates: same code across multiple folders -> keep the largest
    bycode = defaultdict(list)
    for e in entries:
        if e["code"]:
            bycode[e["code"]].append(e)
    actions = []
    dup_dirs = set()
    for code, es in bycode.items():
        if len(es) < 2:
            continue
        es.sort(key=lambda e: -sum(e["sizes"].values()))
        for e in es[1:]:
            actions.append({"type": "duplicate", "code": code, "from": e["dir"],
                            "to": os.path.join(dupd, os.path.basename(e["dir"]))})
            dup_dirs.add(e["dir"])

    for e in entries:
        if e["dir"] in dup_dirs:
            continue
        d, code = e["dir"], e["code"]
        for j in e["junk"]:
            actions.append({"type": "junk", "code": code, "from": os.path.join(d, j),
                            "to": os.path.join(trash, os.path.basename(d), j)})
        if not code:
            continue                       # can't normalize without a code -> leave as-is
        vids = sorted(e["vids"], key=_nat)
        multi = len(vids) > 1
        for i, v in enumerate(vids):
            ext = os.path.splitext(v)[1].lower()
            newn = f"{code}{ext}" if not multi else f"{code}-{i + 1}{ext}"
            if v != newn:
                actions.append({"type": "file", "code": code, "from": os.path.join(d, v),
                                "to": os.path.join(d, newn)})
        # normalize the folder to a bare "<CODE>". Any name suffix (e.g.
        # "<CODE> - Actress Name") is harvested into the catalog DB by the caller
        # BEFORE this runs, so stripping it here loses nothing. Skip only when the
        # folder is already exactly the bare code.
        base = os.path.basename(d)
        if base != code and os.path.realpath(d) != os.path.realpath(library):
            actions.append({"type": "folder", "code": code, "from": d,
                            "to": os.path.join(os.path.dirname(d), code)})
    return actions


def action_id(a, library):
    """Stable id for one action, matching the relative-path id the web UI builds, so the
    client can pass a skip-set that survives a fresh build_plan() at apply time."""
    f = a["from"]
    rel = f[len(library):].lstrip("/") if f.startswith(library) else f
    return f'{a["type"]}|{rel}'


def _do(a):
    os.makedirs(os.path.dirname(a["to"]), exist_ok=True)
    dst = a["to"] if not os.path.exists(a["to"]) else _uniq(a["to"])
    try:
        os.rename(a["from"], dst)           # same share -> instant
    except OSError:
        shutil.move(a["from"], dst)         # cross-device fallback


def apply_plan(library, skip=None):
    """Execute build_plan(). Safe order: dup-moves, junk, file renames, folder renames.
    `skip` is a set of action_id()s to leave untouched (per-change deselection in the UI)."""
    skip = skip or set()
    actions = [a for a in build_plan(library) if action_id(a, library) not in skip]
    order = {"duplicate": 0, "junk": 1, "file": 2, "folder": 3}
    actions.sort(key=lambda a: order.get(a["type"], 9))
    done = {"duplicate": 0, "junk": 0, "file": 0, "folder": 0}
    errors = []
    for a in actions:
        try:
            _do(a)
            done[a["type"]] += 1
        except Exception as ex:
            errors.append(f'{a["type"]}: {os.path.basename(a["from"])}: {ex}')
    return {"done": done, "total": sum(done.values()), "errors": errors[:20]}


if __name__ == "__main__":
    import json
    import sys
    lib = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "--apply":
        print(json.dumps(apply_plan(lib), indent=2))
    else:
        acts = build_plan(lib)
        from collections import Counter
        print("DRY RUN —", dict(Counter(a["type"] for a in acts)))
        for a in acts[:40]:
            print(f'  {a["type"]:9} {a["from"]}  ->  {a["to"]}')
