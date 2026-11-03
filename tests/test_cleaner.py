"""rename_fc2 / refolder_fc2 / migrate_fc2: the paths that can destroy files."""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

rename = helpers.load("rename_fc2.py", "rename_under_test")
migrate = helpers.load("migrate_fc2.py", "migrate_under_test")


class KeepSetTest(unittest.TestCase):
    """The download cleaner deletes junk, but companions belong to the release."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cleaning_keeps_companions_and_removes_junk(self):
        """The real thing: a download folder with a sidecar, a subtitle, a poster,
        a spam file and an ad subfolder."""
        d = helpers.movie(self.tmp, "FC2-PPV-6000001", name="FC2-PPV-6000001",
                          size=200 * 1024 * 1024,
                          extra=("FC2-PPV-6000001.scenes.json", "FC2-PPV-6000001.srt",
                                 "cover.jpg", "spam.txt", ".DS_Store"))
        ads = os.path.join(d, "ads")
        os.makedirs(ads)
        open(os.path.join(ads, "ad.html"), "w").write("x")
        runner = rename.Runner(apply=True)
        rename.process_folder(d, "FC2-PPV-6000001", runner, 80 * 1024 * 1024)
        left = set()
        for root, _dirs, files in os.walk(self.tmp):
            left.update(files)
        for keep in ("FC2-PPV-6000001.scenes.json", "FC2-PPV-6000001.srt", "cover.jpg"):
            self.assertIn(keep, left, "%s must survive the cleaner" % keep)
        for gone in ("spam.txt", "ad.html", ".DS_Store"):
            self.assertNotIn(gone, left, "%s should have been removed" % gone)

    def test_a_subfolder_holding_a_video_is_kept(self):
        d = helpers.movie(self.tmp, "FC2-PPV-6000009", name="FC2-PPV-6000009",
                          size=200 * 1024 * 1024)
        part2 = os.path.join(d, "part2")
        os.makedirs(part2)
        open(os.path.join(part2, "FC2-PPV-6000009-2.mp4"), "wb").write(b"\0" * (150 * 1024 * 1024))
        rename.process_folder(d, "FC2-PPV-6000009", rename.Runner(apply=True),
                              80 * 1024 * 1024)
        self.assertTrue(os.path.isdir(part2) or
                        os.path.isdir(os.path.join(self.tmp, "FC2-PPV-6000009", "part2")),
                        "a subfolder with a real video must not be deleted")

    def test_a_folder_with_a_video_is_not_disposable(self):
        d = helpers.movie(self.tmp, "FC2-PPV-6000001")
        self.assertFalse(rename._dir_is_disposable(d))

    def test_a_folder_with_only_a_sidecar_is_not_disposable(self):
        d = os.path.join(self.tmp, "sidecars")
        os.makedirs(d)
        open(os.path.join(d, "x.scenes.json"), "w").write("{}")
        self.assertFalse(rename._dir_is_disposable(d))

    def test_an_ad_folder_is_disposable(self):
        d = os.path.join(self.tmp, "ads")
        os.makedirs(d)
        open(os.path.join(d, "ad.html"), "w").write("x")
        open(os.path.join(d, ".DS_Store"), "w").write("x")
        self.assertTrue(rename._dir_is_disposable(d))


class RefolderGuardTest(unittest.TestCase):
    """refolder_fc2 cleans every folder it processes, so it must not default to the
    library and must refuse to be pointed at it."""

    def run_it(self, *args):
        return subprocess.run([sys.executable, os.path.join(helpers.ROOT, "refolder_fc2.py")]
                              + list(args), capture_output=True, text=True)

    def test_no_argument_is_refused(self):
        r = self.run_it()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("give the folder", (r.stdout + r.stderr))

    def test_configured_library_is_refused(self):
        import json
        cfg = os.path.join(helpers.ROOT, "webui", "config.json")
        if not os.path.exists(cfg):
            self.skipTest("no config.json in this checkout")
        with open(cfg, encoding="utf-8") as fh:
            lib = (json.load(fh).get("library") or "").rstrip("/")
        if not lib or not os.path.isdir(lib):
            self.skipTest("configured library is not mounted")
        r = self.run_it(lib)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to run on the Vault library", (r.stdout + r.stderr))


class FolderSizeTest(unittest.TestCase):
    """An unreadable file must make a size UNKNOWN, not smaller: a short total used to
    select REPLACE TARGET and delete the good library copy."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_folder_has_a_size(self):
        d = helpers.movie(self.tmp, "FC2-PPV-6000002", size=1000)
        self.assertEqual(migrate.folder_size(d), 1000)

    def test_unreadable_subfolder_makes_it_unknown(self):
        d = helpers.movie(self.tmp, "FC2-PPV-6000003", size=1000)
        locked = os.path.join(d, "locked")
        os.makedirs(locked)
        open(os.path.join(locked, "part.mp4"), "wb").write(b"\0" * 500)
        os.chmod(locked, 0o000)
        try:
            self.assertIsNone(migrate.folder_size(d))
        finally:
            os.chmod(locked, 0o755)


class MoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_move_raises_on_collision(self):
        """Returning quietly marked the row a success and the catalog then claimed the
        movie was in the library."""
        src = helpers.movie(self.tmp, "FC2-PPV-6000004", name="src")
        dst = helpers.movie(self.tmp, "FC2-PPV-6000004", name="dst")
        with self.assertRaises(Exception):
            migrate._move(src, dst)
        self.assertTrue(os.path.isdir(src), "the source must survive a refused move")

    def test_move_relocates(self):
        src = helpers.movie(self.tmp, "FC2-PPV-6000005", name="src")
        dst = os.path.join(self.tmp, "dst")
        migrate._move(src, dst)
        self.assertTrue(os.path.exists(os.path.join(dst, "FC2-PPV-6000005.mp4")))
        self.assertFalse(os.path.exists(src))


if __name__ == "__main__":
    unittest.main()
