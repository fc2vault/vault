#!/usr/bin/env python3
"""
refolder_fc2.py — Turn a FLAT folder of loose FC2 movie files into the
per-release folder structure produced by rename_fc2.py.

For "1_FC2 PPV/" (thousands of loose "[FC2-PPV] - <number> ....mp4" files) it:
  1. Groups every movie file by its FC2 number (and category PPV / OMU).
  2. Creates a folder  FC2-PPV-<number>  (or FC2-OMU-<number>) per release.
  3. Moves the movie file(s) in, renamed to  FC2-PPV-<number>.<ext>
       - multi-file releases -> FC2-PPV-<number>-1.<ext>, -2, ... (sorted order)
       - if the files are genuinely different EDITS (not just parts), the
         distinguishing edit name is appended:  FC2-PPV-<number>-2 - りこフェラ.mp4
       - non-descriptive junk ("- JAV HD Porn", "-Master", resolution/codec
         tags, etc.) is stripped from those edit names.
  4. Cleans up existing release sub-folders using rename_fc2's own logic.
  5. Deletes only clear junk: .DS_Store, hidden dot-files, partial downloads
     (.sgcbxi/.part/...), and non-video sidecars (.txt/.html/images).
     Real movie files are NEVER deleted, regardless of size.
  6. Moves files with no detectable number to  _needs_review/  for you to sort.

SAFETY: dry run by default. Nothing changes until you pass --apply.

Usage:
    python3 refolder_fc2.py                 # preview (dry run)
    python3 refolder_fc2.py --apply         # actually move/rename
    python3 refolder_fc2.py "/other/path"   # different flat folder
"""

import argparse
import os
import re
import sys

# Reuse the shared machinery from the sibling script (same project dir).
from rename_fc2 import Runner, human, process_folder, identify, VIDEO_EXTS

# No default root. This tool CLEANS folders (see rename_fc2.process_folder), and pointing it
# at the live library is destructive, so the path must be given explicitly.
DEFAULT_ROOT = None


def _configured_library():
    """The library Vault is serving, read from webui/config.json when it is there."""
    import json
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            return (json.load(f).get("library") or "").rstrip("/")
    except Exception:
        return ""
REVIEW_DIR = "_needs_review"
KEEP_DIRS = {"Duplicates", REVIEW_DIR}

# Extensions that are junk sidecars / partial downloads -> safe to delete.
JUNK_EXTS = {
    ".txt", ".html", ".htm", ".nfo", ".url", ".db", ".jpg", ".jpeg", ".png",
    ".gif", ".webp", ".sgcbxi", ".part", ".crdownload", ".tmp", ".!ut", ".aria2",
}

# Category + number from "[FC2-PPV] - 1234567 ..." / "[FC2-OMU] - 340671 ...".
PREFIX_RE = re.compile(r"FC2[\s_\-]*(PPV|OMU)", re.IGNORECASE)
# FC2 ids are >=5 digits; this avoids grabbing stray short numbers like "007".
HEAD_STRIP_RE = re.compile(r"^\[?\s*FC2[\s_\-]*(?:PPV|OMU)?\s*\]?\s*[-–]?\s*",
                           re.IGNORECASE)
NUMBER_RE = re.compile(r"([0-9]{5,10})")

# Non-descriptive noise stripped from edit-name suffixes.
JUNK_PHRASE_RE = re.compile(
    r"(jav\s*hd\s*porn(?:\s*[-–]?\s*master)?"
    r"|jav\s*uncensored"
    r"|fc2\s*jav\s*fc2\s*ppv\s*amater"
    r"|jav\s*fc2\s*ppv\s*amater"
    r"|\bamater\b"
    r"|porn\s*[-–]?\s*master"
    r"|[-–]?\s*master\b"
    r"|japteenx[\s-]*\d*"
    r"|vjav\.com[-\s]*\d*"
    r"|\[fc2[\s_-]*(?:ppv|omu)\]"
    r"|\b\d{3,4}[pP]\b|\b4k\b|\b2k\b|\bhevc\b|h\.?26[45]|x26[45])",
    re.IGNORECASE,
)
# A standalone short part-index (needs an explicit separator before it, so that
# attached tokens like "高画質1" are preserved as genuine edit distinctions).
PART_IDX_RE = re.compile(r"[\s_\-–.]+([0-9]{1,2}|[A-Da-d])(?=[\s_\-–.]|$)")


def split_ext(name):
    """Return (stem, ext) collapsing accidental double extensions (foo.mp4.mp4)."""
    stem, ext = os.path.splitext(name)
    if ext.lower() in VIDEO_EXTS:
        inner, inner_ext = os.path.splitext(stem)
        if inner_ext.lower() == ext.lower():
            stem = inner
    return stem, ext.lower()


def parse(name):
    """Return (prefix, number) or (None, None) if no FC2 id is present."""
    prefix = "PPV"
    m = PREFIX_RE.search(name)
    if m:
        prefix = m.group(1).upper()
    head = HEAD_STRIP_RE.sub("", name)
    nm = NUMBER_RE.search(head) or NUMBER_RE.search(name)
    return (prefix, nm.group(1)) if nm else (None, None)


def edit_name(filename, number):
    """Descriptive remainder that distinguishes one edit from another, or ''."""
    stem, _ = split_ext(filename)
    s = HEAD_STRIP_RE.sub("", stem)
    s = s.replace(number, " ", 1)          # drop the id
    s = JUNK_PHRASE_RE.sub(" ", s)          # drop non-descriptive noise
    s = PART_IDX_RE.sub(" ", s)             # drop standalone part indices
    s = re.sub(r"[\s_\-–.]+", " ", s).strip(" -–_.")
    return s


