#!/usr/bin/env python3
"""
rename_fc2.py — Normalize FC2-PPV folders in the "spare" directory.

For every sub-folder that can be identified as an FC2-PPV release it will:
  1. Delete files that are not the actual movie file (txt, html, images, etc.).
  2. Delete movie files smaller than the size threshold (default 80 MB) — these
     are almost always spam / advertising clips.
  3. Delete leftover junk sub-directories.
  4. Rename the remaining movie file(s) to  FC2-PPV-<number>.<ext>
     (multi-part releases become  FC2-PPV-<number>-1.<ext>, -2, ...).
  5. Rename the folder itself to  FC2-PPV-<number>.

Folders that are NOT recognizable as FC2 (e.g. PKYS-001, N0852, _move) are
skipped and reported — nothing about them is touched.

SAFETY: runs as a DRY RUN by default. Nothing is changed until you pass --apply.

Usage:
    python3 rename_fc2.py                 # preview (dry run) on the default folder
    python3 rename_fc2.py --apply         # actually make the changes
    python3 rename_fc2.py --min-size 100  # change the delete-below threshold (MB)
    python3 rename_fc2.py "/some/other/path" --apply
"""

import argparse
import os
import re
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ROOT = "/path/to/downloads"

# File extensions treated as "movie files".
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".mpg", ".mpeg",
    ".ts", ".flv", ".webm", ".m2ts", ".vob", ".rmvb", ".rm", ".asf",
}

# Releases carrying a trailing "-HD" folder suffix or a "nyap2p" filename are
# low-quality re-encodes. They are still renamed normally, but the folder gets
# this suffix appended so you know to re-download a proper copy.
REFLAG_SUFFIX = "-redownload proper"
HD_SUFFIX_RE = re.compile(r"[-_ ]HD\s*$", re.IGNORECASE)  # trailing HD only


def needs_reflag(folder_name, entry_names):
    """True if this release should be flagged for re-download (HD/nyap2p)."""
    if folder_name.endswith(REFLAG_SUFFIX):
        return True  # already flagged — keep it flagged on re-runs (idempotent)
    if HD_SUFFIX_RE.search(folder_name):
        return True
    names = [folder_name] + list(entry_names)
    return any("nyap2p" in n.lower() for n in names)


# ---------------------------------------------------------------------------
# Code recognizers
# ---------------------------------------------------------------------------
# Each recognizer inspects a folder name and returns a NORMALIZED base code
# (used for both the folder name and the movie file name), or None.
# They are tried in order; the first match wins. FC2 is tried first because it
# is the common case. To support a new studio, add a recognizer to RECOGNIZERS.

# FC2 in any observed spelling:
#   FC2-PPV-1234567 / FC2PPV 1234567 / FC2PPV-1234567 / fc2-ppv-1234567-HD
#   (無修正) FC2 PPV 1790483 ... / FC2-3061625 (bare "FC2" + number, no "PPV")
# "PPV" is optional so bare `FC2-<number>` folders normalize to FC2-PPV-<number>.
# FC2-OMU is unaffected: the letters "OMU" block the digits, so this misses it
# and _fc2_omu (tried next) catches it.
FC2_RE = re.compile(r"FC2[\s_\-]*(?:PPV[\s_\-]*)?([0-9]{5,10})", re.IGNORECASE)

# Tokyo-Hot style N-code, e.g. "[Uncensored HD] N0852 ..." -> N0852
NCODE_RE = re.compile(r"(?<![A-Za-z0-9])N([0-9]{3,4})(?![0-9])", re.IGNORECASE)

# Standard studio code LETTERS-DIGITS, e.g. "PKYS-001 - mitsuki" -> PKYS-001
JAV_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2,6})[\s_\-]?([0-9]{2,5})(?![0-9])")


# FC2-OMU is a separate FC2 category, e.g. "[FC2-OMU] - 340671 ..." -> FC2-OMU-340671
FC2_OMU_RE = re.compile(r"FC2[\s_\-]*OMU[\s_\-]*([0-9]{5,10})", re.IGNORECASE)

