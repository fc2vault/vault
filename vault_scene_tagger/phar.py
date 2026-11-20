"""Phase-C engine: P-HAR's TimeSformer (RGB stream) as a local video classifier.

P-HAR (https://github.com/rlleshi/phar, Apache-2.0) ships a 2022 MMAction2 checkpoint. Its 249
tensors map 1:1 onto ``transformers``' TimesformerForVideoClassification, so we run it with plain
PyTorch — no MMAction2/mmcv, and the same code works on CPU, Apple MPS and CUDA.

Inference follows the repo's val_pipeline: 16 frames spanning ~6.4 s, short side 256, center crop
224, normalized with mean/std 127.5. Frames are streamed from one ffmpeg pass (sampled at
2.5 fps, the rate the model was trained at), so memory stays flat on a 3-hour movie.

Two kinds of weights load into the same engine:

- the original MMAction2 checkpoint (``timeSformer.pth``, P-HAR's 17 classes, mapped to Vault labels);
- a fine-tuned model directory (``models/vault-r1/`` ...): Hugging Face ``save_pretrained`` output
  plus ``labels.json``, trained on Vault's reviewed markers, whose classes already are Vault labels.

Without ``--phar-model`` the newest ``models/vault-r<N>/`` wins, else ``models/timeSformer.pth``.
Weights are NOT bundled: download ``timeSformer.pth`` from the project's release v1.0.0.
"""
import collections
import json
import logging
import os
import re
import sys
import subprocess

log = logging.getLogger(__name__)

# resources/annotations/annotations.txt of rlleshi/phar — order defines the label index
CLASSES = ["kissing", "fondling", "handjob", "fingering", "titjob", "blowjob", "cunnilingus",
           "deepthroat", "doggy", "the-snake", "anal", "missionary", "cowgirl", "scoop-up",
           "cumshot", "facial-cumshot", "69"]
CLIP_LEN = 16
SIZE = 224
SAMPLE_FPS = 2.5                      # 30 fps / frame_interval 12, as trained
WINDOW_SECONDS = CLIP_LEN / SAMPLE_FPS  # 6.4 s
# Classes dropped from Vault's label set (2026-09-25) — never emit them. "anal" also fired on
# rear-entry positions (38 of 275 windows in a movie without any anal) and scored 0 of 5 in review.
SUPPRESS_DEFAULT = ("anal", "facial-cumshot", "cunnilingus", "handjob", "69")
FRAME_BYTES = SIZE * SIZE * 3
# A fine-tuned model's "no act here" class: never a marker.
FINETUNED_SUPPRESS = ("background",)
_ROUND_RE = re.compile(r"^vault-r(\d+)$")


class PharError(Exception):
    pass


