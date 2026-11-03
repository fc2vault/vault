"""scan.py: page parsing, censorship, actress matching, ffjav result picking."""
import os
import shutil
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

scan = helpers.load("scan.py", "scan_under_test")


def page(body):
    return "<html>fc2ppv " + body + "</html>"


class ParseTest(unittest.TestCase):
    AGE_GATE = page('<meta property="og:title" content="Age Verification - FC2PPV-DB">'
                    '"mosaicStatus":"NONE" <h5 class="title"><a>looks like a title</a></h5>')

    def test_age_gate_is_detected(self):
        self.assertTrue(scan._is_age_gate(self.AGE_GATE))

    def test_age_gate_yields_nothing(self):
        """The 18+ interstitial must never be stored as movie metadata."""
        self.assertEqual(scan.parse_fc2ppvdb_video(self.AGE_GATE), {})

    def test_cookie_mode_reports_the_age_gate(self):
        """Cookie mode used to parse the gate to {} and count it as a normal miss, so a
        whole-library scan recorded nothing while reporting success."""
        real = scan._get_cf
        scan._get_cf = lambda *a, **k: (self.AGE_GATE, 200)
        try:
            got = scan.fetch_fc2ppvdb("FC2-PPV-1234567", cookie="x", ua="y")
        finally:
            scan._get_cf = real
        self.assertIn("18+", got.get("_error", ""))

    def test_a_404_is_absent_not_an_error(self):
        """A code fc2ppv-db does not carry must read as "not there", never as a transport
        failure: three "failures" in a row abort the scan, and the message blamed Cloudflare
        for movies the site simply never had."""
        real = scan._get_cf
        scan._get_cf = lambda *a, **k: (None, 404)
        try:
            got = scan.fetch_fc2ppvdb("FC2-PPV-1234567", cookie="x", ua="y")
        finally:
            scan._get_cf = real
        self.assertTrue(got.get("_missing"))
        self.assertNotIn("_error", got)

    def test_a_refusal_is_still_an_error(self):
        """No status at all (connection refused, both families) stays a hard error."""
        real = scan._get_cf
        scan._get_cf = lambda *a, **k: (None, None)
        try:
            got = scan.fetch_fc2ppvdb("FC2-PPV-1234567", cookie="x", ua="y")
        finally:
            scan._get_cf = real
        self.assertIn("no response", got.get("_error", ""))
        self.assertFalse(got.get("_missing"))

    def test_solver_404_keeps_the_session(self):
        """solver_get used to call a 404 "Cloudflare check was not solved" and destroy the
        session, so every code the site lacks cost a fresh challenge -- and three in a row
        stopped the scan on a resolver that was working perfectly."""
        killed = []
        real_post, real_kill = scan._solver_post, scan._solver_destroy
        scan._solver_post = lambda ep, payload, timeout: (
            {"status": "ok", "solution": {"status": 200}} if payload.get("cmd") == "sessions.create"
            else {"status": "ok", "solution": {"status": 404, "response": "<html>not found</html>"}})
        scan._solver_destroy = lambda ep: killed.append(ep)
        scan._SOLVER_LIVE["endpoint"] = "http://solver.test/v1"
        try:
            html, err, status = scan.solver_get("http://solver.test", "https://fc2ppv-db.com/x")
        finally:
            scan._solver_post, scan._solver_destroy = real_post, real_kill
            scan._SOLVER_LIVE["endpoint"] = None
        self.assertIsNone(html)
        self.assertEqual(status, 404)
        self.assertEqual(err, "")          # not an error
        self.assertEqual(killed, [])       # and the session survived

    # The real shape of fc2ppv-db's answer for an id it does not have: HTTP 200, the site
    # name as og:title, one shared placeholder image, no video link, no cast, no mosaicStatus.
    NOT_FOUND = page('<meta property="og:title" content="FC2PPV Database">'
                     '<meta property="og:image" '
                     'content="https://fc2ppv-db.com/opengraph-image?0c674a23bfd755fe">')

    def test_soft_404_yields_nothing(self):
        """52 catalog rows ended up titled "FC2PPV Database" with the site's placeholder
        cover, and each one was counted as a successful hit."""
        self.assertEqual(scan.parse_fc2ppvdb_video(self.NOT_FOUND), {})

    def test_soft_404_is_counted_absent_not_found(self):
        d = scan.parse_fc2ppvdb_video(self.NOT_FOUND)
        self.assertFalse(d.get("title_jp") or d.get("actresses"))   # what scan_fc2db tests

    def test_placeholder_cover_is_never_stored(self):
        d = scan.parse_fc2ppvdb_video(
            page('<meta property="og:title" content="FC2-PPV-1234567 A real title">'
                 '<meta property="og:image" '
                 'content="https://fc2ppv-db.com/opengraph-image?0c674a23bfd755fe">'))
        self.assertEqual(d.get("title_jp"), "A real title")
        self.assertIsNone(d.get("cover_url"))

    def test_a_real_page_still_parses(self):
        d = scan.parse_fc2ppvdb_video(
            page('<meta property="og:title" content="FC2-PPV-1234567 Real movie">'
                 '<meta property="og:image" content="https://cdn.example/real.jpg">'))
        self.assertEqual(d.get("title_jp"), "Real movie")
        self.assertEqual(d.get("cover_url"), "https://cdn.example/real.jpg")

    def test_rejects_non_fc2_page(self):
        self.assertEqual(scan.parse_fc2ppvdb_video("<html>something else</html>"), {})

    def test_censorship_only_from_mosaic_status(self):
        # the word "Uncensored" in chrome must NOT make a censored movie uncensored
        self.assertEqual(
            scan.parse_fc2ppvdb_video(page('"mosaicStatus":"THIN" <a>Uncensored</a> "NONE"'))
            .get("censorship"), "censored")
        self.assertEqual(
            scan.parse_fc2ppvdb_video(page('"mosaicStatus":"NONE"')).get("censorship"),
            "uncensored")

    def test_no_mosaic_status_says_nothing(self):
        d = scan.parse_fc2ppvdb_video(page("plain page mentioning Uncensored once"))
        self.assertIsNone(d.get("censorship"))


