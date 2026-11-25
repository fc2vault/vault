"""transcribe.py: man/woman tags from the voice pitch, how they reach the translator, and the
safety nets around the translation (looped words, ages under 18)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

T = helpers.load("webui/tools/transcribe.py", "transcribe_speakers_under_test")
try:
    import numpy as np
except ImportError:            # the app's own Python has no numpy; the Whisper env does
    np = None


def voice(f0, seconds=1.0, seed=0):
    """A vowel-like tone: f0 with falling harmonics and a little noise."""
    t = np.arange(int(seconds * T.SR)) / T.SR
    x = sum((0.5 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 6))
    return (0.3 * x + 0.01 * np.random.default_rng(seed).standard_normal(len(t))).astype(np.float32)


@unittest.skipUnless(np, "numpy not installed")
class PitchTest(unittest.TestCase):
    def test_pitch_of_a_man_and_a_woman(self):
        self.assertAlmostEqual(float(np.median(T.pitch_frames(voice(110)))), 110, delta=6)
        self.assertAlmostEqual(float(np.median(T.pitch_frames(voice(240)))), 240, delta=12)

    def test_silence_has_no_pitch(self):
        self.assertEqual(len(T.pitch_frames(np.zeros(T.SR, np.float32))), 0)
        self.assertEqual(len(T.pitch_frames(np.zeros(100, np.float32))), 0)

    def test_two_voices_are_told_apart(self):
        frames = [T.pitch_frames(voice(f, seed=i)) for i, f in enumerate([105, 120, 230, 260, 110, 250])]
        cut = T.speaker_cut(frames)
        self.assertIsNotNone(cut)
        self.assertEqual([T.speaker_label(f, cut) for f in frames], ["M", "M", "F", "F", "M", "F"])
        mixed = np.concatenate([frames[0], frames[2]])
        self.assertEqual(T.speaker_label(mixed, cut), "")          # both talking: no tag

    def test_one_voice_gets_no_tags(self):
        frames = [T.pitch_frames(voice(f, seed=i)) for i, f in enumerate([220, 240, 260, 230, 250, 270])]
        self.assertIsNone(T.speaker_cut(frames))


class FakeTranslator(T.Translator):
    def __init__(self, answers):
        super().__init__("http", "fake")
        self.answers, self.seen = answers, []

    def chat(self, user, max_tokens):
        self.seen.append((self.system, user))
        return self.answers.pop(0)


class TagPromptTest(unittest.TestCase):
    def test_tags_reach_the_model_and_never_the_subtitle(self):
        tr = FakeTranslator(["1: [M] Can I see your back?\n2: Sure.\n3: Mm."])
        en, refused = tr.translate(["後ろ見てもいいですか?", "はい", "うん"], speakers=["M", "F", ""])
        self.assertEqual(en, ["Can I see your back?", "Sure.", "Mm."])
        system, user = tr.seen[0]
        self.assertIn("[M] 後ろ見てもいいですか?", user)
        self.assertIn("[F] はい", user)
        self.assertIn("3: うん", user)
        self.assertIn("[M] = the man", system)

    def test_no_tags_no_note(self):
        tr = FakeTranslator(["1: Yes."])
        tr.translate(["はい"], speakers=["", ])
        self.assertNotIn("[M]", tr.seen[0][0])
        self.assertNotIn("[", tr.seen[0][1].split("\n", 1)[1])


class SafetyNetTest(unittest.TestCase):
    def test_loops_are_cut_to_three(self):
        self.assertEqual(T.collapse_loops("あげた" * 150), "あげた" * 3)
        self.assertEqual(T.collapse_loops("あ" * 40), "あ" * 6)
        for real in ("気持ちいい気持ちいい", "ちょっとちょっと", "もっともっと"):
            self.assertEqual(T.collapse_loops(real), real)

    def test_minor_ages_are_caught(self):
        for bad in ("10 years old", "She is 15.", "You're 12?", "I'm ten", "aged 16", "a 3-year-old"):
            self.assertTrue(T.MINOR_AGE.search(bad), bad)
        for ok in ("I'm 21 years old", "I came 10 times", "I'm 15 minutes late", "I'm 18 years old",
                   "Ten times!", "E cup"):
            self.assertFalse(T.MINOR_AGE.search(ok), ok)


if __name__ == "__main__":
    unittest.main()