def user_models_dir():
    """Where the Vault app puts the scene model it downloads for Pro users (Settings → License),
    per user and per platform. The same function lives in Vault's serve.py; keep them identical."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Vault")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Vault")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                            "vault")
    return os.path.join(base, "models")


def _rounds_in(models_dir):
    """[(round number, path)] of every fine-tuned models/vault-r<N>/ that holds a config.json."""
    out = []
    try:
        for name in os.listdir(models_dir):
            m = _ROUND_RE.match(name)
            if m and os.path.isfile(os.path.join(models_dir, name, "config.json")):
                out.append((int(m.group(1)), os.path.join(models_dir, name)))
    except OSError:
        pass
    return out


def default_checkpoint(models_dir=None):
    """$VAULT_PHAR_MODEL / $VAULT_PHAR_CHECKPOINT, else the newest vault-r<N>/ that holds a
    config.json — looked for in this project's models/ and in the folder the Vault app downloads
    models to — else models/timeSformer.pth."""
    env = os.environ.get("VAULT_PHAR_MODEL") or os.environ.get("VAULT_PHAR_CHECKPOINT")
    if env:
        return env
    if models_dir is None:
        models_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
    rounds = _rounds_in(models_dir) + _rounds_in(user_models_dir())
    if rounds:
        return max(rounds)[1]
    return os.path.join(models_dir, "timeSformer.pth")


def model_name(path):
    """Provenance tag for markers: "phar" for the original weights, "phar:<dir>" for a fine-tune."""
    if path and os.path.isdir(path):
        return "phar:" + os.path.basename(os.path.normpath(path))
    return "phar"


def convert_state_dict(sd):
    """MMAction2 TimeSformer state dict -> transformers Timesformer parameter names."""
    top = {"backbone.cls_token": "timesformer.embeddings.cls_token",
           "backbone.pos_embed": "timesformer.embeddings.position_embeddings",
           "backbone.time_embed": "timesformer.embeddings.time_embeddings",
           "backbone.patch_embed.projection.weight": "timesformer.embeddings.patch_embeddings.projection.weight",
           "backbone.patch_embed.projection.bias": "timesformer.embeddings.patch_embeddings.projection.bias",
           "backbone.norm.weight": "timesformer.layernorm.weight",
           "backbone.norm.bias": "timesformer.layernorm.bias",
           "cls_head.fc_cls.weight": "classifier.weight",
           "cls_head.fc_cls.bias": "classifier.bias"}
    # attentions.0 = temporal (pre-norm), attentions.1 = spatial, ffns.0 = MLP
    per_layer = {"attentions.0.norm": "temporal_layernorm",
                 "attentions.0.attn.in_proj_weight": "temporal_attention.attention.qkv.weight",
                 "attentions.0.attn.in_proj_bias": "temporal_attention.attention.qkv.bias",
                 "attentions.0.attn.out_proj": "temporal_attention.output.dense",
                 "attentions.0.temporal_fc": "temporal_dense",
                 "attentions.1.norm": "layernorm_before",
                 "attentions.1.attn.in_proj_weight": "attention.attention.qkv.weight",
                 "attentions.1.attn.in_proj_bias": "attention.attention.qkv.bias",
                 "attentions.1.attn.out_proj": "attention.output.dense",
                 "ffns.0.norm": "layernorm_after",
                 "ffns.0.layers.0.0": "intermediate.dense",
                 "ffns.0.layers.1": "output.dense"}
    out = {}
    for k, v in sd.items():
        if k in top:
            out[top[k]] = v
            continue
        prefix = "backbone.transformer_layers.layers."
        if not k.startswith(prefix):
            raise PharError("unexpected key in checkpoint: %s" % k)
        idx, tail = k[len(prefix):].split(".", 1)
        for src, dst in per_layer.items():
            if tail == src:
                out["timesformer.encoder.layer.%s.%s" % (idx, dst)] = v
                break
            if tail.startswith(src + "."):
                out["timesformer.encoder.layer.%s.%s.%s" % (idx, dst, tail[len(src) + 1:])] = v
                break
        else:
            raise PharError("unexpected key in checkpoint: %s" % k)
    return out


def pick_device(requested=None):
    import torch
    if requested and requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class PharEngine:
    """Lazily loaded model; reused across videos in a run."""

    def __init__(self, checkpoint=None, device=None, batch=4, stride=8, threshold=0.35,
                 suppress=SUPPRESS_DEFAULT, ffmpeg="ffmpeg", keep_scores=False):
        self.checkpoint = checkpoint or default_checkpoint()
        self.name = model_name(self.checkpoint)      # recorded as each marker's "engine"
        self.classes = list(CLASSES)                 # replaced by a fine-tuned model's own
        self.device_req = device
        self.batch = max(1, int(batch))
        self.stride = max(1, int(stride))          # sampled frames between windows (8 = 3.2 s)
        self.threshold = float(threshold)
        self.suppress = {s.strip().lower() for s in (suppress or ()) if s.strip()}
        self.ffmpeg = ffmpeg
        # keep every window's full probability vector (all classes), not only the accepted top-1
        # spans: the raw track for finding where one action gives way to the next
        self.keep_scores = bool(keep_scores)
        self._model = None
        self._torch = None
        self.device = None

    def load(self):
        if self._model is not None:
            return
        try:
            import torch
            from transformers import TimesformerConfig, TimesformerForVideoClassification
        except ImportError as e:
            raise PharError(
                "the phar engine needs torch + transformers, which the interpreter running this "
                "tool doesn't have (%s).\n  In Vault: Settings → Scene model → Set up installs them.\n"
                "  Otherwise: pip install torch transformers numpy" % e)
        if os.path.isdir(self.checkpoint):
            model = self._load_finetuned(TimesformerForVideoClassification)
        elif os.path.isfile(self.checkpoint):
            cfg = TimesformerConfig(image_size=SIZE, patch_size=16, num_frames=CLIP_LEN, num_channels=3,
                                    hidden_size=768, num_hidden_layers=12, num_attention_heads=12,
                                    intermediate_size=3072, attention_type="divided_space_time",
                                    layer_norm_eps=1e-6, num_labels=len(CLASSES),
                                    id2label={i: c for i, c in enumerate(CLASSES)})
            model = TimesformerForVideoClassification(cfg)
            sd = torch.load(self.checkpoint, map_location="cpu", weights_only=False)
            model.load_state_dict(convert_state_dict(sd.get("state_dict", sd)), strict=True)
        else:
            raise PharError(
                "P-HAR model not found: %s\n  pass --phar-model with a fine-tuned model directory "
                "(models/vault-r<N>/) or P-HAR's timeSformer.pth from "
                "https://github.com/rlleshi/phar/releases/tag/v1.0.0 (Apache-2.0)" % self.checkpoint)
        self.device = pick_device(self.device_req)
        self._model = model.to(self.device).eval()
        self._torch = torch
        log.info("P-HAR TimeSformer loaded on %s: %s, %d classes (batch %d, stride %.1f s)",
                 self.device, self.name, len(self.classes), self.batch, self.stride / SAMPLE_FPS)

    def _load_finetuned(self, model_cls):
        """A save_pretrained() directory. Its labels.json must describe the input pipeline this
        engine feeds (frames, rate, crop, normalization); refuse anything else rather than
        silently feeding a model the wrong frames."""
        try:
            model = model_cls.from_pretrained(self.checkpoint)
        except Exception as e:                        # broken/foreign directory
            raise PharError("cannot load fine-tuned model %s: %s" % (self.checkpoint, e))
        id2label = model.config.id2label or {}
        classes = [str(id2label[k]) for k in sorted(id2label, key=int)]
        meta_path = os.path.join(self.checkpoint, "labels.json")
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            want = {"clip_len": CLIP_LEN, "sample_fps": SAMPLE_FPS, "size": SIZE, "short_side": 256,
                    "normalize": "(x-127.5)/127.5"}
            bad = {k: meta.get(k) for k, v in want.items() if k in meta and meta[k] != v}
            if bad:
                raise PharError("%s expects a different input pipeline %s; this engine feeds %s"
                                % (self.checkpoint, bad, want))
            if meta.get("classes") and list(meta["classes"]) != classes:
                raise PharError("%s: labels.json and config.json disagree on the class order"
                                % self.checkpoint)
        if not classes:
            raise PharError("%s has no class labels in config.json" % self.checkpoint)
        self.classes = classes
        self.suppress |= set(FINETUNED_SUPPRESS)
        return model

    def _frame_stream(self, video):
        """One ffmpeg pass: sampled, short side 256, center crop 224, raw RGB."""
        cmd = [self.ffmpeg, "-v", "error", "-nostdin", "-i", video,
               "-map", "0:V:0", "-an", "-sn", "-dn",
               "-vf", "fps=%s,scale=-2:256,crop=%d:%d" % (SAMPLE_FPS, SIZE, SIZE),
               "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise PharError("cannot run ffmpeg: %s" % e)
        return proc

    def analyze(self, video, stop_event=None):
        """Return {"spans": [...], "frame_interval": window seconds, "duration": None}.

        One span per accepted window: ``{"tag", "start", "end": None, "confidence"}`` — the same
        shape ai_client returns, so moments.key_moments consumes it unchanged.
        With ``keep_scores`` the result also carries ``"scores"``: every window's start time and
        its probabilities for all classes, before threshold and suppression (see score_track()).
        """
        self.load()
        torch = self._torch
        np = _numpy()
        proc = self._frame_stream(video)
        window = collections.deque(maxlen=CLIP_LEN)
        spans, pending, pending_idx = [], [], []
        track_t, track_p = [], []
        index = 0
        next_emit = 0

        def flush():
            if not pending:
                return
            clips = np.stack(pending)                                  # B,T,H,W,C
            x = torch.from_numpy(clips).float().permute(0, 1, 4, 2, 3)  # B,T,C,H,W
            x = ((x - 127.5) / 127.5).to(self.device)
            with torch.no_grad():
                probs = self._model(pixel_values=x).logits.softmax(-1).cpu()
            for start, p in zip(pending_idx, probs):
                if self.keep_scores:
                    track_t.append(round(start / SAMPLE_FPS, 2))
                    track_p.append([round(float(v), 4) for v in p])
                conf, idx = float(p.max()), int(p.argmax())
                label = self.classes[idx]
                if conf < self.threshold or label in self.suppress:
                    continue
                spans.append({"tag": label, "start": round(start / SAMPLE_FPS, 2),
                              "end": None, "confidence": round(conf, 3)})
            del pending[:], pending_idx[:]

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise PharError("interrupted")
                buf = proc.stdout.read(FRAME_BYTES)
                if not buf or len(buf) < FRAME_BYTES:
                    break
                window.append(np.frombuffer(buf, dtype=np.uint8).reshape(SIZE, SIZE, 3))
                index += 1
                if len(window) == CLIP_LEN and index - CLIP_LEN >= next_emit:
                    pending.append(np.stack(window))
                    pending_idx.append(index - CLIP_LEN)
                    next_emit = index - CLIP_LEN + self.stride
                    if len(pending) >= self.batch:
                        flush()
            flush()
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            proc.stderr.close()
            rc = proc.wait()
        if rc != 0:
            raise PharError("ffmpeg exit %d: %s" % (rc, err.splitlines()[-1] if err else "no output"))
        log.debug("%s: %d P-HAR windows kept (threshold %.2f, suppressed %s)",
                  video, len(spans), self.threshold, ",".join(sorted(self.suppress)) or "-")
        out = {"spans": spans, "frame_interval": WINDOW_SECONDS, "duration": None}
        if self.keep_scores:
            out["scores"] = self.score_track(track_t, track_p)
        return out

    def score_track(self, t, p):
        """The per-window probability track, as written to ``<video>.scores.json.gz``."""
        return {"schema": "vault-scores/1", "engine": self.name, "classes": list(self.classes),
                "window_seconds": WINDOW_SECONDS, "stride_seconds": round(self.stride / SAMPLE_FPS, 3),
                "sample_fps": SAMPLE_FPS, "t": t, "p": p}


def _numpy():
    try:
        import numpy
    except ImportError as e:
        raise PharError("phar engine needs numpy: %s" % e)
    return numpy
