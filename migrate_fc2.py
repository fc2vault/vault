#!/usr/bin/env python3
"""
migrate_fc2.py — Step 2 of the spare cleanup pipeline (direct migrate + dedupe).

Pipeline (each step gated by the operator):
  Step 1  rename_fc2.py --apply     normalize/clean `completed/spare`
  Step 2  migrate_fc2.py --apply    migrate `completed/spare` -> `1_FC2 PPV`,
                                     deduping against the library (bigger wins)

The old `3_Sort_FC2` staging hop was removed — it was a pass-through that just
doubled the cross-share copies. `3_Sort_FC2` still exists on disk for manual use;
it is no longer part of the pipeline. Use `--sweep <root>` to tidy leftover
`.smbdelete` orphan folders there or anywhere.

Comparison basis: TOTAL folder size (sum of all file bytes, recursive).
Matching basis  : canonical release code via rename_fc2.identify()
                  (so `FC2PPV-x` matches `FC2-PPV-x`).
Tolerance       : sizes within --tolerance percent (default 1%) count as EQUAL —
                  same content, keep the library copy, delete the source.

PER-RELEASE DECISION  (source = completed/spare, target = 1_FC2 PPV library)
  - not in library            -> MOVE   source into the library (an ADD)
  - within tolerance / source <= library -> DELETE SOURCE (dedupe; keep library)
  - source BIGGER than library -> REPLACE TARGET (delete library copy, move
                                  source in)  ** deletes in the library **

HARD RULE: any delete inside `1_FC2 PPV` (i.e. every REPLACE TARGET) requires
explicit operator confirmation. Without --confirm-lib-deletes those rows are
DEFERRED (source left in place) and listed. The Mover also refuses, at the
filesystem layer, to delete under the library unless that flag is set.

SAFETY: DRY RUN by default. Nothing changes until you pass --apply.
Moves within the same SMB share are instant renames; cross-share are copy+delete.
Run --apply in the background for large batches.

Usage:
    python3 migrate_fc2.py                       # preview migrate -> library
    python3 migrate_fc2.py --apply               # execute (moves + source deletes)
    python3 migrate_fc2.py --apply --confirm-lib-deletes   # also do REPLACE rows
    python3 migrate_fc2.py --sweep "/path/to/staging"   # tidy orphans
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rename_fc2 import identify, human  # noqa: E402

# Paths default to the author's layout but are overridable via env (used by the Vault UI's
# Import feature, which passes the user's configured download + library folders).
COMPLETED_SPARE = os.environ.get("VAULT_IMPORT_SRC", "/path/to/downloads")
FC2_PPV = os.environ.get("VAULT_LIBRARY", "/path/to/library")  # library — deletes gated
SORT_FC2 = "/path/to/staging"        # legacy staging, kept for --sweep only

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def color(s, c):
    return f"{c}{s}{RESET}" if sys.stdout.isatty() else s


def under(path, root):
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def within_tol(a, b, tol_pct):
    hi = max(a, b)
    return True if hi == 0 else abs(a - b) / hi <= (tol_pct / 100.0)


def is_orphan(path):
    """A folder that holds only SMB `.smbdelete*` pending-delete markers (or is
    empty) — the residue of a delete the SMB client hasn't released yet."""
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    return all(e.startswith(".smbdelete") or e == ".DS_Store" for e in entries)


def build_index(root):
    """Map canonical code -> actual folder path. Handles both a flat library
    (<CODE>) and one foldered by actress (<Actress>/<CODE>) — a non-code dir is
    treated as an actress folder and scanned one level deeper."""
    index = {}

    def add(code, path):
        if code in index:
            print(color(f"  !! duplicate code in {os.path.basename(root)}: {code}  "
                        f"({os.path.basename(index[code])} vs {os.path.basename(path)})",
                        RED))
        index[code] = path

    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        code = identify(entry)
        if code:
            add(code, path)
            continue
        try:
            children = sorted(os.listdir(path))       # actress folder -> descend
        except OSError:
            continue
        for sub in children:
            sp = os.path.join(path, sub)
            if os.path.isdir(sp):
                c = identify(sub)
                if c:
                    add(c, sp)
    return index


def _remove_dir(path):
    """Remove a folder, tolerating SMB `.smbdelete*` markers ('directory not
    empty' / 'resource busy'). Returns True if fully gone."""
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        pass
    try:
        for f in os.listdir(path):
            try:
                os.remove(os.path.join(path, f))
            except OSError:
                pass
        os.rmdir(path)
        return True
    except OSError as e:
        print(color(f"      !! not fully removed (SMB lock?): {path} ({e})", RED))
        return False


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------