class FfjavTest(unittest.TestCase):
    PAGE = ('<html>'
            '<div class="card mb-3"><img class="image" src="/x/FC2-PPV-9999999.png">'
            '<h5 class="title is-4"><a href="#">FC2-PPV-9999999 other movie</a></h5>'
            '<p class="subtitle is-6"><a href="#">2019-01-01</a></p></div>'
            '<div class="card mb-3"><img class="image" src="/x/FC2-PPV-1234567.png">'
            '<h5 class="title is-4"><a href="#">FC2-PPV-1234567 our movie</a></h5>'
            '<p class="subtitle is-6"><a href="#">2024-06-30</a></p></div></html>')

    def setUp(self):
        self._real = scan._get

    def tearDown(self):
        scan._get = self._real

    def test_date_comes_from_our_card(self):
        scan._get = lambda url, **k: self.PAGE
        got = scan.fetch_ffjav("FC2-PPV-1234567")
        self.assertEqual(got["release_date"], "2024-06-30")   # not 2019-01-01
        self.assertEqual(got["title_jp"], "our movie")

    def test_absent_code_returns_nothing(self):
        scan._get = lambda url, **k: self.PAGE
        self.assertEqual(scan.fetch_ffjav("FC2-PPV-5550000"), {})

    def test_no_page(self):
        scan._get = lambda url, **k: None
        self.assertEqual(scan.fetch_ffjav("FC2-PPV-1234567"), {})


class ActressMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matches_existing_by_name(self):
        aid = self.con.execute("INSERT INTO actresses(name_jp) VALUES('みき')").lastrowid
        self.assertEqual(scan._fc2_actress(self.con, {"name": "みき", "aliases": []}),
                         (aid, "name"))

    def test_matches_by_uuid_and_backfills_the_column(self):
        aid = self.con.execute("INSERT INTO actresses(name_jp) VALUES('X')").lastrowid
        self.con.execute("INSERT INTO links VALUES(?, 'fc2ppvdb', 'u-1')", (aid,))
        self.assertEqual(scan._fc2_actress(self.con, {"name": "other", "uuid": "u-1",
                                                      "aliases": []}), (aid, "uuid"))
        self.assertEqual(self.con.execute(
            "SELECT fc2ppvdb_uuid FROM actresses WHERE id=?", (aid,)).fetchone()[0], "u-1")

    def test_dead_link_is_ignored(self):
        """A link pointing at a deleted actress must not resolve to her id."""
        self.con.execute("INSERT INTO links VALUES(4242, 'fc2ppvdb', 'u-dead')")
        aid, how = scan._fc2_actress(self.con, {"name": "fresh", "uuid": "u-dead",
                                                "aliases": []})
        self.assertEqual(how, "new")
        self.assertNotEqual(aid, 4242)

    def test_ambiguous_alias_creates_a_new_record(self):
        a = self.con.execute("INSERT INTO actresses(name_jp) VALUES('A')").lastrowid
        b = self.con.execute("INSERT INTO actresses(name_jp) VALUES('B')").lastrowid
        for who in (a, b):
            self.con.execute("INSERT INTO aliases VALUES(?, 'ゆき')", (who,))
        aid, how = scan._fc2_actress(self.con, {"name": "ゆき", "aliases": []})
        self.assertEqual(how, "new")
        self.assertNotIn(aid, (a, b))

    def test_unique_alias_still_matches(self):
        a = self.con.execute("INSERT INTO actresses(name_jp) VALUES('A')").lastrowid
        self.con.execute("INSERT INTO aliases VALUES(?, 'ほのか')", (a,))
        self.assertEqual(scan._fc2_actress(self.con, {"name": "ほのか", "aliases": []}),
                         (a, "alias"))


class ApplyTest(unittest.TestCase):
    CODE = "FC2-PPV-1000001"

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.con.execute("INSERT INTO works(code, in_library) VALUES(?,1)", (self.CODE,))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cens(self):
        return self.con.execute("SELECT censorship, censorship_src FROM works WHERE code=?",
                                (self.CODE,)).fetchone()

    def test_uncensored_tag_sets_censorship(self):
        scan._apply_fc2db(self.con, self.CODE, {"tags": ["無修正"], "actresses": []})
        self.assertEqual(self.cens()[0], "uncensored")

    def test_light_mosaic_tag_means_censored(self):
        scan._apply_fc2db(self.con, self.CODE, {"tags": ["薄モ"], "actresses": []})
        self.assertEqual(self.cens()[0], "censored")

    def test_no_tag_leaves_the_column_alone(self):
        self.con.execute("UPDATE works SET censorship='censored' WHERE code=?", (self.CODE,))
        scan._apply_fc2db(self.con, self.CODE, {"tags": [], "actresses": []})
        self.assertEqual(self.cens()[0], "censored")

    def test_manual_value_is_never_overwritten(self):
        self.con.execute("UPDATE works SET censorship='censored', censorship_src='manual' "
                         "WHERE code=?", (self.CODE,))
        scan._apply_fc2db(self.con, self.CODE, {"tags": ["無修正"], "actresses": []})
        self.assertEqual(self.cens(), ("censored", "manual"))

    def test_cast_rows_are_written(self):
        scan._apply_fc2db(self.con, self.CODE, {"tags": [], "actresses": [
            {"name": "Lead", "aliases": []}, {"name": "Costar", "aliases": []}]})
        rows = dict(self.con.execute(
            "SELECT a.name_jp, wa.role FROM work_actresses wa "
            "JOIN actresses a ON a.id=wa.actress_id WHERE wa.code=?", (self.CODE,)))
        self.assertEqual(rows, {"Lead": "main", "Costar": "costar"})

    def test_solo_page_does_not_wipe_existing_cast(self):
        """A page naming one performer cannot prove the others are gone."""
        other = self.con.execute("INSERT INTO actresses(name_jp) VALUES('Kept')").lastrowid
        self.con.execute("INSERT INTO work_actresses VALUES(?,?, 'costar')", (self.CODE, other))
        scan._apply_fc2db(self.con, self.CODE, {"tags": [], "actresses": [
            {"name": "Lead", "aliases": []}]})
        names = {r[0] for r in self.con.execute(
            "SELECT a.name_jp FROM work_actresses wa JOIN actresses a ON a.id=wa.actress_id "
            "WHERE wa.code=?", (self.CODE,))}
        self.assertIn("Kept", names)

    def test_rescanning_is_idempotent(self):
        data = {"tags": [], "actresses": [{"name": "Lead", "aliases": []},
                                          {"name": "Costar", "aliases": []}]}
        scan._apply_fc2db(self.con, self.CODE, data)
        scan._apply_fc2db(self.con, self.CODE, data)
        n = self.con.execute("SELECT COUNT(*) FROM work_actresses WHERE code=?",
                             (self.CODE,)).fetchone()[0]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()


