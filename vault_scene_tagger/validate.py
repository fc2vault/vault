"""Validate sidecars against SCENES_SPEC.md §3.

    python3 -m vault_scene_tagger.validate FILE_OR_DIR [...]
"""
import os
import sys

from . import sidecar
from .walker import SCENES_SUFFIX, skip_dir


def _files(target):
    if os.path.isdir(target):
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if not skip_dir(d))
            for name in sorted(filenames):
                if name.endswith(SCENES_SUFFIX) and not name.startswith("."):
                    yield os.path.join(dirpath, name)
    else:
        yield target


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    bad = checked = 0
    for target in argv:
        for path in _files(target):
            checked += 1
            try:
                doc = sidecar.read(path)
                problems = sidecar.validate(doc) if doc is not None else ["file not found"]
            except sidecar.SidecarError as e:
                problems = [str(e)]
            if problems:
                bad += 1
                print("INVALID %s" % path)
                for p in problems:
                    print("  - %s" % p)
    print("%d checked, %d invalid" % (checked, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
