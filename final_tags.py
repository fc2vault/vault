"""Final tags: which markers Vault shows outside training mode.

The scene model writes many markers per movie (every detection window, repeats inside one act,
some false alarms). For everyday viewing the user wants only "the first tag of each new action".
This module picks those markers at display time; it never writes anything, so the sidecar keeps
the model's full output for review and training, and the pick follows every correction.

Designed and scored on 51 hand-curated movies (builds/vault-rounds/final-set/, README there):
edits per movie to reach the user's final set went from ~15 (raw model) to ~4.7. Rules ("lean v2";
v1 had C0 0.6 / W_SPAN 0.1 and scored ~4.9),
applied to the model's markers only; markers the user made or confirmed (source "manual") are
always kept and only take part as context:

  smooth   keep the subset of AI markers that maximises sum(evidence) - LAM * (changes of label),
           evidence = (confidence - C0) + W_SPAN * log2(span / 30 s). Brief, unsure blips that
           would split one act (blowjob - missionary - blowjob) are dropped.
  intro    drop AI positions / creampies before the part's first action (movies open with an action)
  cp_pos   an AI creampie needs a position among the kept markers in the CP_POS s before it
  cp_cont  drop an AI creampie when a position follows within CP_CONT s (the sex goes on)
  runs     keep the first marker of each change of label

Stdlib only (Vault runs on the system python).
"""
import math

LAM, C0, W_SPAN, SPAN0 = 0.1, 0.65, 0.15, 30.0
CP_POS, CP_CONT = 600.0, 180.0
POSITIONS = {"missionary", "doggy", "cowgirl", "standing", "scoop-up"}
ACTIONS = {"fingering", "blowjob", "toy", "titjob"}
SKIP = {"scene", "review"}          # cuts and QC spots are never final tags
RULES = "lean-v2"


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _evidence(m):
    t, end = _num(m.get("t")), m.get("end")
    span = (_num(end) - t) if end is not None else 0.0
    return (_num(m.get("confidence")) - C0) + W_SPAN * math.log2(max(span, 1.0) / SPAN0)


def _smooth(ms):
    """Dynamic programme over 'label of the last kept marker'. Manual markers must be kept;
    creampies pass through untouched (a point event with its own rules)."""
    NEG = float("-inf")
    best = {None: (0.0, [])}
    for i, m in enumerate(ms):
        lab = m["label"]
        if lab == "creampie":
            best = {k: (v[0], v[1] + [i]) for k, v in best.items()}
            continue
        forced = m.get("source") == "manual"
        ev = 0.0 if forced else _evidence(m)
        new = {} if forced else dict(best)          # dropping is only an option for AI markers
        cand = (NEG, None)
        for last, (sc, kept) in best.items():
            s = sc + ev - (LAM if last is not None and last != lab else 0.0)
            if s > cand[0]:
                cand = (s, kept)
        if cand[1] is not None and cand[0] > new.get(lab, (NEG,))[0]:
            new[lab] = (cand[0], cand[1] + [i])
        best = new
    return [ms[i] for i in max(best.values(), key=lambda v: v[0])[1]]


def pick(markers):
    """Return the ids (positions in `markers`) of the final tags of ONE movie part.

    `markers`: dicts with t, label, source and, for AI markers, end and confidence.
    """
    ms = []
    for i, m in enumerate(markers):
        if not isinstance(m, dict) or m.get("label") in SKIP or m.get("t") is None:
            continue
        ms.append(dict(m, _i=i, t=_num(m.get("t"))))
    ms.sort(key=lambda m: m["t"])
    ai = lambda m: m.get("source") != "manual"

    ms = _smooth(ms)
    first_act = next((m["t"] for m in ms if m["label"] in ACTIONS), None)
    if first_act is not None:
        ms = [m for m in ms if not ai(m) or m["t"] >= first_act or m["label"] in ACTIONS]
    kept = []
    for m in ms:
        if ai(m) and m["label"] == "creampie" and not any(
                x["label"] in POSITIONS and m["t"] - x["t"] <= CP_POS for x in kept):
            continue
        kept.append(m)
    ms = [m for m in kept if not (ai(m) and m["label"] == "creampie" and any(
        x["label"] in POSITIONS and 0 < x["t"] - m["t"] <= CP_CONT for x in kept))]
    out = []
    for m in ms:
        if not out or out[-1]["label"] != m["label"]:
            out.append(m)
    return [m["_i"] for m in out]