class CodeGuardTest(unittest.TestCase):
    """A string handed to scan_library() used to be walked character by character, and
    scan_code() created a catalog row for every letter. Eight rows named n, o, t, -, a, l,
    i, s survived in the real catalog — "not-a-list" with the duplicates swallowed by
    INSERT OR IGNORE."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        return [r[0] for r in self.con.execute("SELECT code FROM works")]

    def test_a_single_character_never_becomes_a_work(self):
        for junk in ("n", "-", "a", "", "  "):
            self.assertEqual(scan.scan_code(self.con, junk), {})
        self.assertEqual(self.rows(), [])

    def test_a_real_code_still_gets_its_row(self):
        scan.scan_code(self.con, "FC2-PPV-1234567")
        self.assertIn("FC2-PPV-1234567", self.rows())

    def test_a_non_fc2_library_code_still_works(self):
        """The library holds non-FC2 folders (N0627, MIFD-711); the guard must not eat them."""
        scan.scan_code(self.con, "N0627")
        self.assertIn("N0627", self.rows())

    def test_scan_library_refuses_a_bare_string(self):
        with self.assertRaises(TypeError) as cm:
            scan.scan_library("not-a-list", con=self.con)
        self.assertIn("list of codes", str(cm.exception))
        self.assertEqual(self.rows(), [])


class Fc2dbAuthorityTest(unittest.TestCase):
    """fc2ppv-db is the primary source for the cast: it may correct a lead that came from a
    folder name or from its own earlier read, and must never touch one set by hand."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.con.execute("ALTER TABLE works ADD COLUMN fc2db_at TEXT")
        self.con.execute("ALTER TABLE works ADD COLUMN actress_src TEXT")
        self.con.execute("INSERT INTO actresses(id,name_en,source) VALUES(1,'Folder Guess','folder')")
        self.con.commit()
        self.page = {"title_jp": "A title", "tags": [],
                     "actresses": [{"uuid": "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4
                                            + "-" + "d" * 4 + "-" + "e" * 12,
                                    "name": "Real Performer", "face": None, "aliases": []}]}

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lead(self, code):
        return self.con.execute("SELECT actress_id, actress_src FROM works WHERE code=?",
                                (code,)).fetchone()

    def test_it_corrects_a_folder_derived_lead(self):
        self.con.execute("INSERT INTO works(code,actress_id,in_library,availability,source) "
                         "VALUES('FC2-PPV-1',1,1,'available','folder')")
        scan._apply_fc2db(self.con, "FC2-PPV-1", self.page)
        aid, src = self.lead("FC2-PPV-1")
        self.assertNotEqual(aid, 1)                 # COALESCE used to freeze the guess
        self.assertEqual(src, "fc2ppv-db")

    def test_it_never_overrides_a_hand_set_lead(self):
        self.con.execute("INSERT INTO works(code,actress_id,actress_src,in_library,"
                         "availability,source) VALUES('FC2-PPV-2',1,'manual',1,"
                         "'available','manual')")
        scan._apply_fc2db(self.con, "FC2-PPV-2", self.page)
        self.assertEqual(self.lead("FC2-PPV-2"), (1, "manual"))

    def test_it_stamps_when_the_page_was_read(self):
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-3',1,'available','fs')")
        scan._apply_fc2db(self.con, "FC2-PPV-3", self.page)
        at = self.con.execute("SELECT fc2db_at FROM works WHERE code='FC2-PPV-3'").fetchone()[0]
        self.assertTrue(at)

    def test_a_page_with_no_cast_is_still_stamped(self):
        """Otherwise every scan re-reads the same empty pages forever."""
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-4',1,'available','fs')")
        scan._apply_fc2db(self.con, "FC2-PPV-4", {"title_jp": "T", "tags": [], "actresses": []})
        at = self.con.execute("SELECT fc2db_at FROM works WHERE code='FC2-PPV-4'").fetchone()[0]
        self.assertTrue(at)


