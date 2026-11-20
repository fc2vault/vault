"""transcribe.py text filters: Whisper loops, stray foreign words, stock hallucinations, moans."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

T = helpers.load("webui/tools/transcribe.py", "transcribe_under_test")


class FilterTest(unittest.TestCase):
    def test_loop(self):
        self.assertTrue(T.is_loop("あ" * 60))
        self.assertTrue(T.is_loop("あ、" * 20))
        self.assertTrue(T.is_loop("ああ!" * 8))
        for real in ("子宮の中出して、赤ちゃん欲しい", "気持ちいい気持ちいい", "ちょうだい!ちょうだい!たくさん!"):
            self.assertFalse(T.is_loop(real), real)

    def test_foreign_words(self):
        self.assertEqual(T.drop_foreign("僕が coaster"), "僕が")
        self.assertEqual(T.drop_foreign("片手はマシ influential"), "片手はマシ")
        for keep in ("SRサイズ", "OKです", "Tバック", "AVとか"):
            self.assertEqual(T.drop_foreign(keep), keep)

    def test_stock_lines(self):
        self.assertFalse(T.keep("私はあなたを愛しています", 0, 0))
        self.assertFalse(T.keep("後ろの位置をご覧ください", 0, 0))
        self.assertTrue(T.keep("愛してる", 0, 0))

    def test_glossary_has_the_reviewed_fixes(self):
        for w in ("射精", "ゴム", "ムラムラ", "電マ", "パンツ = panties", "never \"kid\""):
            self.assertIn(w, T.LLM_SYSTEM)


if __name__ == "__main__":
    unittest.main()