# 10Musume: date-based scene code MMDDYY_NN with a "10MU"/"10musume" studio tag,
#   e.g. "081726_01-10MU" or "4k688.com@081726_01-10MU.mp4" -> 10musume-081726_01
# The studio token is required so the generic date_scene pattern can't misfire on
# non-10Musume folders. Idempotent: already-normalized "10musume-081726_01" re-matches.
MUSUME_TAG_RE = re.compile(r"10\s*mu(?:sume)?", re.IGNORECASE)
MUSUME_CODE_RE = re.compile(r"([0-9]{6}_[0-9]{2})")


def _fc2(name):
    m = FC2_RE.search(name)
    return f"FC2-PPV-{m.group(1)}" if m else None


def _fc2_omu(name):
    m = FC2_OMU_RE.search(name)
    return f"FC2-OMU-{m.group(1)}" if m else None


def _musume(name):
    # Require the 10MU/10musume studio tag before trusting the date_scene code.
    if not MUSUME_TAG_RE.search(name):
        return None
    m = MUSUME_CODE_RE.search(name)
    return f"10musume-{m.group(1)}" if m else None


def _ncode(name):
    m = NCODE_RE.search(name)
    return f"N{m.group(1)}" if m else None


def _jav(name):
    m = JAV_RE.search(name)
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


# Order matters — most specific first. _musume must precede _jav: without it the
# broad JAV matcher would mis-read a normalized "10musume-081726_01" as "MUSUME-08172".
RECOGNIZERS = (_fc2, _fc2_omu, _musume, _ncode, _jav)


def identify(name):
    """Return the normalized base code for a folder, or None if unrecognized.
    Note: an appended actress name (see extract_actress) does not interfere —
    the recognizers match the code pattern and ignore the trailing name."""
    for recognizer in RECOGNIZERS:
        code = recognizer(name)
        if code:
            return code
    return None


# ---------------------------------------------------------------------------
# Actress-name preservation
# ---------------------------------------------------------------------------
# If a release's original folder/file name carries an actress name, keep it so
# the normalized name is "<CODE> <Actress>" instead of a bare "<CODE>".
# Conservative on purpose: Latin (romaji) names only, so we never mistake a long
# Japanese *title* for a name. Japanese names are left out rather than risk junk.

_ACTRESS_JUNK = {
    "vr", "hd", "fhd", "uhd", "sd", "4k", "8k", "uncensored", "censored",
    "proper", "redownload", "reload", "master", "leak", "nyap2p", "part",
    "parts", "disc", "cd", "full", "complete", "hevc", "h265", "h264", "x264",
    "x265", "aac", "fc2", "ppv", "omu", "tv", "range", "ver", "version", "hq",
    "raw", "new", "final", "the", "and", "real", "amateur", "jav",
    # re-encode / edit / repost markers — not names
    "fix", "fixed", "repost", "reup", "reupload", "edit", "edited", "cut",
    "uncut", "mosaic", "remux", "rip", "webrip", "sample", "preview", "trailer",
    "fullhd", "1080", "720", "2160", "upscale", "aiupscale", "enhanced",
}


def _looks_like_name(cand):
    if not cand or any(ch.isdigit() for ch in cand):
        return False
    if not re.search(r"[A-Za-z]", cand):          # Latin only (conservative)
        return False
    words = cand.split()
    if not (1 <= len(words) <= 3) or not (2 <= len(cand) <= 25):
        return False
    return not any(w.lower() in _ACTRESS_JUNK for w in words)


