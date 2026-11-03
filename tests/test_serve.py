"""serve.py: the filesystem walk, the index guard, per-folder ids and the URL guard."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

# serve.py reads VAULT_DATA at import time; point it at a scratch dir so the real
# cache and catalog are never touched.
_DATA = tempfile.mkdtemp(prefix="vaulttest-data-")
os.environ["VAULT_DATA"] = _DATA
serve = helpers.load("webui/serve.py", "serve_under_test")


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


class WalkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.lib)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def codes(self):
        return {it["code"] for it in serve.scan_library(self.lib)}

    def test_finds_a_movie(self):
        helpers.movie(self.lib, "FC2-PPV-3000001")
        self.assertEqual(self.codes(), {"FC2-PPV-3000001"})

    def test_skips_reserved_folders_case_insensitively(self):
        """organize.py creates '_Duplicates' with a capital D; the skip-set is lowercase."""
        helpers.movie(self.lib, "FC2-PPV-3000002")
        for reserved in ("_Duplicates", "_duplicates", "_vault_trash", "@eaDir"):
            helpers.movie(os.path.join(self.lib, reserved), "FC2-PPV-3000003")
        self.assertEqual(self.codes(), {"FC2-PPV-3000002"})

    def test_walk_errors_are_reported(self):
        helpers.movie(self.lib, "FC2-PPV-3000004")
        bad = os.path.join(self.lib, "locked")
        os.makedirs(bad)
        helpers.movie(bad, "FC2-PPV-3000005")
        os.chmod(bad, 0o000)
        try:
            serve.scan_library(self.lib)
            self.assertGreater(serve.LAST_WALK["errors"], 0)
        finally:
            os.chmod(bad, 0o755)

    def test_missing_root_is_flagged(self):
        serve.scan_library(os.path.join(self.tmp, "does-not-exist"))
        self.assertTrue(serve.LAST_WALK["missing_root"])


class IndexGuardTest(unittest.TestCase):
    """A short or failed walk must never replace a good index."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.lib)
        for i in range(30):
            helpers.movie(self.lib, "FC2-PPV-40000%02d" % i)
        serve.STATE["library"] = self.lib
        serve.STATE["items"] = []
        serve.rebuild_index()
        self.baseline = len(serve.STATE["items"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_baseline(self):
        self.assertEqual(self.baseline, 30)

    def test_unmounted_library_keeps_the_index(self):
        os.rename(self.lib, self.lib + "-gone")
        try:
            serve.rebuild_index()
            self.assertEqual(len(serve.STATE["items"]), self.baseline)
            self.assertIn("not there", serve.STATE.get("scan_warning", ""))
        finally:
            os.rename(self.lib + "-gone", self.lib)

    def test_a_mostly_empty_walk_keeps_the_index(self):
        hidden = os.path.join(self.tmp, "hidden")
        os.makedirs(hidden)
        moved = sorted(os.listdir(self.lib))[:25]
        for d in moved:
            shutil.move(os.path.join(self.lib, d), os.path.join(hidden, d))
        try:
            serve.rebuild_index()
            self.assertEqual(len(serve.STATE["items"]), self.baseline)
            self.assertIn("walk found", serve.STATE.get("scan_warning", ""))
        finally:
            for d in moved:
                shutil.move(os.path.join(hidden, d), os.path.join(self.lib, d))

    def test_a_real_deletion_is_accepted(self):
        for d in sorted(os.listdir(self.lib))[:3]:
            shutil.rmtree(os.path.join(self.lib, d))
        serve.rebuild_index()
        self.assertEqual(len(serve.STATE["items"]), self.baseline - 3)
        self.assertEqual(serve.STATE.get("scan_warning"), "")


class UidTest(unittest.TestCase):
    """One code in two folders must still address one folder at a time."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.lib = os.path.join(self.tmp, "lib")
        helpers.movie(os.path.join(self.lib, "ActressA"), "FC2-PPV-5000001", size=1024)
        helpers.movie(os.path.join(self.lib, "ActressB"), "FC2-PPV-5000001", size=4096)
        helpers.movie(os.path.join(self.lib, "Solo"), "FC2-PPV-5000002")
        serve.STATE["library"] = self.lib
        serve.STATE["items"] = []
        serve.rebuild_index()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unique_code_keeps_its_code_as_uid(self):
        solo = [i for i in serve.STATE["items"] if i["code"] == "FC2-PPV-5000002"][0]
        self.assertEqual(solo["uid"], solo["code"])

    def test_duplicates_get_distinct_uids(self):
        dups = [i for i in serve.STATE["items"] if i["code"] == "FC2-PPV-5000001"]
        self.assertEqual(len(dups), 2)
        self.assertEqual(len({d["uid"] for d in dups}), 2)
        self.assertTrue(all(d["uid"].startswith("FC2-PPV-5000001~") for d in dups))

    def test_each_uid_points_at_its_own_file(self):
        dups = [i for i in serve.STATE["items"] if i["code"] == "FC2-PPV-5000001"]
        paths = {serve.STATE["by_uid"][d["uid"]]["primary"] for d in dups}
        self.assertEqual(len(paths), 2)

    def test_uid_is_stable_across_rebuilds(self):
        before = {i["uid"] for i in serve.STATE["items"]}
        serve.rebuild_index()
        self.assertEqual({i["uid"] for i in serve.STATE["items"]}, before)


class PrivateUrlTest(unittest.TestCase):
    """The resolver URL must stay on this network: the server has no auth."""

    def test_private_and_loopback_allowed(self):
        for url in ("http://127.0.0.1:8191", "http://10.1.2.3:30098",
                    "http://192.168.1.5:8191", "http://localhost:8191"):
            self.assertTrue(serve._is_private_url(url), url)

    def test_public_refused(self):
        for url in ("http://1.2.3.4:8191", "https://example.com/v1", "http://8.8.8.8"):
            self.assertFalse(serve._is_private_url(url), url)

    def test_garbage_refused(self):
        for url in ("", "not a url", "http://"):
            self.assertFalse(serve._is_private_url(url))


if __name__ == "__main__":
    unittest.main()


class TrashMoveTest(unittest.TestCase):
    """A trash move must be a rename or an honest error — never a silent copy.

    What went wrong: the SMB library refuses to rename a folder while a file inside it is
    open (EACCES), which is exactly the case when you delete the movie you are watching.
    shutil.move() swallowed that and copied 2.7 GB instead; the request hung for minutes, a
    second click started a SECOND copy under a timestamped name, and the trash ended up
    holding the same movie twice."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.src = os.path.join(self.tmp, "FC2-PPV-9000001")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "FC2-PPV-9000001.mp4"), "wb") as fh:
            fh.write(b"\0" * 64)
        self.dst = os.path.join(self.tmp, "_vault_trash", "FC2-PPV-9000001")
        os.makedirs(os.path.dirname(self.dst))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_plain_move_is_a_rename(self):
        serve._rename_or_explain(self.src, self.dst)
        self.assertFalse(os.path.exists(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "FC2-PPV-9000001.mp4")))

    def test_an_open_file_is_reported_not_copied(self):
        import errno as _errno
        copied = []
        real_rename, real_move = os.rename, shutil.move
        os.rename = self._raise(_errno.EACCES)
        shutil.move = lambda *a, **k: copied.append(a)
        try:
            with self.assertRaises(OSError) as cm:
                serve._rename_or_explain(self.src, self.dst)
        finally:
            os.rename, shutil.move = real_rename, real_move
        self.assertIn("still open", str(cm.exception))
        self.assertEqual(copied, [])              # the 2.7 GB copy must not happen
        self.assertTrue(os.path.exists(self.src))  # and the movie stays put

    def test_a_real_cross_volume_move_still_copies(self):
        """EXDEV is the one case where copying is the only option."""
        import errno as _errno
        copied = []
        real_rename, real_move = os.rename, shutil.move
        os.rename = self._raise(_errno.EXDEV)
        shutil.move = lambda *a, **k: copied.append(a)
        try:
            serve._rename_or_explain(self.src, self.dst)
        finally:
            os.rename, shutil.move = real_rename, real_move
        self.assertEqual(len(copied), 1)

    def test_to_trash_surfaces_the_error_and_keeps_the_file(self):
        import errno as _errno
        serve.STATE["library"] = self.tmp
        real_rename = os.rename
        os.rename = self._raise(_errno.EACCES)
        try:
            moved, err = serve._to_trash([self.src])
        finally:
            os.rename = real_rename
        self.assertEqual(moved, [])
        self.assertIn("still open", err or "")
        self.assertTrue(os.path.isfile(os.path.join(self.src, "FC2-PPV-9000001.mp4")))

    @staticmethod
    def _raise(code):
        def boom(*_a, **_k):
            raise OSError(code, os.strerror(code))
        return boom


class Fc2dbPassTest(unittest.TestCase):
    """fc2ppv-db is the PRIMARY source for the cast, so "still to do" means "page never
    read" — not "has no performer". The old rule made it the last resort: a movie named by
    its folder was skipped forever, and never got that page's co-stars, tags, cover or date.
    The lookup is ~3 s each, so re-running must still skip pages already seen."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.con.execute("ALTER TABLE works ADD COLUMN fc2db_at TEXT")
        self.con.execute("INSERT INTO actresses(id,name_en) VALUES(7,'Someone')")
        # 1: read already. 2: untouched. 3: HAS an actress but was never read — the case
        # the old rule dropped on the floor.
        for code, aid, at in (("FC2-PPV-1", None, "2026-10-09"),
                              ("FC2-PPV-2", None, None),
                              ("FC2-PPV-3", 7, None)):
            self.con.execute(
                "INSERT INTO works(code,actress_id,fc2db_at,in_library,availability,source) "
                "VALUES(?,?,?,1,'available','fs')", (code, aid, at))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_pages_already_read(self):
        got = serve.Handler._codes_needing_fc2db(
            self.con, ["FC2-PPV-1", "FC2-PPV-2", "FC2-PPV-3"])
        self.assertNotIn("FC2-PPV-1", got)

    def test_still_visits_a_movie_that_already_has_an_actress(self):
        got = serve.Handler._codes_needing_fc2db(self.con, ["FC2-PPV-3"])
        self.assertEqual(got, ["FC2-PPV-3"])

    def test_keeps_a_code_the_catalog_has_never_seen(self):
        got = serve.Handler._codes_needing_fc2db(self.con, ["FC2-PPV-9"])
        self.assertEqual(got, ["FC2-PPV-9"])

    def test_handles_more_codes_than_sqlite_takes_variables(self):
        """SQLite caps host parameters per statement; a whole-library scan passes 4,000+."""
        codes = ["FC2-PPV-9%04d" % n for n in range(3000)]   # distinct from setUp's rows
        self.con.execute("INSERT INTO works(code,fc2db_at,in_library,availability,source)"
                         " VALUES('FC2-PPV-91234','2026-10-09',1,'available','fs')")
        self.con.commit()
        got = serve.Handler._codes_needing_fc2db(self.con, codes)
        self.assertNotIn("FC2-PPV-91234", got)
        self.assertEqual(len(got), len(codes) - 1)


class NeedsReviewSkipTest(unittest.TestCase):
    """_needs_review is a holding pen for loose files, not a release. The walk indexed it as
    one movie with the code "_needs_review", which became a catalog row and a wasted
    fc2ppv-db lookup on every scan."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(self.lib)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_needs_review_is_not_a_movie(self):
        helpers.movie(self.lib, "FC2-PPV-3000010")
        helpers.movie(os.path.join(self.lib, "_needs_review"), "loose", name="clip.mp4")
        codes = {it["code"] for it in serve.scan_library(self.lib)}
        self.assertEqual(codes, {"FC2-PPV-3000010"})

    def test_loose_files_directly_inside_it_are_skipped_too(self):
        """The real folder holds videos at its own top level, not in sub-folders."""
        d = os.path.join(self.lib, "_needs_review")
        os.makedirs(d)
        with open(os.path.join(d, "[FC2-PPV] - 007.mp4"), "wb") as fh:
            fh.write(b"\0" * 4096)
        helpers.movie(self.lib, "FC2-PPV-3000011")
        codes = {it["code"] for it in serve.scan_library(self.lib)}
        self.assertEqual(codes, {"FC2-PPV-3000011"})

    def test_the_organizer_leaves_it_alone(self):
        organize = helpers.load("organize.py", "organize_skip_test")
        self.assertIn("_needs_review", organize.SPECIAL_DIRS)


class ScanSlotTest(unittest.TestCase):
    """_fc2db_scan claimed the scan slot and then returned 400/503 without releasing it.
    PROGRESS stayed "running" forever, so every later scan, wishlist backfill and scan
    preview answered "a scan is already running" until the process was restarted."""

    def setUp(self):
        serve.release_scan()

    def tearDown(self):
        serve.release_scan()

    def test_claim_then_release_frees_the_slot(self):
        self.assertTrue(serve.claim_scan("x"))
        self.assertFalse(serve.claim_scan("y"), "the slot must be exclusive")
        serve.release_scan()
        self.assertTrue(serve.claim_scan("z"), "release must hand the slot back")

    def test_the_settings_scan_cannot_leak_the_slot(self):
        """Walk the source: every check that can refuse runs BEFORE claim_scan, so once the
        slot is taken the only way out is the pipeline, whose worker releases it. (The old
        handler claimed first and then returned 400/503 — that is the leak.)"""
        import inspect
        src = inspect.getsource(serve.Handler._scan_everything)
        after = src.split("claim_scan(", 1)[1].split("_start_pipeline(", 1)[0]
        plain = [l.strip() for l in after.splitlines() if "return self._json" in l]
        self.assertEqual(plain, ['return self._json({"error": "a scan is already running"}, 409)'])

    def test_every_settings_scan_route_runs_the_full_pipeline(self):
        import inspect
        src = inspect.getsource(serve.Handler._post_routes)
        line = [l for l in src.splitlines() if "/api/fc2db/scan" in l][0]
        self.assertIn("/api/fc2dbnet/scan", line)
        self.assertIn("/api/scanall", line)


class FoldAllowListTest(unittest.TestCase):
    """Organize was switched to an allow-list; fold-by-actress was not, so past ORG_CAP the
    rows the dialog never rendered moved anyway."""

    def test_fold_accepts_an_allow_list(self):
        import inspect
        sig = inspect.signature(serve.Handler._fold_actress)
        self.assertIn("allow", sig.parameters)
        src = inspect.getsource(serve.Handler._fold_actress)
        self.assertIn("allow is not None and e[\"id\"] not in allow", src)


class Review3ServeTest(unittest.TestCase):
    def test_workers_publish_the_index_before_saying_done(self):
        """Both scan workers lowered `running` and only THEN rebuilt the index, so the
        poller's reloadData() got the old grid. In each worker the rebuild must come first."""
        import inspect
        # every scan button now runs one pipeline; its worker is the only one that says "done"
        src = inspect.getsource(serve.Handler._start_pipeline)
        body = src[src.index("def run()"):]
        rebuild = body.index("rebuild_index(")
        lowered = body.index('P["running"] = False')
        self.assertLess(rebuild, lowered, "the pipeline says done before it publishes")
        for fn in (serve.Handler._scan_library, serve.Handler._scan_everything):
            self.assertIn("_start_pipeline(", inspect.getsource(fn), fn.__name__)

    def test_the_pipeline_runs_all_three_passes(self):
        """'Every scan button checks every source' — pinned on the one place that scans."""
        import inspect
        src = inspect.getsource(serve.Handler._start_pipeline)
        for call in ("scan_library(", "scan_fc2db(", "scan_fc2dbnet("):
            self.assertIn(call, src)
        player = inspect.getsource(serve.Handler._scan_one)
        for call in ("scan_code(", "scan_fc2db(", "scan_fc2dbnet("):
            self.assertIn(call, player)

    def test_the_player_scan_takes_the_slot_before_writing(self):
        """It skipped only the fc2ppv-db half during a library scan and still wrote the
        shell pass alongside it. The slot must be claimed before scan_code runs."""
        import inspect
        src = inspect.getsource(serve.Handler._scan_one)
        self.assertLess(src.index("claim_scan("), src.index("scan_code("))
        self.assertIn("409", src)

    def test_a_new_builtin_source_reaches_an_old_sources_json(self):
        """An install saved before fc2db.net existed would otherwise never list it — and a
        source missing from the list is OFF."""
        import json
        old = [dict(d) for d in serve.DEFAULT_SOURCES if d["name"] != "fc2db.net"]
        with open(serve.SOURCES_CFG, "w") as fh:
            json.dump(old, fh)
        try:
            self.assertIn("fc2db.net", serve.enabled_sources())
        finally:
            os.remove(serve.SOURCES_CFG)


class SourcePriorityTest(unittest.TestCase):
    """fc2ppv-db > fc2db.net > 123av/ffjav. Two of those only fill EMPTY fields, so the
    priority is decided by pass order: with the shell pass first (as it was), ffjav's
    search-page cover and date always beat fc2db.net's official ones."""

    def order(self, src, calls):
        return [src.index(c) for c in calls]

    def test_the_pipeline_runs_in_priority_order(self):
        import inspect
        src = inspect.getsource(serve.Handler._start_pipeline)
        src = src[src.index("def run()"):]
        a, b, c = self.order(src, ["scanmod.scan_fc2db(", "scanmod.scan_fc2dbnet(",
                                   "scanmod.scan_library("])
        self.assertTrue(a < b < c, "passes must run fc2ppv-db, fc2db.net, then 123av/ffjav")

    def test_the_player_runs_in_priority_order_and_never_forces_the_shell(self):
        import inspect
        src = inspect.getsource(serve.Handler._scan_one)
        a, b, c = self.order(src, ["scanmod.scan_fc2db(", "scanmod.scan_fc2dbnet(",
                                   "scanmod.scan_code("])
        self.assertTrue(a < b < c)
        shell = src[src.index("scanmod.scan_code("):].split(")", 1)[0]
        self.assertIn("force=False", shell, "the lowest-priority source must not overwrite")
