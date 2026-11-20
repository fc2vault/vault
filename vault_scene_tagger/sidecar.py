"""Read / merge / validate / atomically write ``*.scenes.json`` (spec §3 and §5)."""
import json
import math
import os
import re
import threading
from datetime import datetime, timezone

from . import GENERATOR, NAME

SCHEMA = "vault-scenes/1"
SOURCES = ("cut", "heuristic", "ml", "manual")
# Sources this tool owns and may replace. "manual" is never generated or overwritten.
GENERATED_SOURCES = frozenset({"cut", "heuristic", "ml"})
REQUIRED_KEYS = ("schema", "video", "duration", "generator", "generated_at")
# Extra top-level field (allowed by §3.2) recording which phases ran, for resume.
STATE_KEY = "tagger"

LABEL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]00:?00)$")


class SidecarError(Exception):
    """The sidecar exists but cannot be read/parsed. Never overwrite such a file."""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def read(path):
    """Return the parsed sidecar dict, or None if it does not exist."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SidecarError("cannot read %s: %s" % (path, e))
    if raw.startswith(b"\xef\xbb\xbf"):  # tolerate a BOM written by someone else
        raw = raw[3:]
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SidecarError("invalid JSON in %s: %s" % (path, e))
    if not isinstance(doc, dict):
        raise SidecarError("%s: top level is not a JSON object" % path)
    return doc


def _sort_key(m):
    t = m.get("t") if isinstance(m, dict) else None
    t = float(t) if _num(t) else 0.0
    end = m.get("end") if isinstance(m, dict) else None
    end = float(end) if _num(end) else t
    label = m.get("label") if isinstance(m, dict) else None
    return (t, end, label if isinstance(label, str) else "")


def sort_markers(markers):
    return sorted(markers, key=_sort_key)


def phases_done(doc):
    """Which phases a previous run of this tool recorded in ``doc``."""
    if not isinstance(doc, dict):
        return set()
    state = doc.get(STATE_KEY)
    if isinstance(state, dict) and isinstance(state.get("phases"), list):
        return {p for p in state["phases"] if isinstance(p, str)}
    # A reader may round-trip the file without our extra field; Phase A is always the
    # first thing this tool writes, so our generator string implies it ran.
    gen = doc.get("generator")
    if isinstance(gen, str) and gen.startswith(NAME + " "):
        return {"a"}
    return set()


def engines_done(doc):
    """Phase-C engines recorded in a sidecar (``tagger.params.c.engines``)."""
    state = doc.get(STATE_KEY) if isinstance(doc, dict) else None
    params = state.get("params") if isinstance(state, dict) else None
    c = params.get("c") if isinstance(params, dict) else None
    engines = c.get("engines") if isinstance(c, dict) else None
    return {e for e in engines if isinstance(e, str)} if isinstance(engines, list) else set()


def merge(existing, new_markers, owned_sources, video_name, duration, phases, params,
          drop_phases=()):
    """Build the new sidecar document.

    Keeps every marker whose source is not in ``owned_sources`` (always including
    ``manual``), replaces the owned ones with ``new_markers``, keeps unknown top-level
    fields, and records the phases run (minus ``drop_phases`` whose output was removed).
    """
    owned = set(owned_sources) & GENERATED_SOURCES
    existing = existing if isinstance(existing, dict) else {}

    old = existing.get("markers")
    kept = []
    if isinstance(old, list):
        kept = [m for m in old if not (isinstance(m, dict) and m.get("source") in owned)]

    state = existing.get(STATE_KEY)
    state = dict(state) if isinstance(state, dict) else {}
    done = (phases_done(existing) - set(drop_phases)) | set(phases)
    state["phases"] = sorted(done)
    prev_params = state.get("params") if isinstance(state.get("params"), dict) else {}
    state["params"] = dict(prev_params, **params)

    doc = {
        "schema": SCHEMA,
        "video": video_name,
        "duration": round(float(duration), 3),
        "generator": GENERATOR,
        "generated_at": utc_now(),
    }
    for k, v in existing.items():
        if k not in doc and k not in ("markers", STATE_KEY):
            doc[k] = v
    doc[STATE_KEY] = state
    doc["markers"] = sort_markers(kept + list(new_markers))
    return doc


def validate(doc):
    """Return a list of human-readable problems; empty means valid per §3."""
    if not isinstance(doc, dict):
        return ["top level must be a JSON object"]
    errs = []
    if doc.get("schema") != SCHEMA:
        errs.append("schema must be %r" % SCHEMA)
    for k in ("video", "generator", "generated_at"):
        if not isinstance(doc.get(k), str) or not doc.get(k):
            errs.append("%s must be a non-empty string" % k)
    if isinstance(doc.get("generated_at"), str) and doc["generated_at"] \
            and not ISO_UTC_RE.match(doc["generated_at"]):
        errs.append("generated_at must be ISO-8601 UTC")
    if not _num(doc.get("duration")) or doc["duration"] < 0:
        errs.append("duration must be a number >= 0")

    markers = doc.get("markers")
    if not isinstance(markers, list):
        errs.append("markers must be an array")
        return errs
    prev_t = None
    for i, m in enumerate(markers):
        where = "markers[%d]" % i
        if not isinstance(m, dict):
            errs.append("%s must be an object" % where)
            continue
        t = m.get("t")
        if not _num(t) or t < 0:
            errs.append("%s.t must be a number >= 0" % where)
            t = None
        if "end" in m:
            end = m["end"]
            if not _num(end):
                errs.append("%s.end must be a number" % where)
            elif t is not None and end <= t:
                errs.append("%s.end must be > t" % where)
        label = m.get("label")
        if not isinstance(label, str) or not LABEL_RE.match(label):
            errs.append("%s.label must be lowercase-kebab" % where)
        if "confidence" in m:
            c = m["confidence"]
            if not _num(c) or not 0.0 <= c <= 1.0:
                errs.append("%s.confidence must be within 0.0-1.0" % where)
        if m.get("source") not in SOURCES:
            errs.append("%s.source must be one of %s" % (where, ", ".join(SOURCES)))
        if t is not None:
            if prev_t is not None and t < prev_t:
                errs.append("%s is out of order (markers must be sorted by t)" % where)
            prev_t = t
    return errs


def temp_path(final, ext=""):
    """A hidden temp file next to ``final`` (same directory => same filesystem => atomic rename)."""
    d, base = os.path.split(final)
    return os.path.join(d, ".%s.tmp-%d-%d%s" % (base, os.getpid(), threading.get_ident(), ext))


def discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_bytes_atomic(path, data):
    tmp = temp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        discard(tmp)
        raise


def dumps(doc):
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_atomic(path, doc):
    """UTF-8 (no BOM), temp file + rename so a reader never sees a half-written file."""
    write_bytes_atomic(path, dumps(doc))
