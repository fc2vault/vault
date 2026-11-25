#!/usr/bin/env python3
"""Vault transcription worker: Japanese speech -> English subtitles (.en.srt) with Whisper.

Run by serve.py with the Python that has a Whisper engine installed (Settings -> Subtitles);
it never imports Vault code, so it works from any interpreter / virtualenv.

    python transcribe.py VIDEO OUT.srt [--engine auto|mlx|faster|openai] [--model large-v3]
                         [--language ja] [--task translate] [--chunk 600]
    python transcribe.py --check          # which engines this Python can run (JSON)

Engines (all are Whisper; pick whatever runs fastest where Vault runs):
  mlx     mlx-whisper     Apple Silicon (Metal GPU). The fast one on an M-series Mac.
  faster  faster-whisper  Windows / Linux / Intel Mac. CPU (int8), or an NVIDIA GPU (CUDA).
  openai  openai-whisper  The reference PyTorch build, anywhere PyTorch runs (e.g. CUDA on ARM).
`auto` takes the first of those that imports.

Whisper's own `translate` task turns Japanese speech straight into English text in one pass.
(The large-v3-turbo model was not trained to translate; use large-v3, medium or small.)

The audio is decoded with ffmpeg and transcribed in chunks (default 10 min), so memory stays
flat on a 2-hour movie and progress can be reported. Within a chunk, a speech detector (Silero
VAD, which ships with faster-whisper) keeps only the stretches with talking: Whisper then runs on
those alone and its times are mapped back onto the movie. That is most of the speed (much of a
movie is no dialogue) and most of the timing accuracy and hallucination control. Without
faster-whisper installed the whole chunk is transcribed. Progress goes to stdout as JSON lines:
    {"progress": 0.42, "at": 1510.0, "duration": 3600.0}
    {"done": "OUT.srt", "segments": 812}
    {"error": "..."}
The SRT is written to a temp file and renamed into place, so a half-written file never shows.

Whisper invents text over long stretches without speech (moaning, music, silence): credits,
"thank you for watching", one phrase repeated many times. Segments the model itself marks as
probably-not-speech, known stock phrases, and runs of the same line are dropped.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

SR = 16000

# large-v3 as the 8-bit build: ~1.6 GB instead of ~3 GB, practically the same text. An 8 GB Mac
# ran out of memory with the full-precision one next to Vault and a browser.
MLX_REPOS = {"large-v3": "mlx-community/whisper-large-v3-mlx-8bit",
             "medium": "mlx-community/whisper-medium-mlx",
             "small": "mlx-community/whisper-small-mlx",
             "base": "mlx-community/whisper-base-mlx"}

# stock phrases Whisper produces from non-speech (translated and untranslated forms)
HALLUCINATIONS = [
    r"thank(s| you) (very much )?for watching", r"please subscribe", r"subtitles? by",
    r"subtitled by", r"translated by", r"amara\.org", r"see you (in the )?next (video|time)",
    r"ご視聴ありがとうございました", r"チャンネル登録", r"字幕", r"お疲れ様でした。?$",
    r"(あなた|彼女|彼)を愛しています", r"ご覧ください",
]
HALLU_RE = re.compile("|".join(HALLUCINATIONS), re.I)


def emit(**kw):
    sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def check():
    out = {"python": sys.version.split()[0], "platform": "%s-%s" % (platform.system(), platform.machine()),
           "ffmpeg": bool(shutil.which("ffmpeg")), "engines": []}
    out["llm"] = []
    try:
        __import__("mlx_lm")
        out["llm"].append("mlx")
    except Exception:
        pass
    for eng, mod in (("mlx", "mlx_whisper"), ("faster", "faster_whisper"), ("openai", "whisper")):
        try:
            __import__(mod)
            out["engines"].append(eng)
        except Exception:
            pass
    return out


def pick_engine(want):
    have = check()["engines"]
    if want and want != "auto":
        if want not in have:
            raise SystemExit("engine %s is not installed in this Python (%s)" % (want, sys.executable))
        return want
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    order = (["mlx"] if apple else []) + ["faster", "openai"]
    for e in order:
        if e in have:
            return e
    raise SystemExit("no Whisper engine installed in this Python (%s)" % sys.executable)


def duration_of(video):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", video], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# Voice band + level: cut rumble and hiss outside speech, then even out the volume so quiet
# talk under moaning is as loud as the rest. Both the speech detector and Whisper hear more:
# on a test scene 10 lines instead of 6, and 中に出す heard right where the raw audio gave お金出す.
VOICE_FILTER = "highpass=f=150,lowpass=f=4000,dynaudnorm=f=150:g=15"


def load_chunk(video, start, length, voice=True, af=None):
    """Mono 16 kHz float32 audio of [start, start+length) as a numpy array."""
    import numpy as np
    af = af or (VOICE_FILTER if voice else None)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", "%.3f" % start, "-t", "%.3f" % length,
           "-i", video, "-vn", "-ac", "1", "-ar", str(SR)] + (["-af", af] if af else []) + [
           "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


VAD_WINDOW = 30           # seconds


def speech_regions(audio):
    """[(start_sample, end_sample)] of speech, or None when no detector is installed."""
    try:
        from faster_whisper.vad import get_speech_timestamps, VadOptions
    except Exception:
        return None
    opts = VadOptions(threshold=0.5, min_silence_duration_ms=700, speech_pad_ms=300,
                      min_speech_duration_ms=400)
    # The detector carries state through the audio, and after minutes of moaning it stops
    # hearing speech at all (from 70:00 it missed a line at 73:19 that it found from 73:00).
    # So it starts fresh every 30 s; speech running across a window edge is joined up again.
    w = VAD_WINDOW * SR
    regs = []
    for w0 in range(0, len(audio), w):
        for d in get_speech_timestamps(audio[w0:w0 + w], opts):
            s, e = w0 + d["start"], w0 + d["end"]
            if regs and s - regs[-1][1] < int(0.3 * SR):
                regs[-1] = (regs[-1][0], max(regs[-1][1], e))
            else:
                regs.append((s, e))
    # A short sound right at the start of a chunk is the tail of a moan cut at the chunk boundary;
    # alone it is what Whisper turned into invented lines ("thank you very much") at 30:00, 1:15:00…
    regs = [(s, e) for s, e in regs if not (s <= int(0.35 * SR) and e - s < int(1.5 * SR))]
    # under a second of speech in a whole chunk: noise, nothing to transcribe
    if sum(e - s for s, e in regs) < 1.0 * SR:
        return []
    return regs


def to_original(t, regions, side="start"):
    """A time in the speech-only audio -> the time in the chunk it came from.

    Whisper's segment times sit on the joins between speech stretches more often than not (one
    line ends where the next begins). A line's start that lands in the last half second of a
    stretch belongs to the next one: that tail is the detector's padding, silence. Mapped as-is,
    a third of the lines of a test movie came up early by the length of the pause (1-5 s).
    Likewise a line's end in the first half second of a stretch belongs to the one before."""
    acc = 0.0
    for i, (s, e) in enumerate(regions):
        n = (e - s) / SR
        if t <= acc + n:
            off, edge = t - acc, min(0.5, n / 2)
            if side == "start" and n - off < edge and i + 1 < len(regions):
                return regions[i + 1][0] / SR
            if side == "end" and off < edge and i > 0:
                return regions[i - 1][1] / SR
            return s / SR + off
        acc += n
    return regions[-1][1] / SR if regions else t