class AbsentStampTest(unittest.TestCase):
    """A code fc2ppv-db has no page for must be remembered as asked-and-answered. Stamping
    only the hits left 109 dead codes permanently "never checked", so every future scan
    re-read all of them — ~5 minutes of resolver time per run, growing with the library."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.con.execute("ALTER TABLE works ADD COLUMN fc2db_at TEXT")
        self.con.execute("ALTER TABLE works ADD COLUMN actress_src TEXT")
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-7000001',1,'available','fs')")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stamp(self, code="FC2-PPV-7000001"):
        return self.con.execute("SELECT fc2db_at FROM works WHERE code=?",
                                (code,)).fetchone()[0]

    def test_an_absent_code_is_stamped(self):
        real = scan.fetch_fc2ppvdb
        scan.fetch_fc2ppvdb = lambda *a, **k: {}          # the site's not-found shell
        try:
            p = scan.scan_fc2db(self.con, ["FC2-PPV-7000001"], None, None, progress={})
        finally:
            scan.fetch_fc2ppvdb = real
        self.assertEqual(p["absent"], 1)
        self.assertEqual(p["hits"], 0)
        self.assertTrue(self.stamp(), "an absent code must not stay 'never checked'")

    def test_a_transport_failure_is_NOT_stamped(self):
        """A resolver that was down must be retried, unlike a page that simply is not there."""
        real = scan.fetch_fc2ppvdb
        scan.fetch_fc2ppvdb = lambda *a, **k: {"_error": "FlareSolverr unreachable"}
        try:
            p = scan.scan_fc2db(self.con, ["FC2-PPV-7000001"], None, None, progress={})
        finally:
            scan.fetch_fc2ppvdb = real
        self.assertEqual(p["failed"], 1)
        self.assertIsNone(self.stamp())


class AgeGateSolverTest(unittest.TestCase):
    """The solver path's 18+ branch returned 2 values from a 3-value function, so
    fetch_fc2ppvdb raised ValueError before _error was set: the abort never tripped and a
    scan walked the whole library making futile round trips, reporting nothing and
    stamping nothing."""

    AGE = ('<html>fc2ppv <meta property="og:title" '
           'content="Age Verification - FC2PPV-DB"></html>')

    def setUp(self):
        self.real_post = scan._solver_post
        scan._solver_post = lambda ep, p, timeout: (
            {"status": "ok"} if p.get("cmd") == "sessions.create"
            else {"status": "ok", "solution": {"status": 200, "response": self.AGE}})
        scan._SOLVER_LIVE["endpoint"] = "http://solver.test/v1"

    def tearDown(self):
        scan._solver_post = self.real_post
        scan._SOLVER_LIVE["endpoint"] = None

    def test_solver_get_returns_three_values(self):
        self.assertEqual(len(scan.solver_get("http://solver.test", "https://fc2ppv-db.com/x")), 3)

    def test_fetch_reports_the_age_gate_instead_of_raising(self):
        got = scan.fetch_fc2ppvdb("FC2-PPV-4000001", solver_url="http://solver.test")
        self.assertIn("18+", got.get("_error", ""))

    def test_a_scan_stops_on_the_first_age_gate(self):
        """The wall is the same for every code — walking 450 of them helps nobody."""
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE works(code TEXT PRIMARY KEY, in_library INT,"
                          " availability TEXT, source TEXT, fc2db_at TEXT);")
        p = {}
        scan.scan_fc2db(con, ["FC2-PPV-400000%d" % i for i in range(1, 8)],
                        None, None, progress=p, solver_url="http://solver.test")
        self.assertLess(p["done"], 7, "it walked the whole list behind a wall it had hit")
        self.assertEqual(p["failed"], 1)
        self.assertIn("18+", p["error"])
        con.close()


class Fc2dbHitTest(unittest.TestCase):
    """A page with a date/cover/tags but no performer was counted 'absent': its fields were
    thrown away AND it was stamped, so "Scan missing" never came back for it."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.con.execute("ALTER TABLE works ADD COLUMN fc2db_at TEXT")
        self.con.execute("ALTER TABLE works ADD COLUMN actress_src TEXT")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_cast_less_page_with_fields_is_a_hit(self):
        page = {"title_jp": None, "actresses": [], "release_date": "2026-01-02",
                "cover_url": "https://cdn.example/x.jpg", "tags": []}
        self.assertTrue(scan._has_fields(page))
        p = {}
        real = scan.fetch_fc2ppvdb
        scan.fetch_fc2ppvdb = lambda *a, **k: page
        try:
            scan.scan_fc2db(self.con, ["FC2-PPV-4000002"], None, None, progress=p)
        finally:
            scan.fetch_fc2ppvdb = real
        self.assertEqual((p["hits"], p["absent"]), (1, 0))
        row = self.con.execute("SELECT release_date, cover_url FROM works "
                               "WHERE code='FC2-PPV-4000002'").fetchone()
        self.assertEqual(row[0], "2026-01-02")
        self.assertTrue(row[1])

    def test_the_sites_not_found_shell_is_still_absent(self):
        self.assertFalse(scan._has_fields({}))
        self.assertFalse(scan._has_fields({"_missing": True}))