def extract_actress(folder_name, entries=()):
    """Best-effort romaji actress name from the folder name or a video file name.
    Returns the cleaned name (e.g. 'mizuki', 'Yuuna Himekawa') or None."""
    vids = [os.path.splitext(e)[0] for e in entries
            if os.path.splitext(e)[1].lower() in VIDEO_EXTS]
    for src in [folder_name] + vids:
        # (a) a parenthesised / bracketed name: (Yuuna Himekawa), [Reira Aisaki]
        for grp in re.findall(r"[\(\[]([^\)\]]{2,25})[\)\]]", src):
            cand = grp.strip(" -_.")
            if _looks_like_name(cand):
                return cand
        # (b) a trailing romaji token right after the code: "MIFD-220 mizuki"
        m = re.search(r"[0-9]{2,}[\s_\-]+([A-Za-z][A-Za-z ]{1,20})$", src.strip())
        if m:
            cand = m.group(1).strip(" -_.")
            if _looks_like_name(cand):
                return cand
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.1f}{unit}"
        nbytes /= 1024


class Runner:
    """Executes or simulates filesystem actions and keeps a log."""

    def __init__(self, apply):
        self.apply = apply

    def _do(self, verb, detail, fn):
        prefix = "      " + (verb if self.apply else f"[dry] {verb}")
        print(f"{prefix} {detail}")
        if self.apply:
            fn()

    def makedirs(self, path):
        self._do("mkdir       ", os.path.basename(path) + "/",
                 lambda: os.makedirs(path, exist_ok=True))

    def delete_file(self, path):
        self._do("delete file ", os.path.basename(path), lambda: os.remove(path))

    def delete_tree(self, path):
        import shutil
        self._do("delete dir  ", os.path.basename(path) + "/",
                 lambda: shutil.rmtree(path))

    def rename(self, src, dst):
        # When moving across folders, show the destination folder for clarity;
        # for an in-place rename just show the new basename.
        if os.path.dirname(src) != os.path.dirname(dst):
            shown = os.path.join(os.path.basename(os.path.dirname(dst)),
                                 os.path.basename(dst))
        else:
            shown = os.path.basename(dst)
        self._do("rename      ", f"{os.path.basename(src)}  ->  {shown}",
                 lambda: os.rename(src, dst))


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_folder(folder_path, target_base, runner, min_bytes):
    """Clean up and rename the contents of one folder, then the folder itself."""
    original_name = os.path.basename(folder_path)
    entries = sorted(os.listdir(folder_path))

    # Preserve an actress name if the original name carries one:
    #   "<CODE>"  ->  "<CODE> <Actress>"  (matching still keys off <CODE>).
    actress = extract_actress(original_name, entries)
    base = f"{target_base} {actress}" if actress else target_base
    if actress:
        print(f"      + keep actress name: {actress}")

    # Decide whether this release should be flagged for re-download BEFORE any
    # renaming strips the tell-tale "-HD" / "nyap2p" markers.
    reflag = needs_reflag(original_name, entries)
    if reflag:
        print(f"      >> FLAG: HD/nyap2p re-encode — will append "
              f"'{REFLAG_SUFFIX}'")

    # 1. Categorize the current contents.
    kept_videos = []          # (path, size) — the real movie files
    for entry in entries:
        if entry == ".DS_Store":
            entry_path = os.path.join(folder_path, entry)
            runner.delete_file(entry_path)
            continue
        entry_path = os.path.join(folder_path, entry)

        if os.path.isdir(entry_path):
            # Junk sub-directory (e.g. spam "論壇文宣"). Remove it entirely.
            runner.delete_tree(entry_path)
            continue

        ext = os.path.splitext(entry)[1].lower()
        if ext not in VIDEO_EXTS:
            # Not a movie file -> remove.
            runner.delete_file(entry_path)
            continue

        size = os.path.getsize(entry_path)
        # A small video is only spam if it does NOT reference this release's
        # number — that distinguishes ad clips (e.g. "台湾uu美少女直播") from a
        # genuine short part (e.g. "...4942041_2.mp4"), which we always keep.
        release_number = re.sub(r"\D", "", target_base)
        if size < min_bytes and release_number and release_number not in entry:
            print(f"      (small spam clip {human(size)})", end=" ")
            runner.delete_file(entry_path)
            continue

        kept_videos.append((entry_path, size))

    if not kept_videos:
        print("      !! no movie file >= threshold survived — folder left as-is")
        return False

    # 2. Rename the surviving movie file(s).
    multi = len(kept_videos) > 1
    planned = []  # (src, dst)
    for idx, (path, _size) in enumerate(kept_videos, start=1):
        ext = os.path.splitext(path)[1].lower()
        if multi:
            new_name = f"{base}-{idx}{ext}"
        else:
            new_name = f"{base}{ext}"
        planned.append((path, os.path.join(folder_path, new_name)))

    # Two-step rename to avoid clobbering if a source already matches a target.
    tmp_renames = []
    for i, (src, dst) in enumerate(planned):
        if src == dst:
            continue
        tmp = dst + f".__tmp{i}__"
        runner.rename(src, tmp)
        tmp_renames.append((tmp, dst))
    for tmp, dst in tmp_renames:
        runner.rename(tmp, dst)

    # 3. Rename the folder itself (movie files keep the clean <CODE>; only the
    #    folder carries the re-download flag).
    final_folder_name = base + (REFLAG_SUFFIX if reflag else "")
    parent = os.path.dirname(folder_path)
    new_folder_path = os.path.join(parent, final_folder_name)
    if os.path.abspath(folder_path) != os.path.abspath(new_folder_path):
        if os.path.exists(new_folder_path):
            print(f"      !! target folder '{final_folder_name}' already exists — "
                  f"folder NOT renamed")
        else:
            runner.rename(folder_path, new_folder_path)
    return True


