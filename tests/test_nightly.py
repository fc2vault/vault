"""nightly.py: the window, the settings, and each step against a fake host (no real library)."""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

nightly = helpers.load("webui/nightly.py", "nightly_under_test")


class FakeHost:
    def __init__(self, root):
        self.root = root
        self.cfg = {"nightly": {}}
        self._items = []
        self.imports = []
        self.meta_asked = []
        self.meta_finds = set()          # codes the "sources" know
        self.queued = []
        self.cancelled = 0

    def config(self):
        return self.cfg

    def items(self):
        return self._items

    def library(self):
        return self.root

    def item_parts(self, it):
        return it.get("parts") or []

    def log_path(self, name):
        return os.path.join(self.root, name)

    def run_import(self, replace, keep_dupes):
        self.imports.append((replace, keep_dupes))
        return "nothing new"

    def start_metadata(self, codes):
        self.meta_asked.append(list(codes))
        for it in self._items:
            if it["code"] in codes and it["code"] in self.meta_finds:
                it["title_en"] = "found"
        return True, ""

    def metadata_running(self):
        return False

    def whisper_python(self):
        return ""

    def subs_ready(self):
        return ""

    def subs_busy(self):
        return False

    def subs_enqueue(self, jobs):
        self.queued.extend(jobs)
        return len(jobs)

    def subs_cancel(self):
        self.cancelled += 1

    def subs_finished(self):
        return len(self.queued)

    # scrub previews: a video "needs" one until it was queued once
    favs = set()
    scrubbed = None

    def favourites(self):
        return self.favs

    def scrub_needed(self, path):
        return path not in (self.scrubbed or [])

    def scrub_enqueue(self, paths):
        self.scrubbed = (self.scrubbed or []) + list(paths)
        return len(paths)

    def scrub_busy(self):
        return False

    def scrub_stop(self):
        self.cancelled += 1


def movie(root, code, added, srt=False, scenes=False):
    d = os.path.join(root, code)
    os.makedirs(d, exist_ok=True)
    vp = os.path.join(d, code + ".mp4")
    open(vp, "wb").close()
    if srt:
        open(os.path.join(d, code + ".en.srt"), "w").close()
    if scenes:
        open(os.path.join(d, code + ".scenes.json"), "w").close()
    return {"code": code, "added": added, "parts": [{"path": vp, "name": code + ".mp4"}]}


