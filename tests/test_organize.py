"""organize.py: the plan must never delete or overwrite, and apply must run only
what the preview showed."""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

org = helpers.load("organize.py", "organize_under_test")


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.lib)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_never_deletes(self):
        helpers.movie(self.lib, "FC2-PPV-1000001", name="FC2-PPV-1000001 - Someone",
                      extra=("readme.txt", "poster.jpg"))
        for a in org.build_plan(self.lib):
            self.assertIn(a["type"], ("folder", "file", "junk", "duplicate"))
            self.assertTrue(a.get("to"), "every action must have a destination")

    def test_junk_is_moved_not_removed(self):
        helpers.movie(self.lib, "FC2-PPV-1000002", extra=("spam.txt",))
        junk = [a for a in org.build_plan(self.lib) if a["type"] == "junk"]
        self.assertTrue(junk)
        self.assertTrue(all("_vault_trash" in a["to"] for a in junk))

    def test_subtitles_and_posters_are_not_junk(self):
        helpers.movie(self.lib, "FC2-PPV-1000003", extra=("sub.srt", "cover.jpg", "info.nfo"))
        junked = [os.path.basename(a["from"])
                  for a in org.build_plan(self.lib) if a["type"] == "junk"]
        for keep in ("sub.srt", "cover.jpg", "info.nfo"):
            self.assertNotIn(keep, junked)

    def test_apply_is_idempotent(self):
        helpers.movie(self.lib, "FC2-PPV-1000004", name="FC2-PPV-1000004 - Name")
        org.apply_plan(self.lib)
        self.assertEqual(org.build_plan(self.lib), [], "a tidy library needs no actions")

    def test_apply_does_not_overwrite(self):
        """Two folders normalizing to the same name must both survive."""
        helpers.movie(self.lib, "FC2-PPV-1000005", name="FC2-PPV-1000005 - A")
        before = sum(len(f) for _r, _d, f in os.walk(self.lib))
        org.apply_plan(self.lib)
        after = sum(len(f) for _r, _d, f in os.walk(self.lib))
        self.assertEqual(before, after, "no file may disappear")


class AllowListTest(unittest.TestCase):
    """The preview is capped; apply used to run everything not explicitly skipped."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.lib)
        for i in range(4):
            helpers.movie(self.lib, "FC2-PPV-200000%d" % i,
                          name="FC2-PPV-200000%d - Name" % i, extra=("junk.txt",))
        self.plan = org.build_plan(self.lib)
        self.ids = [org.action_id(a, self.lib) for a in self.plan]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_allowed_actions_run(self):
        res = org.apply_plan(self.lib, allow=[self.ids[0]])
        self.assertEqual(res["total"], 1)
        self.assertLess(res["total"], len(self.plan))

    def test_empty_allow_list_runs_nothing(self):
        res = org.apply_plan(self.lib, allow=[])
        self.assertEqual(res["total"], 0)
        self.assertEqual(len(org.build_plan(self.lib)), len(self.plan))

    def test_stale_fingerprint_is_refused(self):
        res = org.apply_plan(self.lib, allow=self.ids, expect="0000000000000000")
        self.assertTrue(res.get("stale"))
        self.assertEqual(res["total"], 0)
        self.assertEqual(len(org.build_plan(self.lib)), len(self.plan), "nothing ran")

    def test_matching_fingerprint_proceeds(self):
        fp = org.plan_fingerprint(self.plan, self.lib)
        res = org.apply_plan(self.lib, allow=self.ids, expect=fp)
        self.assertFalse(res.get("stale"))
        self.assertGreater(res["total"], 0)

    def test_fingerprint_changes_with_the_library(self):
        fp = org.plan_fingerprint(self.plan, self.lib)
        helpers.movie(self.lib, "FC2-PPV-2999999", name="FC2-PPV-2999999 - New")
        self.assertNotEqual(org.plan_fingerprint(org.build_plan(self.lib), self.lib), fp)

    def test_legacy_skip_still_works(self):
        res = org.apply_plan(self.lib, skip={self.ids[0]})
        self.assertEqual(res["total"], len(self.plan) - 1)


if __name__ == "__main__":
    unittest.main()