def fold_loose_files(root, runner):
    """Move bare  <CODE>.<ext>  movie files sitting directly at the root into their own
    <CODE>/ folder, so the normal per-folder pass can process them. Also removes .DS_Store.
    (Downloads often land as loose files, not folders.)"""
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if entry == ".DS_Store":
            runner.delete_file(path)
            continue
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(entry)
        if ext.lower() not in VIDEO_EXTS:
            continue
        code = identify(stem)
        if not code:
            continue
        dest_dir = os.path.join(root, code)
        print(f"LOOSE {entry}\n      -> {code}/")
        runner.makedirs(dest_dir)
        runner.rename(path, os.path.join(dest_dir, entry))
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Normalize FC2-PPV folders and their movie files.")
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT,
                        help=f"folder to process (default: {DEFAULT_ROOT})")
    parser.add_argument("--apply", action="store_true",
                        help="actually make changes (default is a dry run)")
    parser.add_argument("--min-size", type=float, default=80.0,
                        help="delete movie files smaller than this many MB "
                             "(default: 80)")
    args = parser.parse_args()

    root = args.root
    if not os.path.isdir(root):
        sys.exit(f"error: not a directory: {root}")

    min_bytes = int(args.min_size * 1024 * 1024)
    runner = Runner(apply=args.apply)

    mode = "APPLY" if args.apply else "DRY RUN (no changes) — pass --apply to execute"
    print(f"Root : {root}")
    print(f"Mode : {mode}")
    print(f"Rule : delete non-video files and movies < {args.min_size:g} MB\n")

    fold_loose_files(root, runner)          # bare <CODE>.mp4 at root -> <CODE>/ first

    processed, skipped = [], []
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue

        code = identify(entry)
        if code is None:
            skipped.append(entry)
            print(f"SKIP  {entry}\n      (not a recognized code — left untouched)\n")
            continue

        print(f"MATCH {entry}\n      -> {code}")
        ok = process_folder(path, code, runner, min_bytes)
        (processed if ok else skipped).append(entry)
        print()

    print("=" * 60)
    print(f"Processed folders     : {len(processed)}")
    print(f"Skipped               : {len(skipped)}")
    for s in skipped:
        print(f"   - {s}")
    if not args.apply:
        print("\nThis was a DRY RUN. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    main()