def ram_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 8 << 30


class Engine:
    def __init__(self, name, model, language, task, mem_frac=0.33):
        self.name, self.language, self.task = name, language, task
        self.mx = None
        if name == "mlx":
            import mlx.core as mx
            import mlx_whisper
            # Memory cap. MLX keeps freed GPU buffers in a cache that otherwise grows until macOS
            # starts swapping; keep that small, and ask MLX to stay under a share of the RAM.
            limit = int(ram_bytes() * mem_frac)
            try:
                mx.set_memory_limit(limit)
                mx.set_cache_limit(256 << 20)
            except Exception:
                pass
            self.mx, self.limit = mx, limit
            self.mod, self.repo = mlx_whisper, MLX_REPOS.get(model, model)
        elif name == "faster":
            from faster_whisper import WhisperModel
            # int8 and a few threads: ~1-2 GB for large-v3, and the computer stays usable
            self.m = WhisperModel(model, device="auto", compute_type="int8",
                                  cpu_threads=max(2, min(4, (os.cpu_count() or 4) // 2)))
        else:
            import whisper
            self.m = whisper.load_model(model)

    def run(self, audio):
        """-> [(start, end, text, no_speech_prob, avg_logprob)] relative to `audio`."""
        common = dict(language=self.language, task=self.task, condition_on_previous_text=False)
        if self.name == "faster":
            segs, _ = self.m.transcribe(audio, vad_filter=False, **common)
            return [(s.start, s.end, s.text, s.no_speech_prob, s.avg_logprob) for s in segs]
        if self.name == "mlx":
            r = self.mod.transcribe(audio, path_or_hf_repo=self.repo, verbose=None, **common)
        else:
            r = self.m.transcribe(audio, verbose=None, fp16=False, **common)
        return [(s["start"], s["end"], s["text"], s.get("no_speech_prob", 0.0), s.get("avg_logprob", 0.0))
                for s in r.get("segments", [])]

    def release(self):
        """Between chunks: hand cached GPU memory back to the system."""
        import gc
        gc.collect()
        if self.mx is not None:
            try:
                self.mx.clear_cache()
            except Exception:
                pass

    def peak_mb(self):
        try:
            return round(self.mx.get_peak_memory() / 2**20) if self.mx is not None else None
        except Exception:
            return None


def keep(text, no_speech, logprob):
    t = text.strip()
    if not t or not re.search(r"\w", t):
        return False
    if HALLU_RE.search(t):
        return False
    if re.fullmatch(r"(?i)\(?(lol|laughs?|laughter|笑|w+)\)?[.!]?", t):   # 「笑」 rendered as a line of its own
        return False
    if no_speech > 0.6 and logprob < -1.0:         # Whisper's own "probably not speech"
        return False
    return True


# Second pass for chunks where the speech detector heard nothing. Silero misses quiet talk under
# moaning ("頭とか裏筋も食べて" at 2:02 in a test movie), so Whisper hears the whole chunk once
# more — where it mostly invents: stock phrases, a name or "うん" over and over, a sentence looped.
# Only lines Whisper itself is fairly sure are speech, that are not stock or looped, are kept.
QUIET_STOCK = re.compile(r"おやすみなさい|ご覧いただき|お会いしましょう|次回|ご視聴|ありがとうございました$|この動画|このビデオ|チャンネル|"
                         r"^(はい|うん|ええ|あ+|ん+)[。、!?]*$")


def _compression(t):
    import zlib
    b = t.encode("utf-8")
    return len(b) / max(1, len(zlib.compress(b)))


GAP_MIN = 6.0            # seconds without detected speech that get a second listen
PIECE = 6.0              # ... in pieces this long: Whisper's times in long non-speech audio drift
VOICE_P90 = 0.15         # ... and only pieces where the detector was at least this unsure
_VAD = {}
RELISTEN = "packed"     # five 6 s pieces per Whisper call (~40% faster, better on conversation;
                         # "pieces" = one call each: keeps short repeated words like 気持ちいい x5)


def voiced_pieces(audio, g0, g1):
    """Pieces of audio[g0:g1] that may hold words: the detector's own speech probability, below its
    threshold but not near zero (words under moaning reach 0.26-0.73 at the 90th percentile, moaning
    and silence 0.03-0.09). Skipping the rest makes the second listen ~6x cheaper."""
    import numpy as np
    try:
        from faster_whisper.vad import get_vad_model
        m = _VAD.get("m") or _VAD.setdefault("m", get_vad_model())
    except Exception:
        m = None
    step = int(PIECE * SR)
    for p0 in range(g0, g1, step):
        p1 = min(g1, p0 + step)
        if p1 - p0 < SR:
            continue
        if m is not None:                       # fresh detector state per piece (it goes deaf)
            x = audio[p0:p1]
            pp = m(np.pad(x, (0, (-len(x)) % 512))).reshape(-1)
            if np.percentile(pp, 90) < VOICE_P90:
                continue
        yield p0, p1


def transcribe_chunk(eng, audio, length, quiet_pass=True):
    """-> [(start, end, text, no_speech, logprob)] in seconds within the chunk."""
    regions = speech_regions(audio)
    if regions is None:                                 # no detector: the whole chunk
        return [(s, min(e, length), t, ns, lp) for s, e, t, ns, lp in eng.run(audio)]
    out = []
    if regions:
        import numpy as np
        speech = np.concatenate([audio[s:e] for s, e in regions])
        for s, e, t, ns, lp in eng.run(speech):
            a0 = to_original(s, regions)
            a1 = max(a0 + 0.3, to_original(e, regions, "end"))
            out.append((a0, min(a1, length), t, ns, lp))
    if quiet_pass:
        # The detector doesn't hear words said while moaning ("気持ちいい", "もう行く" between
        # 55:33 and 57:01 of a test movie: nothing even at its most sensitive). Every stretch of
        # 6 s or more it called silence gets a careful second listen, in 6 s pieces, where the
        # detector was not sure there was none (all of a chunk it found nothing in, too). Costs time on movies with long sex scenes.
        edges = [0] + [x for r in regions for x in r] + [len(audio)]
        pieces = [p for g0, g1 in zip(edges[0::2], edges[1::2]) if g1 - g0 >= GAP_MIN * SR
                  for p in voiced_pieces(audio, g0, g1)]
        heard = []
        if RELISTEN == "packed":
            # Whisper pads every call to 30 s: five 6 s pieces glued into one window cost what one
            # piece costs alone. Times map back per piece, like the main pass.
            import numpy as np
            per = max(1, int(30 // PIECE))
            for i in range(0, len(pieces), per):
                grp = pieces[i:i + per]
                got = eng.run(np.concatenate([audio[p0:p1] for p0, p1 in grp]))
                if len(grp) > 1 and any(is_loop(t) for _, _, t, _, _ in got):
                    # Whisper got stuck repeating a moan ("ああああ…" for the whole call) and the
                    # words in the other pieces are lost with it (58:06-58:24 of a test movie:
                    # nothing packed, five lines of climax dialogue one piece at a time).
                    for p0, p1 in grp:
                        heard += [(p0 / SR + s, min(p0 / SR + e, p1 / SR), t, ns, lp)
                                  for s, e, t, ns, lp in eng.run(audio[p0:p1])]
                    continue
                for s, e, t, ns, lp in got:
                    a0 = to_original(s, grp)
                    a1 = max(a0 + 0.3, to_original(e, grp, "end"))
                    heard.append((a0, a1, t, ns, lp))
        else:
            for p0, p1 in pieces:
                heard += [(p0 / SR + s, min(p0 / SR + e, p1 / SR), t, ns, lp)
                          for s, e, t, ns, lp in eng.run(audio[p0:p1])]
        out += quiet_keep(heard)
        out.sort(key=lambda x: x[0])
    return out


def is_loop(text):
    """Whisper stuck on one sound: the same character or short syllable over and over."""
    core = _PUNCT.sub("", text)
    return bool(re.search(r"(.)\1{9,}", core) or re.search(r"(.{2,4})\1{5,}", core))


def quiet_keep(segs):
    texts = [re.sub(r"\s+", "", t) for _, _, t, _, _ in segs]
    out = []
    for (s, e, t, ns, lp), k in zip(segs, texts):
        if ns >= 0.6 or lp < -1.0 or len(k) < 3:
            continue
        if re.search(r"[A-Za-z]", k) or QUIET_STOCK.search(k) or HALLU_RE.search(k):
            continue
        # Said again and again is real ("気持ちいい" three times while she comes); the loops Whisper
        # invents are marked not-speech above, or run on for many lines / one very long one.
        if texts.count(k) > 4 or _compression(k) > 4.0:
            continue
        out.append((s, e, t, ns, lp))
    return out


# Moans, sighs and laughter as subtitles ("Ah", "Uuuu…", "Huhuhu") are noise: dropped by default
# (--keep-moans keeps them). A Japanese line made of nothing but vocal sounds goes, except the
# short words that are real answers (はい yes, いや no, うん yeah…) and short questions (え? ん?).
# The English side catches what the Japanese check can't see (a sound misheard as a word).
_VOCAL = set("あぁいぃうぅえぇおぉんっはひふへほやゃゅょゎーアァイィウゥエェオォンッハヒフヘホ")
_PUNCT = re.compile(r"[\s、。,.!?！？…〜~・「」()（）]+")
JA_ANSWERS = {"はい", "はーい", "いいえ", "いや", "いやいや", "いい", "うん", "うんうん", "ううん", "ええ",
              "えぇ", "おい", "おいおい", "いやだ"}
EN_SOUND = re.compile(r"(?i)(a+h*|o+h+|u+h*|m+|h+m+|n+|h*a+h+|(ha)+h?|(hu)+|(he)+|(fu)+|o+|wo+|ugh|"
                      r"[a-z]?(mn)+m?|eh|huh|u?wa+h*)")
FILTER_MOANS = True


def is_moan(text):
    t = text.strip()
    core = _PUNCT.sub("", t)
    if not core:
        return True
    if t.endswith(("?", "？")):                     # a reaction ("え?", "Hm?") is dialogue
        return False
    if all(ch in _VOCAL for ch in core):
        return core not in JA_ANSWERS
    words = [w for w in re.split(r"[\s,.!~…-]+", t) if w]
    return bool(words) and all(EN_SOUND.fullmatch(w) for w in words)


def srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


# Japanese lines with a stray lowercase foreign word in them ("僕が coaster", "片手はマシ influential"):
# Whisper wandering into another language on audio it can't make out. English loanwords come out
# in katakana or capitals (OK, SNS, AV), so a lowercase Latin word of 4+ letters is noise.
FOREIGN_WORD = re.compile(r"(?<![A-Za-z])[a-zß-ÿĀ-ž]{4,}(?![A-Za-z])")


def drop_foreign(text):
    return re.sub(r"\s{2,}", " ", FOREIGN_WORD.sub("", text)).strip()


def collapse_loops(text):
    """"ううう…" x400 -> six; a word Whisper looped ("あげたあげた…" x150) -> three times."""
    text = re.sub(r"(.)\1{5,}", r"\1\1\1\1\1\1", text)
    return re.sub(r"(.{2,8}?)\1{3,}", r"\1\1\1", text)


# Everyone in these videos is an adult. A line that states an age under 18 is a mishearing
# (10回 "ten times" heard as 10歳) and must never reach the screen: the cue is dropped.
_NUM = r"(?:[1-9]|1[0-7]|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)"
MINOR_AGE = re.compile(r"(?i)\b%s[- ]?(?:years?[- ]old|year-olds?|yo)\b|\baged? %s\b|\b%s(?: years)? of age\b"
                       r"|\b(?:I'?m|I am|she'?s|she is|you'?re|you are|turned|turning) %s(?=\s*(?:[.!?,…]|$|now|this|next))"
                       % (_NUM, _NUM, _NUM, _NUM))


def clean(segs):
    """Drop hallucinations, collapse runs of the same line, fix overlaps."""
    out = []
    for a, b, t, ns, lp in segs:
        t = re.sub(r"\s+", " ", t).strip()
        t = collapse_loops(t)
        if not keep(t, ns, lp) or (FILTER_MOANS and is_moan(t)):
            continue
        if out and out[-1][2].lower() == t.lower() and a - out[-1][1] < 5:
            out[-1][1] = max(out[-1][1], b)            # the same line again: one longer cue
            continue
        out.append([a, b, t, ns])
    # a phrase that comes back over and over is a loop, not dialogue — unless Whisper was sure
    # it was speech each time (she can say 気持ちいい a dozen times in a movie)
    from collections import Counter
    cnt = Counter(x[2].lower() for x in out if x[3] >= 0.3)
    out = [x[:3] for x in out if x[3] < 0.3 or cnt[x[2].lower()] < 8 or len(x[2]) > 40]
    # a cue spanning a long silence (its end mapped across a gap, or two repeats merged across
    # one): keep it on screen about as long as it takes to read. Last, so nothing re-lengthens it.
    for x in out:
        if x[1] - x[0] > 12:
            x[1] = x[0] + min(12.0, max(2.0, 0.08 * len(x[2]) + 1.5))
    for i in range(len(out) - 1):
        if out[i][1] > out[i + 1][0]:
            out[i][1] = out[i + 1][0]
    return out


# ---------------------------------------------------------------- who is talking: man or woman
# Japanese leaves out who does what, and the translator's worst habit is turning the man's
# questions into "I" ("Can I see my back?"). The voice tells them apart: men speak at about
# 85-155 Hz, women at 165-350 Hz. Each cue's pitch frames vote, against a man/woman cut found
# per movie (the valley between the two voices, kept within 140-200 Hz). Cues that are unclear
# (mixed, too quiet, whispered) get no tag. Hand-checked on two interviews (2026-11-23): 53 right,
# 5 wrong, 23 untagged.
PITCH_AF = "highpass=f=60,highpass=f=60,lowpass=f=1000,lowpass=f=1000"
PITCH_THRESHOLD = 0.4          # YIN dip: the man's voice through a camera mic is rarely cleaner
PITCH_SLACK = 0.05
SPEAKERS = True


def pitch_frames(x):
    """F0 (Hz) of the voiced 40 ms frames of a mono 16 kHz clip (YIN-style, 10 ms hop)."""
    import numpy as np
    W, H = 640, 160
    lmin, lmax = SR // 450, SR // 65
    if len(x) < W + H:
        return np.zeros(0)
    n = (len(x) - W) // H
    fr = np.lib.stride_tricks.as_strided(x, (n, W), (x.strides[0] * H, x.strides[0])).astype(np.float64)
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = fr[(fr * fr).sum(axis=1) >= 0.02]             # silence has no pitch
    if not len(fr):
        return np.zeros(0)
    spec = np.fft.rfft(fr, 2 * W, axis=1)
    ac = np.fft.irfft(spec * spec.conj(), 2 * W, axis=1)[:, :lmax + 1]
    d = 2 * (ac[:, :1] - ac)                             # difference function
    cm = d[:, 1:] / (np.cumsum(d[:, 1:], axis=1) / np.arange(1, lmax + 1) + 1e-9)
    cm = np.concatenate([np.ones((len(cm), 1)), cm], axis=1)
    seg = cm[:, lmin:lmax]
    # the first dip that is nearly as deep as the deepest one: a shallow early dip is a harmonic
    # (the man heard at 2-3x his pitch), the period itself dips deepest
    th = np.minimum(PITCH_THRESHOLD, seg.min(axis=1, keepdims=True) + PITCH_SLACK)
    below = seg < th
    ok = below.any(axis=1)
    k = below.argmax(axis=1) + lmin                      # the first dip under the threshold...
    out = []
    for row, kk, good in zip(cm, k, ok):
        if not good:
            continue
        while kk + 1 < lmax and row[kk + 1] < row[kk]:   # ...down to its bottom
            kk += 1
        out.append(SR / kk)
    return np.array(out)


def speaker_cut(frames):
    """The man/woman pitch boundary for this movie (Otsu on log F0), or None if there aren't two voices."""
    import numpy as np
    f = np.concatenate([x[(x >= 70) & (x < 450)] for x in frames]) if frames else np.zeros(0)
    if len(f) < 200:
        return None
    lf = np.log(f)
    h, edges = np.histogram(lf, 60)
    c = (edges[:-1] + edges[1:]) / 2
    best, cut = -1.0, None
    for i in range(1, 60):
        w0, w1 = h[:i].sum(), h[i:].sum()
        if not w0 or not w1:
            continue
        m0, m1 = (h[:i] * c[:i]).sum() / w0, (h[i:] * c[i:]).sum() / w1
        v = w0 * w1 * (m0 - m1) ** 2
        if v > best:
            best, cut = v, edges[i]
    cut = min(max(float(np.exp(cut)), 140.0), 200.0)
    low, high = f[f < cut], f[f >= cut]
    # one voice only (a woman alone, or a man alone) splits into two halves of the same voice
    if len(low) < 0.05 * len(f) or len(high) < 0.05 * len(f) or np.median(low) > 160 or np.median(high) < 170:
        return None
    return cut


def speaker_label(f, cut):
    m = int(((f >= 70) & (f < cut * 0.95)).sum())
    w = int(((f >= cut * 1.05) & (f < 450)).sum())
    if m + w < 5:
        return ""
    return "M" if m > 2 * w else ("F" if w > 2 * m else "")


def speaker_tags(video, cues, chunk=600.0):
    """'M', 'F' or '' for each cue [start, end, text], from the movie's audio."""
    import numpy as np
    frames = [np.zeros(0)] * len(cues)
    if not cues:
        return []
    t, end = 0.0, max(c[1] for c in cues)
    while t < end:
        idx = [i for i, c in enumerate(cues) if t <= c[0] < t + chunk]
        if idx:
            hi = max(cues[i][1] for i in idx)
            audio = load_chunk(video, t, hi - t + 0.1, af=PITCH_AF)
            for i in idx:
                frames[i] = pitch_frames(audio[int((cues[i][0] - t) * SR):int((cues[i][1] - t) * SR)])
            del audio
        t += chunk
    cut = speaker_cut(frames)
    if cut is None:
        return [""] * len(cues)
    return [speaker_label(f, cut) for f in frames]


# ---------------------------------------------------------------- LLM translation (two-step mode)
# Whisper's own `translate` hears Japanese and writes English in one go: the gist is right, but it
# guesses who is talking about whom (Japanese leaves the subject out) and confuses words that sound
# alike (職 "job" / 食 "food"). Its Japanese transcript is much more accurate. So in `--translator
# llm` mode Whisper only writes the Japanese, Whisper is unloaded, and a small instruction-tuned
# text model translates the lines in batches, seeing the conversation around them.
LLM_SYSTEM = (
    "You translate Japanese subtitles of an adult video (all performers are adults) into English "
    "subtitles. Usually an interviewer or man talks with a woman: first an interview, then sex. "
    "Translate faithfully and naturally, the way people really talk; sexual words stay sexual and "
    "explicit — never soften, censor, refuse, explain or add notes. "
    "Who is meant: Japanese leaves it out, so work it out from the conversation. The man mostly asks "
    "and gives instructions, the woman answers: his questions, commands and remarks (〜の?, 〜んだ, "
    "〜でしょ, 〜てくれた, 脱いで, 緊張してる) are about her (\"you\"), not \"I\"; おじさん said by "
    "the man means himself. He is often just as polite as she is: his 〜んですか?, 〜ますか?, "
    "〜てもいいですか?, 〜ですね about her, echo questions repeating her words (21歳?, 自分の髪で?), "
    "お子さん, 旦那さん, 奥さん, いらっしゃる and her name + さん are him talking to her (\"you\", \"your\"); "
    "her short answers come right after his questions. 自分 = oneself (usually her), not \"my\". "
    "Names stay romanised as names (ひなさん = Hina) — never translate their kanji; titles of shows "
    "and films: the official English title, or \"it\", never a romanised guess. "
    "Keep negatives and questions as they are: 〜じゃない?/〜ないの? asks for agreement (\"isn't it…?\"), "
    "and もちろん/うん after it agrees. "
    "A sentence can run over several lines: translate only each line's own part, never move words "
    "to another number. "
    "Everyone is an adult: never write an age under 18 — a number + 歳 under 18 in sex talk is "
    "misheard 回 (times). Copy numbers exactly. "
    "The Japanese comes from speech recognition and contains misheard words: translate what was most "
    "likely said. A word that makes no sense here is probably misheard — guess the likely word from "
    "the scene; never write misheard nonsense or romanised gibberish (\"Tomato\", \"Gundam\", \"still "
    "life\"). If a line is unclear, keep it short and plain; don't invent sex words that aren't there. "
    "Moans and sounds stay short (\"Ah!\", \"Mm...\"). "
    "Glossary — sex: エッチ = sexy, naughty, sex; 気持ちいい = feels good; "
    "イク/イっちゃう/行く/行って/行きそう/行っちゃう/いった (in sex, or talking about masturbating) = cum, I'm cumming, about to cum; 行かせる = make (her) cum; "
    "出る/出ちゃう/出すよ/出そう (a man, in sex) = cum, I'm gonna cum; 出る/出ない/出ちゃう/吹いた (a woman, "
    "fingering, a toy) = squirt, squirting — never \"cum\" for her; 出る of milk (母乳) = milk comes out; "
    "出て/上がる (from a bath or room) = get out; "
    "射精/精子 (misheard 写生, 写真) = cumming, cum; 外に出して = pull out (cum outside), only for 外; "
    "中に出した/中に出ちゃった = came inside; 中に出す/中出し = cum inside; 口に出して = cum in my mouth; "
    "何発 = how many loads; かき出す (misheard 書き出す) = scoop it (the cum) out; "
    "抜く = make him cum, jerk off (or pull it out, by context) — but 抜いて with clothes is misheard 脱いで = "
    "take it off; 生 (in sex) = raw, bareback, no condom — never \"fresh\"; "
    "ゴム = condom; ゴムつける = put on a condom; 取る/外す (a condom) = take it off; 外す (a ring, mask) = take off; "
    "感じる/感じやすい/感じるところ = sensitive, turned on; "
    "前から = from the front; 後ろから (misheard 後から) = from behind; 正常位 = missionary; 騎乗位 = cowgirl; "
    "バック = doggy; 飲んじゃった (cum) = swallowed; 焦らす = tease; 変態 = pervert, kinky; "
    "欲求不満 = sexually frustrated; する/してる (with someone) = have sex; "
    "漏れちゃう = leaking; 大洪水 = soaked; "
    "立つ/立ってる/立ってきた/立っちゃった (of a cock) = get hard, hard; 硬い = hard (only of a cock; "
    "of a person: serious); 舐める (misheard ラメ) = lick; 咥える (misheard 加える, 食われる) = suck; フェラ = blowjob; "
    "入れる/入れて/挿れて = put it in (never \"insert\"); 濡れてる/濡れてきた (misheard 寝てる) = wet, getting wet; 垂れてる = dripping; 潮/潮吹き (misheard 塩) = squirt, squirting; "
    "吹いてる (misheard 吐いてる) = squirting; 締まり (misheard 縛り), キツい (in sex) = tight; "
    "四つん這い = on all fours; 手をついて = put your hands on (it); 腰 = hips; 当てる (a toy) = press it on; "
    "電マ (misheard 電話, テンマン, トマト) = vibrator, wand — never \"phone\"; ローター = bullet vibrator; ムラムラ = horny; 興奮する = turned on; "
    "やらしい/いやらしい = dirty, lewd; 性感帯 = erogenous zones; 経験人数 = how many guys she's slept with; "
    "何本 = how many cocks; 乱交 (misheard 乱行, 韓国) = orgy; 痴漢 = groper; 痴漢に遭う = get groped. "
    "Body: ちんちん/ちんこ/おちんちん/ちんぽ = cock, dick; 裏筋 = underside of the cock; まんこ (misheard マンゴー, マント, あんこ) = "
    "pussy; まん毛 = pubic hair; クリ (misheard 栗, クリーム) = clit; おっぱい = tits, boobs; 乳首 (misheard ちくみ, チクメ) "
    "= nipples; ケツ = ass; 唾 (misheard 粒) = spit; ベロ/舌 = tongue; 谷間 = cleavage; 奥 = deep (inside); "
    "Eカップ (misheard いいカップ; a lone letter or 塩 after 何カップ) = E cup, the bra size; 母乳 = breast milk. "
    "Clothes and poses: パンツ = panties (not pants); Tバック = thong; 下着になって = strip to your underwear; "
    "脱いで = take it off; 剃ってる = shaved; ピース = make a peace sign (for the camera). "
    "Everyday: この子/子 = girl (never \"kid\"); 恥ずかしい = I'm embarrassed, this is embarrassing; "
    "くすぐったい = ticklish; スタイルいい = great body; くさい = stinks; 違う/違います = no, that's not it; "
    "へー = huh, oh?; 嘘 = no way, really?; 聞いてない = you never told me; やばい = so good / oh no (by context); "
    "だめ = no, don't (だめ before イく/出る = \"No, I'm cumming!\"); もっと = more; 出す (a video) = put it out, publish; "
    "撮る/回してる = film, the camera's rolling; はじめまして = nice to meet you; 久しぶり = first time in ages; "
    "出会い = chances to meet guys; 気が合う = hit it off; 気になる = curious about; 聞いてる = have been told; "
    "憧れ = dream of; 旦那 = husband; バレる = get found out; ゆっくりできる = have time; 喉乾いた = thirsty; "
    "イケメン = hot guy. "
    "Keep every line short like a subtitle. Answer with exactly one line per numbered input line, "
    "in the form `N: English`, same numbers, nothing else.")
SPEAKER_NOTE = (" Lines may start with a speaker tag from the voice: [M] = the man says it, [F] = the woman "
                "says it (no tag = unknown; a tag can be wrong now and then). Write each line from that speaker's "
                "point of view: [M] questions and remarks about her are \"you\"; [F] lines about herself are \"I\". "
                "Never put the tag in your answer.")
_TAG_ECHO = re.compile(r"^\s*\[(?:M|F|W)\]\s*")
# only the assistant voice: "I can't" alone is everyday dialogue ("I can't say that")
REFUSAL_RE = re.compile(r"\b(I (can'?t|cannot|won'?t|am not able to|'m not able to) (help|assist|translate|provide|"
                        r"comply|do that|fulfil)|I'?m sorry, but|as an AI|this content|explicit content)\b", re.I)


class Translator:
    """backend `mlx` (mlx-lm, Apple Silicon) or `http` (any OpenAI-compatible server: Ollama,
    LM Studio, llama.cpp server — for Windows / Linux)."""

    def __init__(self, backend, model, url=None, mem_frac=0.4):
        self.backend, self.model, self.url = backend, model, (url or "").rstrip("/")
        self.system = LLM_SYSTEM
        self.mx = None
        if backend == "mlx":
            import mlx.core as mx
            from mlx_lm import load
            try:
                mx.set_memory_limit(int(ram_bytes() * mem_frac))
                mx.set_cache_limit(256 << 20)
            except Exception:
                pass
            self.mx = mx
            self.m, self.tok = load(model)

    def chat(self, user, max_tokens):
        if self.backend == "mlx":
            from mlx_lm import generate
            from mlx_lm.sample_utils import make_sampler
            msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
            kw = {}
            try:   # Qwen3 and friends: no "thinking" preamble
                prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False,
                                                      enable_thinking=False)
            except TypeError:
                prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
            # small prefill steps: the prompt is read in pieces, so its activations stay small
            out = generate(self.m, self.tok, prompt=prompt, max_tokens=max_tokens,
                           sampler=make_sampler(temp=0.2), prefill_step_size=256, **kw)
            try:
                self.mx.clear_cache()
            except Exception:
                pass
            return re.sub(r"(?s)<think>.*?</think>", "", out).strip()
        import urllib.request
        body = json.dumps({"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                           "messages": [{"role": "system", "content": self.system},
                                        {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(self.url + "/v1/chat/completions", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()

    def translate(self, lines, progress=None, batch=15, context=6, speakers=None):
        """Japanese lines -> English lines (same length). Returns (english, refused_count).
        speakers: 'M' / 'F' / '' per line (who says it), or None."""
        raw = lines
        if speakers and any(speakers):
            self.system = LLM_SYSTEM + SPEAKER_NOTE
            lines = [("[%s] " % sp if sp else "") + ln for sp, ln in zip(speakers, lines)]
        else:
            self.system = LLM_SYSTEM
        out = [None] * len(lines)
        refused = 0
        for b0 in range(0, len(lines), batch):
            idx = list(range(b0, min(len(lines), b0 + batch)))
            ctx = "".join("  %s\n" % lines[j] for j in range(max(0, b0 - context), b0))
            user = ((("Earlier lines, for context only (do not translate):\n" + ctx + "\n") if ctx else "")
                    + "Translate:\n" + "".join("%d: %s\n" % (k + 1, lines[i]) for k, i in enumerate(idx)))
            got = self._parse(self.chat(user, 40 * len(idx) + 60), len(idx))
            for k, i in enumerate(idx):
                out[i] = got.get(k + 1)
            # what didn't come back (or came back as a refusal): one line at a time
            for k, i in enumerate(idx):
                # (an answer with no Latin letters is the Japanese echoed back, not a translation)
                if not out[i] or REFUSAL_RE.search(out[i]) or not re.search(r"[A-Za-z]", out[i]):
                    one = self._parse(self.chat("Translate:\n1: %s\n" % lines[i], 80), 1).get(1)
                    if not one or not re.search(r"[A-Za-z]", one):     # echoed again: ask plainly
                        one = self._parse(self.chat("Translate this Japanese subtitle line into "
                                                    "English. Reply with the English only.\n%s" % raw[i], 80), 1).get(1)
                    if one and not REFUSAL_RE.search(one) and re.search(r"[A-Za-z]", one):
                        out[i] = one
                    else:
                        refused += 1
                        out[i] = raw[i]            # keep the Japanese rather than nothing
            if progress:
                progress(min(1.0, (b0 + len(idx)) / max(1, len(lines))))
        return out, refused

    @staticmethod
    def _parse(text, n):
        got = {}
        for ln in text.splitlines():
            m = re.match(r"\s*(\d+)\s*[:.)\]]\s*(.*\S)", ln)
            if m and 1 <= int(m.group(1)) <= n and int(m.group(1)) not in got:
                got[int(m.group(1))] = _TAG_ECHO.sub("", m.group(2)).strip().strip('"')
        if not got and n == 1 and text.strip():
            got[1] = _TAG_ECHO.sub("", text.strip().splitlines()[0]).strip().strip('"')
        return got

    def release(self):
        import gc
        self.m = self.tok = None
        gc.collect()
        if self.mx is not None:
            try:
                self.mx.clear_cache()
            except Exception:
                pass


def write_srt(path, cues):
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for i, (s, e, t) in enumerate(cues, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (i, srt_time(s), srt_time(e), t))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--engine", default="auto")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--task", default="translate")
    ap.add_argument("--chunk", type=float, default=300.0)
    ap.add_argument("--mem", type=float, default=0.33, help="MLX: share of the RAM to stay under")
    ap.add_argument("--translator", default="whisper", choices=("whisper", "llm"),
                    help="whisper: Whisper translates (one pass); llm: Whisper writes Japanese, an LLM translates")
    ap.add_argument("--llm", default="mlx-community/Qwen3-4B-Instruct-2507-4bit",
                    help="mlx-lm model (Apple Silicon), or the model name on --llm-url")
    ap.add_argument("--llm-url", default="", help="OpenAI-compatible server (Ollama: http://localhost:11434)")
    ap.add_argument("--ja-out", default="", help="also keep the Japanese subtitles here (llm mode)")
    ap.add_argument("--ja-in", default="", help="llm mode: translate this existing Japanese .srt instead of transcribing")
    ap.add_argument("--relisten", default=None, choices=("pieces", "packed"),
                    help="second listen: one Whisper call per 6 s piece, or five pieces per call")
    ap.add_argument("--keep-moans", action="store_true",
                    help="keep lines that are only moans, sighs or laughter")
    ap.add_argument("--no-voice-filter", dest="voice_filter", action="store_false",
                    help="feed the raw audio instead of the voice band evened out in level")
    ap.add_argument("--no-quiet-pass", dest="quiet_pass", action="store_false",
                    help="skip the second listen to chunks where the speech detector heard nothing")
    ap.add_argument("--no-speakers", dest="speakers", action="store_false",
                    help="llm mode: don't tag lines as the man's or the woman's (from the voice pitch)")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    global FILTER_MOANS, RELISTEN, SPEAKERS
    FILTER_MOANS = not a.keep_moans
    SPEAKERS = a.speakers
    RELISTEN = a.relisten or RELISTEN
    if a.check:
        print(json.dumps(check()))
        return
    if not a.video or not a.out:
        ap.error("VIDEO and OUT are required")
    if not shutil.which("ffmpeg"):
        emit(error="ffmpeg not found on PATH")
        sys.exit(2)
    try:
        eng_name = pick_engine(a.engine)
    except SystemExit as e:
        emit(error=str(e))
        sys.exit(2)
    dur = duration_of(a.video)
    if dur <= 0:
        emit(error="could not read the video's duration")
        sys.exit(2)
    llm_mode = a.translator == "llm"
    share = 0.7 if llm_mode else 1.0                   # progress: listening 0-70 %, translating 70-100 %
    emit(engine=eng_name, model=a.model, duration=dur, progress=0.0,
         translator=(a.llm.split("/")[-1] if llm_mode else "whisper"))
    try:
        os.nice(10)                     # background work: the user's apps come first
    except (AttributeError, OSError):
        pass
    t0 = time.time()
    if llm_mode and a.ja_in:
        cues = [c[:2] + [collapse_loops(drop_foreign(c[2]))] for c in read_srt(a.ja_in)]
        cues = [c for c in cues if keep(c[2], 0.0, 0.0)]
        emit(progress=share, phase="translating", elapsed=0.0)
        return finish_llm(a, cues, t0, share, None)
    eng = Engine(eng_name, a.model, a.language, "transcribe" if llm_mode else a.task, a.mem)
    segs = []
    start = 0.0
    while start < dur:
        length = min(a.chunk, dur - start)
        audio = load_chunk(a.video, start, length, a.voice_filter)
        if len(audio) > SR:                             # skip a sub-second tail
            for s, e, t, ns, lp in transcribe_chunk(eng, audio, length, a.quiet_pass):
                segs.append((start + s, start + e, t, ns, lp))
        start += length
        del audio
        eng.release()
        emit(progress=round(share * start / dur, 4), at=round(start, 1), duration=dur,
             elapsed=round(time.time() - t0, 1), peak_mb=eng.peak_mb(),
             phase="listening" if llm_mode else None)
    if llm_mode:                                       # Japanese: stray foreign words are noise
        segs = [(s0, s1, drop_foreign(t), ns, lp) for s0, s1, t, ns, lp in segs]
    cues = clean(segs)
    if not llm_mode:
        write_srt(a.out, cues)
        emit(done=a.out, segments=len(cues), elapsed=round(time.time() - t0, 1), peak_mb=eng.peak_mb())
        return
    peak = eng.peak_mb()
    del eng                                            # Whisper out of memory before the LLM comes in
    import gc
    gc.collect()
    try:
        import mlx.core as mx
        mx.clear_cache()
        mx.reset_peak_memory()
    except Exception:
        pass
    if a.ja_out:
        write_srt(a.ja_out, cues)
    finish_llm(a, cues, t0, share, peak)


def read_srt(path):
    txt = open(path, encoding="utf-8").read().replace("\r\n", "\n")
    cues = []
    for blk in re.split(r"\n\s*\n", txt.strip()):
        ls = blk.splitlines()
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", blk)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s0 = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        s1 = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text = " ".join(l for l in ls if "-->" not in l and not re.fullmatch(r"\d+", l.strip())).strip()
        if text:
            cues.append([s0, s1, text])
    return cues


def finish_llm(a, cues, t0, share, whisper_peak):
    backend = "http" if a.llm_url else "mlx"
    speakers = None
    if SPEAKERS:
        emit(phase="speakers", progress=share, elapsed=round(time.time() - t0, 1))
        try:
            speakers = speaker_tags(a.video, cues)
        except Exception as e:                         # no tags is fine: the translator guesses as before
            emit(warning="speaker detection failed: %s" % e)
        if speakers:
            emit(speakers={"M": speakers.count("M"), "F": speakers.count("F"), "unknown": speakers.count("")})
    emit(phase="translating", progress=share, line_count=len(cues), elapsed=round(time.time() - t0, 1))
    tr = Translator(backend, a.llm, a.llm_url)
    en, refused = tr.translate([c[2] for c in cues], speakers=speakers,
                               progress=lambda f: emit(progress=round(share + (1 - share) * f, 4),
                                                       phase="translating",
                                                       elapsed=round(time.time() - t0, 1)))
    peak = None
    if tr.mx is not None:
        try:
            peak = round(tr.mx.get_peak_memory() / 2 ** 20)
        except Exception:
            pass
    tr.release()
    pairs = [(c, t) for c, t in zip(cues, en)
             if t and keep(t, 0.0, 0.0) and not MINOR_AGE.search(t)
             and not (FILTER_MOANS and (is_moan(t) or is_moan(c[2])))]
    out = [[c[0], c[1], t] for c, t in pairs]
    write_srt(a.out, out)
    if a.ja_out and len(pairs) < len(cues):
        write_srt(a.ja_out, [list(c) for c, t in pairs])
    emit(done=a.out, segments=len(out), refused=refused, elapsed=round(time.time() - t0, 1),
         peak_mb=max(x for x in (whisper_peak, peak, 0) if x is not None))


if __name__ == "__main__":
    main()
