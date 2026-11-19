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


class SameCodeInBatchTest(unittest.TestCase):
    """Two download folders with one code (a re-download, or a step-1 rename collision whose
    loser kept its raw name) both used to get MOVE: the library index is built before the
    batch is read, so the second copy landed in the library under its raw name."""

    RAW = "(無修正) FC2 PPV 1234567 11月限定【モ無し】テスト"
    CODE = "FC2-PPV-1234567"

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.src = os.path.join(self.tmp, "downloads")
        self.lib = os.path.join(self.tmp, "library")
        os.makedirs(self.src)
        os.makedirs(self.lib)
        self.saved = (migrate.COMPLETED_SPARE, migrate.FC2_PPV, os.environ.get("VAULT_DB"))
        migrate.COMPLETED_SPARE, migrate.FC2_PPV = self.src, self.lib
        os.environ["VAULT_DB"] = os.path.join(self.tmp, "no-catalog.db")   # never the real one
        migrate.AMBIGUOUS.clear()

    def tearDown(self):
        migrate.COMPLETED_SPARE, migrate.FC2_PPV, db = self.saved
        if db is None:
            os.environ.pop("VAULT_DB", None)
        else:
            os.environ["VAULT_DB"] = db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_migrate(self, apply=True):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            migrate.migrate(apply, 1.0, 0, False)
        return out.getvalue()

    def test_identical_copies_land_once_under_the_clean_name(self):
        helpers.movie(self.src, self.CODE, name=self.RAW, size=4000)
        helpers.movie(self.src, self.CODE, size=4000)
        out = self.run_migrate()
        self.assertEqual(sorted(os.listdir(self.lib)), [self.CODE], out)
        self.assertEqual(os.listdir(self.src), [], "the duplicate download must be removed")
        self.assertIn("same code as %s in this batch" % self.CODE, out)

    def test_a_different_rip_is_left_for_a_human(self):
        helpers.movie(self.src, self.CODE, name=self.RAW, size=4000)
        helpers.movie(self.src, self.CODE, size=2000)
        out = self.run_migrate()
        self.assertEqual(len(os.listdir(self.lib)), 1, out)
        self.assertEqual(len(os.listdir(self.src)), 1, "the other rip must stay in downloads")
        self.assertIn("different size", out)

    def test_dry_run_changes_nothing(self):
        helpers.movie(self.src, self.CODE, name=self.RAW, size=4000)
        helpers.movie(self.src, self.CODE, size=4000)
        out = self.run_migrate(apply=False)
        self.assertEqual(os.listdir(self.lib), [])
        self.assertEqual(len(os.listdir(self.src)), 2)
        self.assertIn("MOVE", out)
        self.assertIn("same code as %s in this batch" % self.CODE, out)

    def test_keeper_choice(self):
        c = self.CODE
        # within tolerance: the clean name wins even when the raw copy is a few bytes bigger
        keep, dups, skips = migrate.pick_keepers([(c, self.RAW, 1005), (c, c, 1000)], 1.0)
        self.assertEqual([k[1] for k in keep], [c])
        self.assertEqual(dups, {self.RAW: c})
        self.assertEqual(skips, [])
        # an unreadable size: nothing of that code is moved or deleted
        keep, dups, skips = migrate.pick_keepers([(c, self.RAW, None), (c, c, 1000)], 1.0)
        self.assertEqual((keep, dups), ([], {}))
        self.assertEqual(len(skips), 2)
        # a single folder per code is untouched
        keep, dups, skips = migrate.pick_keepers([(c, c, 1000)], 1.0)
        self.assertEqual(([k[1] for k in keep], dups, skips), ([c], {}, []))

    def test_an_existing_library_copy_still_wins(self):
        helpers.movie(self.lib, self.CODE, size=4000)
        helpers.movie(self.src, self.CODE, name=self.RAW, size=4000)
        helpers.movie(self.src, self.CODE, size=4000)
        out = self.run_migrate()
        self.assertEqual(os.listdir(self.lib), [self.CODE], out)
        self.assertEqual(os.listdir(self.src), [], out)


class TrashIsNotLibraryTest(unittest.TestCase):
    """The library index walked into _vault_trash as if it were an actress folder: a movie
    deleted in Vault still counted as 'already in the library', so downloading it again got
    DELETE SOURCE, and a trashed copy beside a live one made the code AMBIGUOUS."""

    CODE = "FC2-PPV-7654321"
    setUp = SameCodeInBatchTest.setUp
    tearDown = SameCodeInBatchTest.tearDown
    run_migrate = SameCodeInBatchTest.run_migrate

    def test_a_movie_only_in_the_trash_is_imported_again(self):
        trashed = helpers.movie(os.path.join(self.lib, "_vault_trash"), self.CODE, size=4000)
        helpers.movie(self.src, self.CODE, size=4000)
        out = self.run_migrate()
        self.assertTrue(os.path.isdir(os.path.join(self.lib, self.CODE)), out)
        self.assertTrue(os.path.isdir(trashed), "the trash must be left alone")
        self.assertEqual(os.listdir(self.src), [], out)

    def test_a_trashed_copy_does_not_make_the_live_one_ambiguous(self):
        helpers.movie(os.path.join(self.lib, "Some Actress"), self.CODE, size=4000)
        helpers.movie(os.path.join(self.lib, "_Vault_Trash"), self.CODE, size=4000)
        helpers.movie(os.path.join(self.lib, "_duplicates"), self.CODE, size=4000)
        index = migrate.build_index(self.lib)
        self.assertEqual(index, {self.CODE: os.path.join(self.lib, "Some Actress", self.CODE)})
        self.assertNotIn(self.CODE, migrate.AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