class ProgressCountersTest(unittest.TestCase):
    """One PROGRESS dict, two passes. absent/failed leaked in from the previous run, and
    the fc2ppv-db pass zeroed the titles pass's hits."""

    def test_a_titles_run_clears_the_previous_runs_counters(self):
        p = {"absent": 58, "failed": 3, "hits": 9}
        scan.scan_library([], con=sqlite3.connect(":memory:"), progress=p)
        self.assertEqual((p["absent"], p["failed"]), (0, 0))

    def test_hits_stay_additive_across_a_held_handoff(self):
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE works(code TEXT PRIMARY KEY, in_library INT,"
                          " availability TEXT, source TEXT, fc2db_at TEXT);")
        p = {"hits": 12, "hold": True}          # titles pass wrote 12
        real = scan.fetch_fc2ppvdb
        scan.fetch_fc2ppvdb = lambda *a, **k: {}
        try:
            scan.scan_fc2db(con, ["FC2-PPV-4000003"], None, None, progress=p)
        finally:
            scan.fetch_fc2ppvdb = real
        self.assertEqual(p["hits"], 12)         # not reset to 0
        con.close()

    def test_running_is_held_across_the_handoff(self):
        p = {"hold": True}
        scan.scan_library([], con=sqlite3.connect(":memory:"), progress=p)
        self.assertTrue(p["running"], "a poll in the gap would paint 'Done' mid-scan")