def sweep_orphans(root, apply, label="SWEEP"):
    """Remove orphan `.smbdelete`-only / empty subfolders under root. NEVER call
    with root inside the library without confirmation — callers guard that."""
    removed, stuck = [], []
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        if is_orphan(path):
            print(f"      {label if apply else '[dry] ' + label} orphan {entry}/")
            if apply:
                (removed if _remove_dir(path) else stuck).append(entry)
            else:
                removed.append(entry)
    if not removed and not stuck:
        print("      (no orphans)")
    return removed, stuck


# ---------------------------------------------------------------------------
# Step 2 — direct migrate completed/spare -> 1_FC2 PPV
# ---------------------------------------------------------------------------

def migrate(apply, tol, limit, confirm_lib):
    print(f"STEP 2  MIGRATE  {COMPLETED_SPARE}  ->  {FC2_PPV}")
    print(f"Mode : {'APPLY' if apply else 'DRY RUN'}   (bigger wins; "
          f"equal within {tol:g}% -> delete source)")
    print(f"Library deletes (REPLACE TARGET): "
          f"{'CONFIRMED — will execute' if confirm_lib else 'require --confirm-lib-deletes'}\n")
    for p in (COMPLETED_SPARE, FC2_PPV):
        if not os.path.isdir(p):
            sys.exit(f"error: not a directory: {p}")

    print("Orphan sweep of source:")
    sweep_orphans(COMPLETED_SPARE, apply)
    print()

    lib = build_index(FC2_PPV)

    rows = []          # (code, entry, ssize, tsize, match, action)
    counts = {"MOVE": 0, "DELETE SOURCE": 0, "REPLACE TARGET": 0}
    b_move = b_del_src = b_replace = 0
    skipped = []
    for entry in sorted(os.listdir(COMPLETED_SPARE)):
        src = os.path.join(COMPLETED_SPARE, entry)
        if not os.path.isdir(src):
            continue
        code = identify(entry)
        if code is None:
            skipped.append(entry)
            continue
        ssize = folder_size(src)
        match = lib.get(code)
        if match is None:
            action, tsize = "MOVE", None
            b_move += ssize
        else:
            tsize = folder_size(match)
            if within_tol(ssize, tsize, tol) or ssize <= tsize:
                action = "DELETE SOURCE"
                b_del_src += ssize
            else:
                action = "REPLACE TARGET"
                b_replace += ssize
        counts[action] += 1
        rows.append((code, entry, ssize, tsize, match, action))

    # Table
    print(f"{'CODE':<22}  {'SRC SIZE':>11}  {'LIB SIZE':>11}  ACTION")
    print("-" * 74)
    for code, entry, ssize, tsize, _m, action in (rows[:limit] if limit else rows):
        ts = human(tsize) if tsize is not None else "-"
        if action == "MOVE":
            acol = color("MOVE (add)", DIM)
        elif action == "DELETE SOURCE":
            acol = color("DELETE SOURCE", RED)
        else:
            note = "" if confirm_lib else color("  [needs --confirm]", YELLOW)
            acol = color("REPLACE TARGET", GREEN) + note
        print(f"{code:<22}  {human(ssize):>11}  {ts:>11}  {acol}")
    if limit and len(rows) > limit:
        print(f"... and {len(rows) - limit} more rows")

    print("\n" + "=" * 74)
    print(f"MOVE (add to library)   : {counts['MOVE']:>3}   ({human(b_move)} to copy)")
    print(color(f"REPLACE TARGET (lib del): {counts['REPLACE TARGET']:>3}   "
                f"(source bigger; {human(b_replace)} in) "
                f"{'' if confirm_lib else '-- DEFERRED, needs --confirm-lib-deletes'}",
                GREEN if confirm_lib else YELLOW))
    print(color(f"DELETE SOURCE           : {counts['DELETE SOURCE']:>3}   "
                f"(library as good/bigger; {human(b_del_src)} source freed)", RED))
    if skipped:
        print(f"Skipped (unrecognized, left in place): {len(skipped)}")
        for s in skipped:
            print(f"   - {s}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to execute.")
        return

    print("\n--- executing ---")
    failures, deferred = [], []
    in_library = []        # codes that end up present in the library, for the catalog
    for code, entry, ssize, tsize, match, action in rows:
        src = os.path.join(COMPLETED_SPARE, entry)
        dst = os.path.join(FC2_PPV, entry)
        try:
            if action == "MOVE":
                _move(src, dst)
                in_library.append(code)
            elif action == "DELETE SOURCE":
                print(f"      DELETE {entry}/  (library as good/bigger)")
                _remove_dir(src)
                in_library.append(code)            # the library copy is the keeper
            elif action == "REPLACE TARGET":
                if not confirm_lib:
                    print(color(f"      DEFERRED {entry} — library delete needs "
                                f"--confirm-lib-deletes", YELLOW))
                    deferred.append(entry)
                    continue
                _lib_delete(match)                 # gated hard-delete in library
                # keep the replacement where the old copy lived (e.g. inside its
                # actress folder if the library is foldered), else the flat root.
                _move(src, os.path.join(os.path.dirname(match), entry))
                in_library.append(code)
        except Exception as e:
            print(color(f"      !! FAILED {entry}: {e}", RED))
            failures.append((entry, str(e)))

    mark_available(in_library)      # only codes whose action actually succeeded

    if deferred:
        print(color(f"\n{len(deferred)} REPLACE TARGET row(s) DEFERRED (library "
                    f"deletes) — confirm, then re-run with --confirm-lib-deletes:", YELLOW))
        for d in deferred:
            print(f"   - {d}")
    if failures:
        print(color(f"\n{len(failures)} FAILURE(S) — review manually:", RED))
        for entry, e in failures:
            print(f"   - {entry}: {e}")
    if not deferred and not failures:
        print("\nAll actions completed with no errors.")