class WindowTest(unittest.TestCase):
    def at(self, h, m=0):
        return datetime(2026, 11, 20, h, m)

    def test_same_day_window(self):
        self.assertIsNotNone(nightly.window(self.at(2), "01:00", "06:00"))
        self.assertIsNone(nightly.window(self.at(6), "01:00", "06:00"))
        self.assertIsNone(nightly.window(self.at(0, 59), "01:00", "06:00"))

    def test_window_across_midnight(self):
        ws, we = nightly.window(self.at(23), "22:00", "05:00")
        self.assertEqual((ws.day, ws.hour, we.day, we.hour), (20, 22, 21, 5))
        ws, we = nightly.window(self.at(3), "22:00", "05:00")
        self.assertEqual((ws.day, ws.hour, we.day, we.hour), (19, 22, 20, 5))
        self.assertIsNone(nightly.window(self.at(12), "22:00", "05:00"))
        self.assertEqual(nightly.window_length("22:00", "05:00"), 7 * 3600)

    def test_settings_are_validated(self):
        s = nightly.settings({"nightly": {"start": "25:00", "dupes": "burn", "subs_scope": "x",
                                          "enabled": 1, "bogus": True}})
        self.assertEqual((s["start"], s["dupes"], s["subs_scope"], s["enabled"]),
                         ("01:00", "delete", "new", True))
        self.assertNotIn("bogus", s)
        self.assertEqual(nightly.clean({"start": "3:00", "end": "03:00"}, s)[1],
                         "The window needs an end different from its start")
        self.assertEqual(nightly.clean({"start": "7:5"}, s)[0]["start"], "07:05")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vaulttest-nightly-")
        self.h = FakeHost(self.root)
        self.r = nightly.Runner(self.h, os.path.join(self.root, "nightly.json"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_steps(self, **cfg):
        self.h.cfg = {"nightly": dict({"enabled": True, "metadata": False}, **cfg)}
        self.r.ensure_baseline()
        s = nightly.settings(self.h.cfg)
        self.r.st["last"] = {"started": 0, "ended": 0, "manual": True, "log": []}
        self.r._run(s, time.time() + 3600)
        return [m for _t, m in self.r.st["last"]["log"]]

    def test_once_per_window(self):
        self.h.cfg = {"nightly": {"enabled": True, "start": "01:00", "end": "06:00", "metadata": False}}
        self.assertTrue(self.r.tick(datetime(2026, 11, 20, 2)))
        for _ in range(100):
            if not self.r.status["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(self.r.tick(datetime(2026, 11, 20, 3)))       # same night
        self.assertFalse(self.r.tick(datetime(2026, 11, 20, 12)))      # outside
        self.h.cfg["nightly"]["enabled"] = False
        self.assertFalse(self.r.tick(datetime(2026, 11, 21, 2)))       # switched off

    def test_import_passes_the_choices(self):
        self.run_steps(**{"import": True, "replace": True, "dupes": "keep"})
        self.assertEqual(self.h.imports, [(True, True)])

    def test_metadata_only_new_movies_and_weekly_retry(self):
        base = int(time.time()) - 10
        self.r.st["baseline"] = base
        self.h._items = [{"code": "OLD", "added": base - 100},             # before the switch-on
                         {"code": "NEW1", "added": base + 1},
                         {"code": "NEW2", "added": base + 1},
                         {"code": "HAS", "added": base + 1, "title_en": "x"}]
        self.h.meta_finds = {"NEW1"}
        log = self.run_steps(metadata=True)
        self.assertEqual(self.h.meta_asked, [["NEW1", "NEW2"]])
        self.assertIn("Metadata: found 1 of 2; the rest are asked again in a week", log)
        self.assertEqual(set(self.r.st["meta"]), {"NEW2"})
        log = self.run_steps(metadata=True)                              # the next night
        self.assertEqual(len(self.h.meta_asked), 1)
        self.assertIn("Metadata: nothing new to look up (1 wait for their weekly retry)", log)
        self.r.st["meta"]["NEW2"] -= nightly.META_RETRY                   # a week later
        self.run_steps(metadata=True)
        self.assertEqual(self.h.meta_asked[-1], ["NEW2"])

    def test_subtitles_new_movies_without_srt(self):
        base = int(time.time()) - 10
        self.r.st["baseline"] = base
        self.h._items = [movie(self.root, "A", base + 1), movie(self.root, "B", base + 1, srt=True),
                         movie(self.root, "OLD", base - 100)]
        self.run_steps(subs=True)
        self.assertEqual([j["code"] for j in self.h.queued], ["A"])
        self.h.queued = []
        self.run_steps(subs=True, subs_scope="library")
        self.assertEqual(sorted(j["code"] for j in self.h.queued), ["A", "OLD"])

    def test_scrub_previews_favourites_first_then_newest(self):
        base = int(time.time()) - 10
        self.r.st["baseline"] = base
        self.h._items = [movie(self.root, "OLD", base - 100), movie(self.root, "NEW", base + 5),
                         movie(self.root, "FAV", base - 200), dict(movie(self.root, "GONE", base), missing=True)]
        self.h.favs = {"FAV"}
        log = self.run_steps(scrub=True)                       # default scope: the whole library
        self.assertEqual([os.path.basename(p) for p in self.h.scrubbed], ["FAV.mp4", "NEW.mp4", "OLD.mp4"])
        self.assertIn("Scrub previews: 3 done", log)
        self.assertIn("Scrub previews: every movie has them", self.run_steps(scrub=True))
        self.h.scrubbed = []
        self.run_steps(scrub=True, scrub_scope="new")
        self.assertEqual([os.path.basename(p) for p in self.h.scrubbed], ["NEW.mp4"])

    def test_scene_markers_run_the_tagger_per_new_video(self):
        base = int(time.time()) - 10
        self.r.st["baseline"] = base
        self.h._items = [movie(self.root, "A", base + 1), movie(self.root, "B", base + 1, scenes=True)]
        fake = os.path.join(self.root, "fake-tagger.py")
        with open(fake, "w") as f:
            f.write("import json,sys\nopen(sys.argv[1].rsplit('.',1)[0]+'.scenes.json','w').write('{}')\n"
                    "a=sys.argv; json.dump({'processed':1}, open(a[a.index('--summary-json')+1],'w'))\n")
        cmd = '"%s" "%s"' % (sys.executable, fake)
        log = self.run_steps(scenes=True, tagger=cmd)
        self.assertIn("Scene markers: 1 video tagged", log)
        self.assertTrue(os.path.exists(os.path.join(self.root, "A", "A.scenes.json")))
        log = self.run_steps(scenes=True, tagger=cmd)
        self.assertIn("Scene markers: every new movie has them", log)

    def test_scene_markers_stop_at_the_deadline(self):
        slow = os.path.join(self.root, "slow.py")
        with open(slow, "w") as f:
            f.write("import time\ntime.sleep(60)\n")
        self.h.cfg = {"nightly": {"tagger": '"%s" "%s"' % (sys.executable, slow), "scenes_scope": "library"}}
        self.r.st["last"] = {"started": 0, "ended": 0, "manual": True, "log": []}
        t0 = time.time()
        self.r._scenes(nightly.settings(self.h.cfg), time.time() + 2)
        self.assertLess(time.time() - t0, 30)
        self.assertIn("stopped at the end of the window", self.r.status["line"])

    def test_missing_tagger_is_reported(self):
        log = self.run_steps(scenes=True, tagger="")
        if not shutil.which("vault-scene-tagger"):
            self.assertTrue(any("scene tagger isn't set up" in m for m in log))


if __name__ == "__main__":
    unittest.main()
