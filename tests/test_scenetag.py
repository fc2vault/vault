"""scenetag.py: the bundled tagger's launcher, the setup's pip steps, and a tagging run (fake tagger)."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

scenetag = helpers.load("webui/scenetag.py", "scenetag_under_test")

FAKE = r'''
import json, sys, time
a = sys.argv[1:]
video = a[0]
if "slow" in video:
    time.sleep(30)
open(video.rsplit(".", 1)[0] + ".scenes.json", "w").write('{"schema": "vault-scenes/1"}')
json.dump({"processed": 1}, open(a[a.index("--summary-json") + 1], "w"))
assert a[a.index("--engine") + 1] == "phar"
'''


class LauncherTest(unittest.TestCase):
    def test_tagger_ships_with_vault(self):
        # dev tree: the vault-tagger checkout next to vault/; release: scene_tagger.py next to serve.py
        path = scenetag.tagger_script()
        self.assertTrue(path and os.path.isfile(path), path)

    def test_pip_steps(self):
        with mock.patch.object(scenetag.platform, "system", return_value="Darwin"), \
             mock.patch.object(scenetag.platform, "machine", return_value="arm64"):
            self.assertEqual(scenetag.pip_steps(), [["torch"], ["transformers", "numpy"]])
            self.assertEqual(scenetag.hardware_note(), "")
        with mock.patch.object(scenetag.platform, "system", return_value="Windows"), \
             mock.patch.object(scenetag.platform, "machine", return_value="AMD64"), \
             mock.patch.object(scenetag.shutil, "which", return_value="C:/nvidia-smi.exe"):
            self.assertIn("https://download.pytorch.org/whl/cu126", scenetag.pip_steps()[0])
        with mock.patch.object(scenetag.platform, "system", return_value="Windows"), \
             mock.patch.object(scenetag.platform, "machine", return_value="AMD64"), \
             mock.patch.object(scenetag.shutil, "which", return_value=None):
            self.assertEqual(scenetag.pip_steps()[0], ["torch"])
            self.assertIn("processor", scenetag.hardware_note())
        with mock.patch.object(scenetag.platform, "system", return_value="Linux"), \
             mock.patch.object(scenetag.shutil, "which", return_value=None):
            self.assertIn("https://download.pytorch.org/whl/cpu", scenetag.pip_steps()[0])

    def test_not_set_up(self):
        self.assertFalse(scenetag.check("/nonexistent/python")["ready"])
        self.assertIsNone(scenetag.command("/nonexistent/python"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vaulttest-tag-")
        self.script = os.path.join(self.dir, "fake_tagger.py")
        with open(self.script, "w") as f:
            f.write(FAKE)
        self.patch = mock.patch.object(scenetag, "tagger_script", return_value=self.script)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        scenetag.stop()
        for _ in range(100):
            if not scenetag.JOB["running"]:
                break
            time.sleep(0.1)
        shutil.rmtree(self.dir, ignore_errors=True)

    def video(self, name):
        p = os.path.join(self.dir, name)
        open(p, "wb").close()
        return p

    def wait(self, t=30):
        end = time.time() + t
        while scenetag.JOB["running"] and time.time() < end:
            time.sleep(0.1)

    def test_tags_each_video(self):
        vids = [self.video("A.mp4"), self.video("B.mp4")]
        seen = []
        self.assertTrue(scenetag.start(sys.executable, vids, os.path.join(self.dir, "t.log"), seen.append))
        self.assertFalse(scenetag.start(sys.executable, vids, os.path.join(self.dir, "t.log")))   # one at a time
        self.wait()
        snap = scenetag.snapshot()
        self.assertEqual((snap["done"], snap["total"], snap["errors"]), (2, 2, []))
        self.assertTrue(snap["line"].startswith("Done: 2 movies"))
        self.assertEqual(seen, vids)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "A.scenes.json")))

    def test_stop(self):
        vids = [self.video("slow.mp4"), self.video("C.mp4")]
        scenetag.start(sys.executable, vids, os.path.join(self.dir, "t.log"))
        time.sleep(1)
        t0 = time.time()
        scenetag.stop()
        self.wait()
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(scenetag.snapshot()["line"].startswith("Stopped after 0"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "C.scenes.json")))


if __name__ == "__main__":
    unittest.main()
