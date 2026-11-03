# Vault scene sidecar spec — `vault-scenes/1`

Canonical, versioned contract between `vault-scene-tagger` (writer) and any reader such as the
Vault media server. Copied verbatim from the project brief §3–§5; keep this file identical in both
repos. A machine-readable JSON Schema lives in [`examples/scenes.schema.json`](examples/scenes.schema.json).

---

## 3. THE CONTRACT — sidecar file format (implement exactly)

### 3.1 Location & naming
- One sidecar **per video file**, written **in the same directory as that video**.
- Name: the video's filename **with its extension replaced by `.scenes.json`**.
  - `FC2-PPV-1234567.mp4` → `FC2-PPV-1234567.scenes.json`
  - `Some Movie part2.mkv` → `Some Movie part2.scenes.json`
- Timestamps in a sidecar are **relative to that one video file** (important for multi-part
  movies, where each part gets its own sidecar).

### 3.2 JSON shape

```json
{
  "schema": "vault-scenes/1",
  "video": "FC2-PPV-1234567.mp4",
  "duration": 3187.4,
  "generator": "vault-scene-tagger 0.3.1",
  "generated_at": "2026-09-14T10:00:00Z",
  "markers": [
    { "t": 12.0,  "label": "scene",   "source": "cut" },
    { "t": 73.5,  "end": 210.0, "label": "blowjob", "confidence": 0.82, "source": "ml" },
    { "t": 210.0, "end": 540.0, "label": "doggy",   "confidence": 0.71, "source": "ml" },
    { "t": 545.0, "end": 560.0, "label": "cumshot", "confidence": 0.90, "source": "ml" }
  ]
}
```

Top-level fields:
| field | req | meaning |
|------|-----|---------|
| `schema` | ✓ | literal `"vault-scenes/1"`. Bump the number on breaking changes. |
| `video` | ✓ | basename of the annotated file (sanity check for readers). |
| `duration` | ✓ | seconds (float), from ffprobe. |
| `generator` | ✓ | `"name version"`. |
| `generated_at` | ✓ | ISO-8601 UTC. |
| `markers` | ✓ | array, sorted by `t` ascending. May be empty. |

Marker object:
| field | req | meaning |
|------|-----|---------|
| `t` | ✓ | start time, seconds (float ≥ 0). |
| `end` | – | end time, seconds. Omit for an instantaneous marker (e.g. a raw cut, a cumshot). If present, `end > t`. |
| `label` | ✓ | lowercase-kebab, from the vocabulary in §4 (unknown labels are allowed; the reader shows them verbatim). |
| `confidence` | – | 0.0–1.0. Omit for deterministic/manual markers. |
| `source` | ✓ | one of `"cut"`, `"heuristic"`, `"ml"`, `"manual"` (see §5). |

Rules:
- **Sorted by `t`.** Overlaps are allowed (two things at once) but keep it sane.
- Unknown top-level fields must be ignored by readers — you may add extras, but don't rely on them.
- Write atomically (temp file + rename) so a reader never sees a half-written file.
- UTF-8, no BOM.

### 3.3 Optional companion: scrub-thumbnail sprites (NICE-TO-HAVE, not required)
The reader (Vault) generates its own scrub-bar thumbnails, so you do **not** need to. But since
you're already decoding frames, you MAY additionally emit a standard sprite sheet the reader will
prefer if present:
- `<basename>.sprites.jpg` — a grid of small frames (e.g. 160×90) sampled every N seconds.
- `<basename>.sprites.vtt` — WebVTT thumbnail cues mapping `time → sprites.jpg#xywh=X,Y,W,H`.
Only emit both or neither. If you skip this, the reader falls back to generating its own.

---

## 4. Label vocabulary

Lowercase-kebab. Readers may group by the categories below for coloring. **Unknown labels are
allowed** (a reader shows them raw), but prefer these so different installs agree:

- **structural:** `scene` (a raw shot/cut boundary — the Phase-A output), `intro`, `outro`, `talk`
- **state:** `clothed`, `undressing`, `topless`, `nude`
- **act:** `kissing`, `foreplay`, `fingering`, `handjob`, `blowjob`, `deepthroat`, `titjob`,
  `cunnilingus`, `69`, `sex`, `anal`, `masturbation`, `toy`, `facial`, `cumshot`, `creampie`
- **position:** `missionary`, `doggy`, `cowgirl`, `reverse-cowgirl`, `standing`, `spooning`, `sideways`

Notes:
- `scene` markers (Phase A) are just boundaries — instantaneous, no `end`, `source:"cut"`.
- An act and a position can co-occur (e.g. `sex` + `doggy`); emit both as separate ranged markers.
- Keep the set small and honest; don't invent hyper-specific labels the model can't reliably hit.

---

## 5. Source ownership & merge rule (so re-runs don't clobber edits)

The sidecar is a **shared, merged source of truth**. The reader (Vault) lets users add/correct
markers and writes them **back into the same sidecar** with `source:"manual"`. Therefore:

- On (re)generation you **must preserve every marker with `source:"manual"`** and merge your
  output around them.
- You **own and may freely replace** markers whose `source` is `"cut"`, `"heuristic"`, or `"ml"`
  (i.e. delete your previous ones and write the new ones; leave `manual` untouched).
- If a sidecar doesn't exist yet, create it. If it exists, read → keep `manual` → replace yours →
  write atomically.
- Never delete a sidecar.

`source` values:
- `"cut"` — deterministic shot-change boundary (ffmpeg). No confidence.
- `"heuristic"` — rule-based (e.g. nudity-detector state transition). Confidence optional.
- `"ml"` — a classifier/vision model prediction. Include `confidence`.
- `"manual"` — written by a human via the reader. **Never generated by this tool; never overwritten.**

---

## Implementation notes (writer: vault-scene-tagger)

Not part of the contract. Readers must not depend on them.

- The tagger adds an extra top-level field `"tagger": {"phases": [...], "params": {...}}` to
  remember which phases ran. Readers ignore it (§3.2). If a reader drops it when writing back,
  the tagger falls back to its `generator` string.
- A re-run replaces only the sources owned by the phases it runs (Phase A → `cut`,
  Phase B → `heuristic`, Phase C → `ml`), so `--phase a` never removes Phase C output. Markers
  with any other `source` value (including unknown ones) are kept as-is.
- A sidecar that exists but can't be parsed is never overwritten. It's reported as an error.
- **User feedback (written by Vault, used for training and by the tagger):** markers are edited
  as start points. When the user confirms, moves or relabels a marker it becomes `source: "manual"` without
  `end`/`confidence`. If it was an AI marker, its original fields are kept under a marker extra
  `"was": {"t", "end", "label", "source", "confidence"}`. When the user deletes a non-manual marker
  it is appended to a top-level extra `"rejected": [{…marker…, "rejected_at": ISO-8601}]`. The tagger
  doesn't re-suggest a moment with the same label within 10 s of a `rejected` entry or a `was`.
- The sidecar is re-read right before writing, so manual markers added while ffmpeg was running
  are kept.
- Temp files are hidden dotfiles in the same directory (`.<name>.tmp-…`), renamed into place.
