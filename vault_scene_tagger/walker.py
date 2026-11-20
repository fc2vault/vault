"""Media walking & keying (spec §7)."""
import logging
import os

log = logging.getLogger(__name__)

VIDEO_EXT = frozenset({
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".webm", ".flv",
    ".m4v", ".rmvb", ".mpg", ".mpeg",
})
# Compared lowercased, so Synology's "@eaDir" matches too.
RESERVED_DIRS = frozenset({"_vault_trash", "_duplicates", "_unsorted", "_vault", "@eadir"})

SCENES_SUFFIX = ".scenes.json"
SPRITES_JPG_SUFFIX = ".sprites.jpg"
SPRITES_VTT_SUFFIX = ".sprites.vtt"


def is_video_name(name):
    # Dotfiles, which includes "._foo.mp4" AppleDouble files.
    if name.startswith("."):
        return False
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def skip_dir(name):
    return name.startswith(".") or name.lower() in RESERVED_DIRS


def iter_videos(root):
    """Yield absolute video paths under ``root`` in a stable (sorted) order.

    ``root`` may also be a single video file.
    """
    root = os.path.abspath(root)
    if os.path.isfile(root):
        if is_video_name(os.path.basename(root)):
            yield root
        return

    def _onerror(err):
        log.warning("cannot list %s: %s", getattr(err, "filename", "?"), err.strerror or err)

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror):
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d))
        for name in sorted(filenames):
            if is_video_name(name):
                yield os.path.join(dirpath, name)


def companion_path(video, suffix):
    """``/a/b/Movie part2.mkv`` + ``.scenes.json`` -> ``/a/b/Movie part2.scenes.json``."""
    return os.path.splitext(video)[0] + suffix


def sidecar_path(video):
    return companion_path(video, SCENES_SUFFIX)