class SourceToggleTest(unittest.TestCase):
    """The Settings checkboxes under "Other sources" were written to sources.json and never
    read: unticking 123av or ffjav changed nothing."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.calls = []
        self.real = (scan.fetch_123av_title, scan.fetch_ffjav)
        scan.fetch_123av_title = lambda n: self.calls.append("123av") or "En title"
        scan.fetch_ffjav = lambda c: self.calls.append("ffjav") or {}

    def tearDown(self):
        scan.fetch_123av_title, scan.fetch_ffjav = self.real
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_opinion_uses_everything(self):
        scan.scan_code(self.con, "FC2-PPV-4000010")
        self.assertEqual(sorted(self.calls), ["123av", "ffjav"])

    def test_an_unticked_source_is_not_fetched(self):
        scan.scan_code(self.con, "FC2-PPV-4000011", sources={"ffjav"})
        self.assertEqual(self.calls, ["ffjav"])

    def test_all_unticked_fetches_nothing(self):
        scan.scan_code(self.con, "FC2-PPV-4000012", sources=set())
        self.assertEqual(self.calls, [])


class Review3ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_title_pass_records_a_failure(self):
        """scan_library did `except Exception: pass` — a failed 123av/ffjav fetch or a
        constraint error counted nothing and the dialog reported a clean pass."""
        real = scan.scan_code
        def boom(*a, **k):
            raise RuntimeError("ffjav exploded")
        scan.scan_code = boom
        try:
            p = {}
            scan.scan_library(["FC2-PPV-4000020"], con=self.con, progress=p)
        finally:
            scan.scan_code = real
        self.assertEqual(p["failed"], 1)
        self.assertIn("ffjav exploded", p["error"])

    def test_enrich_skips_an_unticked_source(self):
        """Settings stored the javdatabase / jav.guru boxes and Enrich ignored them."""
        calls = []
        real = (scan.fetch_javdatabase, scan.fetch_javguru_codes)
        scan.fetch_javdatabase = lambda n: calls.append("javdatabase") or {}
        scan.fetch_javguru_codes = lambda n: calls.append("jav.guru") or (None, set())
        self.con.execute("INSERT INTO actresses(id,name_en) VALUES(3,'Some One')")
        try:
            scan.enrich_actress(self.con, 3, dry=True, library_codes=set(), sources={"jav.guru"})
        finally:
            scan.fetch_javdatabase, scan.fetch_javguru_codes = real
        self.assertNotIn("javdatabase", calls)
        self.assertIn("jav.guru", calls)

    def test_a_held_fc2ppv_db_pass_leaves_running_to_its_caller(self):
        """The caller still has to rebuild the index; clearing `running` here let the poll
        reload the grid from the pre-rebuild index."""
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE works(code TEXT PRIMARY KEY, in_library INT,"
                          " availability TEXT, source TEXT, fc2db_at TEXT);")
        real = scan.fetch_fc2ppvdb
        scan.fetch_fc2ppvdb = lambda *a, **k: {}
        try:
            p = {"hold": True}
            scan.scan_fc2db(con, ["FC2-PPV-4000021"], None, None, progress=p)
        finally:
            scan.fetch_fc2ppvdb = real
        self.assertTrue(p["running"])
        con.close()


class HeldCountersTest(unittest.TestCase):
    """The titles pass now runs LAST. Resetting its counters there wiped the cast passes'
    'updated / not found / failed' from the job's final summary."""

    def test_a_held_titles_pass_keeps_the_jobs_totals(self):
        p = {"hold": True, "hits": 7, "absent": 4, "failed": 1, "error": "x"}
        scan.scan_library([], con=sqlite3.connect(":memory:"), progress=p)
        self.assertEqual((p["hits"], p["absent"], p["failed"]), (7, 4, 1))

    def test_a_held_fc2dbnet_pass_keeps_the_jobs_totals(self):
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE works(code TEXT PRIMARY KEY, in_library INT,"
                          " availability TEXT, source TEXT, fc2dbnet_at TEXT);")
        real, real_sleep = scan.fetch_fc2dbnet, scan.time.sleep
        scan.fetch_fc2dbnet, scan.time.sleep = (lambda *a, **k: {"_missing": True}), (lambda s: None)
        try:
            p = {"hold": True, "hits": 5, "absent": 2, "failed": 0}
            scan.scan_fc2dbnet(con, ["FC2-PPV-4000030"], progress=p)
        finally:
            scan.fetch_fc2dbnet, scan.time.sleep = real, real_sleep
        self.assertEqual((p["hits"], p["absent"]), (5, 3))     # added to, not reset
        con.close()