def plan_group(prefix, number, filenames):
    """Return target_base and a list of (src_name, dst_name) for one release."""
    target_base = f"FC2-{prefix}-{number}"
    files = sorted(filenames)
    descs = [edit_name(f, number) for f in files]
    # Genuinely different edits -> keep the distinguishing names.
    distinguishing = len(set(descs)) > 1 and any(descs)
    multi = len(files) > 1
    plan = []
    for idx, (f, desc) in enumerate(zip(files, descs), start=1):
        _, ext = split_ext(f)
        base = f"{target_base}-{idx}" if multi else target_base
        if distinguishing and desc:
            base += f" - {desc}"
        plan.append((f, base + ext))
    return target_base, plan


def main():
    ap = argparse.ArgumentParser(description="Refolder a flat FC2 movie dump.")
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT,
                    help="the DOWNLOAD folder to refolder (never the library)")
    ap.add_argument("--i-know-this-is-the-library", action="store_true",
                    help="allow running on the configured Vault library (it will clean it)")
    ap.add_argument("--apply", action="store_true",
                    help="actually move/rename (default is a dry run)")
    ap.add_argument("--min-size", type=float, default=80.0,
                    help="spam threshold (MB) used only when cleaning existing "
                         "sub-folders; loose movies are never size-filtered")
    args = ap.parse_args()

    root = args.root
    if not root:
        sys.exit("error: give the folder to refolder, e.g. refolder_fc2.py ~/Downloads/fc2\n"
                 "       (there is no default: this tool deletes junk inside the folders it "
                 "processes)")
    if not os.path.isdir(root):
        sys.exit(f"error: not a directory: {root}")
    lib = _configured_library()
    if lib and os.path.realpath(root).rstrip("/") == os.path.realpath(lib).rstrip("/") \
            and not args.i_know_this_is_the_library:
        sys.exit(f"refusing to run on the Vault library: {lib}\n"
                 "  This cleans each release folder and would delete companion files.\n"
                 "  Use organize.py for the library, or pass "
                 "--i-know-this-is-the-library to override.")
    min_bytes = int(args.min_size * 1024 * 1024)
    runner = Runner(apply=args.apply)

    print(f"Root : {root}")
    print(f"Mode : {'APPLY' if args.apply else 'DRY RUN — pass --apply to execute'}\n")

    entries = sorted(os.listdir(root))
    groups = {}          # (prefix, number) -> [filenames]
    review = []          # unidentifiable files -> _needs_review
    existing_dirs = []   # release sub-folders to clean via rename_fc2

    # --- classify every entry -------------------------------------------------
    for e in entries:
        path = os.path.join(root, e)

        if os.path.isdir(path):
            if e in KEEP_DIRS:
                continue
            # Bracket-form folders ("[FC2-PPV] - 4942041") need the same
            # prefix/number parse as loose files; fall back to identify().
            d_prefix, d_number = parse(e)
            code = f"FC2-{d_prefix}-{d_number}" if d_number else identify(e)
            if code:
                existing_dirs.append((e, code))
            else:
                print(f"SKIP dir (unrecognized): {e}")
            continue

        if e == ".DS_Store" or e.startswith("."):
            runner.delete_file(path)          # junk / hidden partial download
            continue

        _, ext = split_ext(e)
        if ext in JUNK_EXTS:
            runner.delete_file(path)
            continue
        if ext not in VIDEO_EXTS:
            print(f"      ? unknown type, sending to {REVIEW_DIR}: {e}")
            review.append(e)
            continue

        prefix, number = parse(e)
        if number is None:
            review.append(e)
            continue
        groups.setdefault((prefix, number), []).append(e)

    # --- move loose files into per-release folders ---------------------------
    made, moved, collisions = 0, 0, []
    for (prefix, number), filenames in sorted(groups.items()):
        target_base, plan = plan_group(prefix, number, filenames)
        dest_dir = os.path.join(root, target_base)

        if os.path.isdir(dest_dir):
            # A release folder already exists for this id — don't risk a merge.
            collisions.append(target_base)
            review.extend(filenames)
            continue

        runner.makedirs(dest_dir)
        made += 1
        for src_name, dst_name in plan:
            runner.rename(os.path.join(root, src_name),
                          os.path.join(dest_dir, dst_name))
            moved += 1

    # --- move unidentifiable files into _needs_review ------------------------
    if review:
        review_path = os.path.join(root, REVIEW_DIR)
        runner.makedirs(review_path)
        for name in sorted(set(review)):
            runner.rename(os.path.join(root, name),
                          os.path.join(review_path, name))

    # --- clean pre-existing release sub-folders via rename_fc2 ----------------
    for e, code in existing_dirs:
        print(f"CLEAN existing folder: {e}\n      -> {code}")
        process_folder(os.path.join(root, e), code, runner, min_bytes)

    # --- summary --------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"Release folders created : {made}")
    print(f"Movie files moved       : {moved}")
    print(f"Existing folders cleaned: {len(existing_dirs)}")
    print(f"Sent to {REVIEW_DIR:<15}: {len(set(review))}")
    for name in sorted(set(review)):
        print(f"   - {name}")
    if collisions:
        print(f"Collisions (folder already existed): {collisions}")
    if not args.apply:
        print("\nDRY RUN — re-run with --apply to make these changes.")


if __name__ == "__main__":
    main()
