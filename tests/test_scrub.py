"""scrub.py: the coarse-to-fine order, the background queue, and (when ffmpeg is there) a real
strip from a generated test video. No library, no network."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

scrub = helpers.load("webui/scrub.py", "scrub_under_test")
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class OrderTest(unittest.TestCase):
    def test_every_slot_once_coarse_first(self):
        for n in (1, 7, 16, 50, 385, 1800):
            order = scrub._ordered_slots(n)
            self.assertEqual(sorted(order), list(range(n)))
        order = scrub._ordered_slots(385)
        first = sorted(order[:16])                   # the first 16 spread over the whole movie
        self.assertLess(first[0], 385 / 16)
        self.assertGreater(first[-1], 385 * 15 / 16 - 1)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vaulttest-scrub-")
        self.s = scrub.Scrubber(os.path.join(self.d, "cache"))
        self.built = []
        self.s.build = lambda p: self.built.append(p)     # no ffmpeg: record the order only
        self.vids = []
        for n in "abc":
            p = os.path.join(self.d, n + ".mp4")
            open(p, "wb").close()
            self.vids.append(p)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def wait(self):
        for _ in range(200):
            if not self.s.queue_len():
                return
            time.sleep(0.01)

    def test_player_request_jumps_the_queue(self):
        self.s._lock.acquire()                              # hold the worker until all are in
        try:
            self.s._q.extend(self.vids[:2])
            self.s._bg.update(self.vids[:2])
        finally:
            self.s._lock.release()
        self.s.request(self.vids[2], front=True)
        self.wait()
        self.assertEqual(self.built[0], self.vids[2])
        self.assertEqual(sorted(self.built), sorted(self.vids))

    def test_stop_background_drops_queued_nightly_work(self):
        self.s._q.extend(self.vids)
        self.s._bg.update(self.vids[:2])
        self.s.stop_background()
        self.assertEqual(list(self.s._q), [self.vids[2]])
        self.assertFalse(self.s.background_busy())

    def test_status_of_unknown_and_changed_files(self):
        self.assertEqual(self.s.status(self.vids[0])["state"], "none")
        self.s._write(self.vids[0], {"fmt": scrub.FMT, "sig": self.s._sig(self.vids[0]), "done": True,
                                     "n": 1, "t": [1.0]})
        self.assertEqual(self.s.status(self.vids[0])["state"], "done")
        self.assertFalse(self.s.needed(self.vids[0]))
        with open(self.vids[0], "wb") as f:                  # the file changes: made again
            f.write(b"x")
        self.assertEqual(self.s.status(self.vids[0])["state"], "none")
        self.s._write(self.vids[1], {"sig": self.s._sig(self.vids[1]), "done": True, "n": 1})
        self.assertEqual(self.s.status(self.vids[1])["state"], "none")   # an older layout


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class BuildTest(unittest.TestCase):
    def test_real_strip(self):
        d = tempfile.mkdtemp(prefix="vaulttest-scrubb-")
        try:
            vid = os.path.join(d, "v.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=d=60:s=320x240:r=10",
                            "-g", "20", vid], check=True)
            s = scrub.Scrubber(os.path.join(d, "cache"))
            s.build(vid)
            st = s.status(vid)
            self.assertEqual(st["state"], "done")
            self.assertGreaterEqual(st["n"], 5)
            self.assertEqual(st["t"], sorted(st["t"]))
            path, ctype = s.sheet_path(vid, 0)
            self.assertTrue(path and os.path.getsize(path) > 0)
            self.assertIn(ctype, ("image/webp", "image/jpeg"))
            self.assertEqual(st["pages"], 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