def _catalog_db():
    """Path to the Vault catalog, or None when it isn't there (this script has always
    worked standalone, so a missing catalog must stay non-fatal)."""
    p = os.environ.get("VAULT_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "db", "catalog.db")
    return p if os.path.isfile(p) else None


def mark_available(codes):
    """Flip catalog rows for codes now present in the library from 'missing' to
    'available'. Without this a wishlist entry stays a ghost in the Vault UI even
    after the movie has been migrated in."""
    codes = sorted(set(c for c in codes if c))
    if not codes:
        return
    db = _catalog_db()
    if not db:
        print(color("      (no catalog.db found — skipped availability update)", DIM))
        return
    try:
        import sqlite3
        con = sqlite3.connect(db, timeout=30)
        cur = con.execute(
            "SELECT COUNT(*) FROM works WHERE availability!='available' AND code IN "
            "(%s)" % ",".join("?" * len(codes)), codes)
        stale = cur.fetchone()[0]
        con.executemany(
            "UPDATE works SET availability='available', in_library=1 WHERE code=?",
            [(c,) for c in codes])
        con.commit()
        con.close()
        print(f"      catalog: {stale} work(s) marked available "
              f"({len(codes)} code(s) checked)")
    except Exception as e:
        print(color(f"      !! catalog availability update failed: {e}", YELLOW))


def _move(src, dst):
    print(f"      MOVE  {os.path.basename(src)}  ->  1_FC2 PPV/{os.path.basename(dst)}")
    if os.path.exists(dst):
        print(color(f"      !! dest exists, skipping: {dst}", RED))
        return
    try:
        os.rename(src, dst)                # fast path (same share)
        return
    except OSError:
        pass
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    _remove_dir(src)


def _lib_delete(path):
    """Delete inside the library — only reached when --confirm-lib-deletes is set;
    hard-guarded so it can never fire without confirmation."""
    if not under(path, FC2_PPV):
        raise RuntimeError(f"_lib_delete called off-library: {path}")
    print(color(f"      DELETE (library) {os.path.basename(path)}/  "
                f"(replaced by bigger source)", GREEN))
    _remove_dir(path)


def main():
    ap = argparse.ArgumentParser(description="Step 2: migrate completed/spare -> library.")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--confirm-lib-deletes", action="store_true",
                    help="authorize REPLACE TARGET rows (deletes inside 1_FC2 PPV)")
    ap.add_argument("--tolerance", type=float, default=1.0,
                    help="percent size difference treated as EQUAL (default 1)")
    ap.add_argument("--limit", type=int, default=0, help="cap preview rows (0=all)")
    ap.add_argument("--sweep", metavar="ROOT",
                    help="sweep-only: remove orphan .smbdelete folders under ROOT")
    args = ap.parse_args()

    if args.sweep:
        if under(args.sweep, FC2_PPV) and not args.confirm_lib_deletes:
            sys.exit("refusing to sweep inside the library without "
                     "--confirm-lib-deletes")
        print(f"SWEEP  {args.sweep}   Mode: {'APPLY' if args.apply else 'DRY RUN'}")
        rem, stuck = sweep_orphans(args.sweep, args.apply)
        print(f"\nRemoved: {len(rem)}   Stuck (SMB lock, retry later): {len(stuck)}")
        return

    migrate(args.apply, args.tolerance, args.limit, args.confirm_lib_deletes)


if __name__ == "__main__":
    main()
